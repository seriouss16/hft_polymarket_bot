"""Hub for strategy registration, switching, and parallel execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from core.strategy_base import BaseStrategy


@dataclass
class StrategyResult:
    """Represent a result emitted by a named strategy."""

    strategy: str
    payload: dict[str, Any]


class StrategyHub:
    """Route ticks to one or many strategies through a stable API."""

    def __init__(self) -> None:
        """Initialize hub state."""
        self._strategies: dict[str, BaseStrategy] = {}
        self._active_name: str | None = None
        self._parallel_enabled = False

    def register(self, strategy: BaseStrategy) -> None:
        """Register strategy instance by unique name."""
        if not getattr(strategy, "name", ""):
            raise ValueError("Strategy must define non-empty 'name'.")
        self._strategies[strategy.name] = strategy
        if self._active_name is None:
            self._active_name = strategy.name

    def set_active(self, name: str) -> None:
        """Set active strategy for single-mode execution."""
        if name not in self._strategies:
            raise KeyError(f"Unknown strategy: {name}")
        self._active_name = name

    def enable_parallel(self, enabled: bool = True) -> None:
        """Enable or disable parallel strategy mode."""
        self._parallel_enabled = bool(enabled)

    def list_strategies(self) -> list[str]:
        """Return registered strategy names."""
        return sorted(self._strategies.keys())

    def get_active_strategy(self) -> BaseStrategy:
        """Return currently active strategy in single-mode."""
        if self._active_name is None:
            raise RuntimeError("No active strategy in hub.")
        return self._strategies[self._active_name]

    @property
    def entry_max_latency_ms(self) -> float:
        """Expose latency threshold from active strategy."""
        return float(self.get_active_strategy().entry_max_latency_ms)

    def reload_profile_params(self) -> None:
        """Re-read session-profile env-vars into all registered strategies.

        Call after session_profile.apply_profile() switches NIGHT/DAWN/DAY so
        that cached engine attributes (speed gates, imbalance thresholds, etc.)
        are immediately updated without requiring a full restart.
        """
        for strategy in self._strategies.values():
            if hasattr(strategy, "reload_profile_params"):
                strategy.reload_profile_params()

    def reset_for_new_market(self) -> None:
        """Reset state for all registered strategies."""
        for strategy in self._strategies.values():
            strategy.reset_for_new_market()

    def get_trend_state(self) -> dict[str, Any]:
        """Return trend state of active strategy."""
        return self.get_active_strategy().get_trend_state()

    def get_rsi_v5_state(self) -> dict[str, Any]:
        """Return RSI or reaction-score state of active strategy."""
        return self.get_active_strategy().get_rsi_v5_state()

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
        slot_anchor_price: float = 0.0,
    ) -> dict[str, Any] | None:
        """Run one active strategy or all strategies and return merged decision."""
        if not self._parallel_enabled:
            return await self.get_active_strategy().process_tick(
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
                slot_anchor_price=slot_anchor_price,
            )

        results: list[StrategyResult] = []
        for name, strategy in self._strategies.items():
            payload = await strategy.process_tick(
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
                slot_anchor_price=slot_anchor_price,
            )
            if isinstance(payload, dict) and payload.get("event"):
                results.append(StrategyResult(strategy=name, payload=payload))
        if not results:
            return None
        prioritized = sorted(
            results,
            key=lambda item: 0 if item.payload.get("event") == "CLOSE" else 1,
        )[0]
        payload = dict(prioritized.payload)
        payload["strategy"] = prioritized.strategy
        return payload

    def generate_live_signal(
        self,
        fast_price: float,
        poly_mid: float,
        zscore: float,
        price_history: list[float] | None = None,
        recent_pnl: float = 0.0,
        latency_ms: float = 0.0,
    ) -> str | None:
        """Return first non-empty signal from active or all strategies."""
        if not self._parallel_enabled:
            return self.get_active_strategy().generate_live_signal(
                fast_price,
                poly_mid,
                zscore,
                price_history=price_history,
                recent_pnl=recent_pnl,
                latency_ms=latency_ms,
            )
        for strategy in self._strategies.values():
            signal = strategy.generate_live_signal(
                fast_price,
                poly_mid,
                zscore,
                price_history=price_history,
                recent_pnl=recent_pnl,
                latency_ms=latency_ms,
            )
            if signal:
                return signal
        return None
