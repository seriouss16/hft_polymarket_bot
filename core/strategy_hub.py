"""Hub for strategy registration, switching, and parallel execution."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from core.strategy_base import BaseStrategy

logger = logging.getLogger(__name__)


@dataclass(slots=True)
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
        # Metrics counters
        self._strategy_errors: int = 0
        self._strategy_timeouts: int = 0
        # Config flags (read from env at init)
        self._use_gather: bool = os.getenv("HFT_USE_GATHER", "1") == "1"
        timeout_ms = float(os.getenv("HFT_STRATEGY_TIMEOUT_MS", "100"))
        self._strategy_timeout_sec: float | None = timeout_ms / 1000.0 if timeout_ms > 0 else None

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
        slot_price_to_beat: float = 0.0,
    ) -> dict[str, Any] | None:
        """Run one active strategy or all strategies and return merged decision."""
        import time

        signal_ts = time.time()
        if not self._parallel_enabled:
            res = await self.get_active_strategy().process_tick(
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
                slot_price_to_beat=slot_price_to_beat,
            )
            if res:
                res["signal_ts"] = signal_ts
            return res

        # Execute strategies either concurrently (gather) or sequentially
        raw_results: list[tuple[str, dict[str, Any] | None | Exception]] = []

        if self._use_gather:
            # Concurrent execution: create tasks for all strategies
            tasks = []
            for name, strategy in self._strategies.items():
                coro = strategy.process_tick(
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
                    slot_price_to_beat=slot_price_to_beat,
                )
                if self._strategy_timeout_sec is not None:
                    # Wrap with timeout; TimeoutError will be raised on timeout
                    coro = asyncio.wait_for(coro, timeout=self._strategy_timeout_sec)
                tasks.append((name, coro))

            # Gather all results, capturing exceptions (including TimeoutError)
            task_coros = [coro for _, coro in tasks]
            gathered = await asyncio.gather(*task_coros, return_exceptions=True)
            raw_results = list(zip([name for name, _ in tasks], gathered))
        else:
            # Sequential execution: one strategy at a time
            for name, strategy in self._strategies.items():
                try:
                    coro = strategy.process_tick(
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
                        slot_price_to_beat=slot_price_to_beat,
                    )
                    if self._strategy_timeout_sec is not None:
                        result = await asyncio.wait_for(coro, timeout=self._strategy_timeout_sec)
                    else:
                        result = await coro
                    raw_results.append((name, result))
                except asyncio.TimeoutError:
                    self._strategy_timeouts += 1
                    logger.warning("Strategy %s timed out after %.1fms", name, self._strategy_timeout_sec * 1000)
                    raw_results.append((name, asyncio.TimeoutError()))
                except Exception as exc:
                    self._strategy_errors += 1
                    logger.warning("Strategy %s raised exception: %s", name, exc)
                    raw_results.append((name, exc))

        # Process results: filter None, handle exceptions, build StrategyResult list
        results: list[StrategyResult] = []
        for name, result in raw_results:
            if isinstance(result, Exception):
                # Check if it's a timeout
                if isinstance(result, asyncio.TimeoutError):
                    self._strategy_timeouts += 1
                    logger.warning("Strategy %s timed out after %.1fms", name, self._strategy_timeout_sec * 1000)
                else:
                    self._strategy_errors += 1
                    logger.warning("Strategy %s raised exception: %s", name, result)
                continue
            if result is None:
                continue
            if isinstance(result, dict) and result.get("event"):
                results.append(StrategyResult(strategy=name, payload=result))

        if not results:
            return None

        # Merge results: priority order: entry signals > exit signals > hold
        # If multiple entry signals, pick highest confidence
        merged = self._merge_strategy_results(results)
        if merged:
            merged["signal_ts"] = signal_ts
        return merged

    def _merge_strategy_results(self, results: list[StrategyResult]) -> dict[str, Any]:
        """Merge multiple strategy results into a single decision.

        Priority: ENTRY > EXIT > HOLD.
        For multiple ENTRY signals, select the one with highest confidence.
        """
        # Define event priority (lower number = higher priority)
        event_priority = {"ENTRY": 0, "EXIT": 1, "CLOSE": 1, "HOLD": 2}

        # Sort by priority first, then by confidence (descending) for ENTRY events
        def sort_key(item: StrategyResult) -> tuple[int, float]:
            event = item.payload.get("event", "HOLD").upper()
            priority = event_priority.get(event, 2)
            # For ENTRY events, use confidence as secondary sort key (desc)
            # For others, confidence doesn't matter
            if event == "ENTRY":
                confidence = item.payload.get("confidence", 0.0)
                return (priority, -confidence)
            return (priority, 0)

        sorted_results = sorted(results, key=sort_key)
        best = sorted_results[0]
        payload = dict(best.payload)
        payload["strategy"] = best.strategy
        return payload

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
        """Return first non-empty signal from active or all strategies."""
        if not self._parallel_enabled:
            return self.get_active_strategy().generate_live_signal(
                fast_price,
                poly_mid,
                zscore,
                price_history=price_history,
                recent_pnl=recent_pnl,
                latency_ms=latency_ms,
                poly_orderbook=poly_orderbook,
                seconds_to_expiry=seconds_to_expiry,
            )
        for strategy in self._strategies.values():
            signal = strategy.generate_live_signal(
                fast_price,
                poly_mid,
                zscore,
                price_history=price_history,
                recent_pnl=recent_pnl,
                latency_ms=latency_ms,
                poly_orderbook=poly_orderbook,
                seconds_to_expiry=seconds_to_expiry,
            )
            if signal:
                return signal
        return None
