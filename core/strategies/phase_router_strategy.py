"""Route each tick to latency or soft-flow parameter sets on a single HFTEngine."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from core.engine import HFTEngine
from core.market_phase import diagnose_phase, select_engine_profile
from core.strategy_base import BaseStrategy


class PhaseRouterStrategy(BaseStrategy):
    """Apply market-phase profile selection then delegate to one shared HFTEngine."""

    name = "phase_router"

    def __init__(self, pnl_tracker: Any, is_test_mode: bool = True) -> None:
        """Build engine and cache max latency for feed warnings."""
        self._engine = HFTEngine(
            pnl_tracker,
            is_test_mode=is_test_mode,
            strategy_label=self.name,
        )
        self._max_entry_latency_ms = float(self._engine.max_entry_latency_ms_all_profiles())
        self._last_applied: str | None = None
        self._last_switch_log_ts = 0.0
        self._last_diag_log_ts = 0.0

    @property
    def entry_max_latency_ms(self) -> float:
        """Return max entry latency across profiles for staleness logging."""
        return self._max_entry_latency_ms

    def reset_for_new_market(self) -> None:
        """Reset wrapped engine; profile returns to latency inside engine."""
        self._engine.reset_for_new_market()
        self._last_applied = None

    def get_trend_state(self) -> dict[str, Any]:
        """Return trend diagnostics from wrapped engine."""
        return self._engine.get_trend_state()

    def get_rsi_v5_state(self) -> dict[str, float]:
        """Return RSI diagnostics from wrapped engine."""
        return self._engine.get_rsi_v5_state()

    def get_active_profile(self) -> str:
        """Return last applied entry profile (latency or soft_flow)."""
        return self._engine.get_active_profile()

    def _apply_phase(self, latency_ms: float) -> None:
        """Select profile from prior trend state and apply before signal logic."""
        tr = self._engine.get_trend_state()
        profile = select_engine_profile(tr, latency_ms)
        self._engine.apply_profile(profile)
        now = time.time()
        diag = diagnose_phase(tr, latency_ms)
        if not diag.get("logic_ok", True):
            logging.warning(
                "Phase classifier inconsistency: selected=%s soft_eligible=%s (check select_engine_profile vs diagnose_phase).",
                diag.get("selected"),
                diag.get("soft_eligible"),
            )
        if os.getenv("HFT_LOG_PHASE_DIAGNOSTICS", "0") == "1":
            if (
                profile != self._last_applied
                or now - self._last_diag_log_ts >= float(os.getenv("HFT_LOG_PHASE_DIAGNOSTICS_SEC", "45"))
            ):
                th = diag.get("thresholds") or {}
                obs = diag.get("observed") or {}
                chk = diag.get("checks") or {}
                logging.info(
                    "Phase diag: selected=%s soft_eligible=%s logic_ok=%s | "
                    "trend=%s speed=%.2f (max_soft %.1f) edge=%.2f (max_soft %.1f) "
                    "age=%.1f (min %.1f) stale=%.0f (max_soft %.0f) | "
                    "checks dir=%s age=%s spd=%s edge=%s lat=%s | blockers=%s",
                    diag.get("selected"),
                    diag.get("soft_eligible"),
                    diag.get("logic_ok"),
                    obs.get("trend"),
                    float(obs.get("speed", 0.0)),
                    float(th.get("soft_max_abs_speed", 0.0)),
                    float(obs.get("edge", 0.0)),
                    float(th.get("soft_max_abs_edge", 0.0)),
                    float(obs.get("age", 0.0)),
                    float(th.get("soft_min_age", 0.0)),
                    float(obs.get("staleness_ms", 0.0)),
                    float(th.get("soft_max_latency_ms", 0.0)),
                    chk.get("directional"),
                    chk.get("age_ok"),
                    chk.get("speed_ok"),
                    chk.get("edge_ok"),
                    chk.get("latency_ok"),
                    ",".join(diag.get("blockers") or []) or "-",
                )
                self._last_diag_log_ts = now
        if profile != self._last_applied:
            if now - self._last_switch_log_ts >= 15.0 or self._last_applied is None:
                logging.info(
                    "Market phase profile: %s (trend=%s speed=%.2f edge=%.2f age=%.1fs stale=%.0fms)",
                    profile,
                    tr.get("trend"),
                    float(tr.get("speed", 0.0)),
                    float(tr.get("edge", 0.0)),
                    float(tr.get("age", 0.0)),
                    float(latency_ms),
                )
                self._last_switch_log_ts = now
            self._last_applied = profile

    async def process_tick(
        self,
        fast_price: float,
        poly_orderbook: dict[str, Any],
        price_history: list[float],
        lstm_forecast: float,
        zscore: float = 0.0,
        latency_ms: float = 0.0,
        recent_pnl: float = 0.0,
        meta_enabled: bool = True,
        seconds_to_expiry: float | None = None,
        cex_bid_imbalance: float | None = None,
    ) -> dict[str, Any] | None:
        """Apply phase profile for this tick then run the shared engine."""
        self._apply_phase(latency_ms)
        return await self._engine.process_tick(
            fast_price=fast_price,
            poly_orderbook=poly_orderbook,
            price_history=price_history,
            lstm_forecast=lstm_forecast,
            zscore=zscore,
            latency_ms=latency_ms,
            recent_pnl=recent_pnl,
            meta_enabled=meta_enabled,
            seconds_to_expiry=seconds_to_expiry,
            cex_bid_imbalance=cex_bid_imbalance,
        )

    def generate_live_signal(
        self,
        fast_price: float,
        poly_mid: float,
        zscore: float,
        price_history: list[float] | None = None,
        recent_pnl: float = 0.0,
        latency_ms: float = 0.0,
    ) -> str | None:
        """Match live signal path to the same phase selection as process_tick."""
        self._apply_phase(latency_ms)
        return self._engine.generate_live_signal(
            fast_price,
            poly_mid,
            zscore,
            price_history=price_history,
            recent_pnl=recent_pnl,
            latency_ms=latency_ms,
        )
