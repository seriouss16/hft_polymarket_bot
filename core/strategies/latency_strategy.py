"""Adapter for legacy latency-arbitrage strategy implementation."""

from __future__ import annotations

from typing import Any

from core.engine import HFTEngine
from core.strategy_base import BaseStrategy


class LatencyArbitrageStrategy(BaseStrategy):
    """Wrap legacy `HFTEngine` into pluggable `BaseStrategy` contract."""

    name = "latency_arbitrage"

    def __init__(self, pnl_tracker: Any, is_test_mode: bool = True) -> None:
        """Initialize wrapped legacy engine."""
        self._engine = HFTEngine(
            pnl_tracker,
            is_test_mode=is_test_mode,
            strategy_label=self.name,
        )

    @property
    def entry_max_latency_ms(self) -> float:
        """Expose entry latency threshold."""
        return float(self._engine.entry_max_latency_ms)

    def reload_profile_params(self) -> None:
        """Re-read session-profile env-vars into wrapped engine."""
        self._engine.reload_profile_params()

    def reset_for_new_market(self) -> None:
        """Reset strategy internals for new market context."""
        self._engine.reset_for_new_market()

    def get_trend_state(self) -> dict[str, Any]:
        """Return trend diagnostics from wrapped engine."""
        return self._engine.get_trend_state()

    def get_rsi_v5_state(self) -> dict[str, Any]:
        """Return RSI diagnostics from wrapped engine."""
        return self._engine.get_rsi_v5_state()

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
        skew_ms: float = 0.0,
        **kwargs: Any,
    ) -> dict[str, Any] | None:
        """Forward tick to wrapped strategy engine."""
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
            skew_ms=skew_ms,
            **kwargs,
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
        """Forward live signal generation to wrapped strategy engine."""
        return self._engine.generate_live_signal(
            fast_price,
            poly_mid,
            zscore,
            price_history=price_history,
            recent_pnl=recent_pnl,
            latency_ms=latency_ms,
        )
