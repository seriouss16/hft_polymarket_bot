"""Abstract contracts for pluggable trading strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseStrategy(ABC):
    """Define the minimum strategy API consumed by the runtime hub."""

    name: str

    def performance_label(self) -> str:
        """Return stable key for realized PnL attribution (default: strategy name)."""
        return str(self.name)

    @property
    @abstractmethod
    def entry_max_latency_ms(self) -> float:
        """Return max tolerated feed staleness for entries."""

    @abstractmethod
    def reset_for_new_market(self) -> None:
        """Reset strategy state when market token or slot changes."""

    @abstractmethod
    def get_trend_state(self) -> dict[str, Any]:
        """Return current trend diagnostics."""

    @abstractmethod
    def get_rsi_v5_state(self) -> dict[str, Any]:
        """Return current RSI or reaction-score diagnostics (0–100 scale)."""

    @abstractmethod
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
        """Process market tick and optionally return an OPEN/CLOSE event."""

    @abstractmethod
    def generate_live_signal(
        self,
        fast_price: float,
        poly_mid: float,
        zscore: float,
        price_history: list[float] | None = None,
        recent_pnl: float = 0.0,
        latency_ms: float = 0.0,
        *,
        poly_orderbook: dict[str, Any] | None = None,
        seconds_to_expiry: float | None = None,
    ) -> str | None:
        """Return live signal name or None."""
