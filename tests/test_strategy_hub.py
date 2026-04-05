"""Tests for StrategyHub concurrent execution with asyncio.gather()."""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.strategy_base import BaseStrategy
from core.strategy_hub import StrategyHub, StrategyResult


# Mock strategy for testing
class MockStrategy(BaseStrategy):
    """A test strategy that returns configurable results."""

    def __init__(
        self,
        name: str,
        result: dict[str, Any] | None = None,
        delay: float = 0.0,
        raise_exc: Exception | None = None,
        latency_ms: float = 100.0,
    ):
        """Initialize with test parameters."""
        self.name = name
        self.result = result
        self.delay = delay
        self.raise_exc = raise_exc
        self._entry_max_latency_ms = latency_ms

    @property
    def entry_max_latency_ms(self) -> float:
        return float(self._entry_max_latency_ms)

    def reset_for_new_market(self) -> None:
        pass

    def get_trend_state(self) -> dict[str, Any]:
        return {}

    def get_rsi_v5_state(self) -> dict[str, Any]:
        return {}

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
        """Simulate strategy processing with optional delay and exception."""
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.raise_exc is not None:
            raise self.raise_exc
        if self.result is not None:
            return dict(self.result)
        return None

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
        return None


class TestStrategyHubConcurrent:
    """Test concurrent execution in StrategyHub."""

    @pytest.fixture
    def hub(self):
        """Create a StrategyHub with parallel mode enabled."""
        h = StrategyHub()
        h.enable_parallel(True)
        return h

    def test_concurrent_execution_reduces_latency(self, hub, monkeypatch):
        """Verify concurrent execution: total time ≈ max(delay_i) not sum(delay_i)."""
        # Set timeout to None to avoid timeout interference
        monkeypatch.setenv("HFT_STRATEGY_TIMEOUT_MS", "0")
        hub._strategy_timeout_sec = None

        # Create strategies with different delays
        # Strategy A: 100ms, Strategy B: 50ms, Strategy C: 80ms
        strat_a = MockStrategy(name="A", result={"event": "ENTRY", "side": "UP"}, delay=0.1)
        strat_b = MockStrategy(name="B", result={"event": "EXIT", "side": "DOWN"}, delay=0.05)
        strat_c = MockStrategy(name="C", result={"event": "HOLD"}, delay=0.08)

        hub.register(strat_a)
        hub.register(strat_b)
        hub.register(strat_c)

        # Measure wall time for concurrent execution
        start = time.perf_counter()
        result = asyncio.run(
            hub.process_tick(
                fast_price=1.0,
                poly_orderbook={},
                price_history=[],
                lstm_forecast=0.0,
            )
        )
        elapsed = time.perf_counter() - start

        # Should complete in roughly the max delay (0.1s) not sum (0.23s)
        # Allow some tolerance for asyncio overhead
        assert elapsed < 0.15, f"Concurrent execution took {elapsed}s, expected ~0.1s"
        assert result is not None
        # The result should be ENTRY from strat_a (highest priority event)
        assert result["event"] == "ENTRY"
        assert result["strategy"] == "A"

    def test_exception_handling_continues_other_strategies(self, hub, monkeypatch, caplog):
        """One strategy raising exception should not block others."""
        monkeypatch.setenv("HFT_STRATEGY_TIMEOUT_MS", "0")
        hub._strategy_timeout_sec = None

        # Strategy A raises, B and C succeed
        strat_a = MockStrategy(name="A", result={"event": "ENTRY", "side": "UP"}, raise_exc=RuntimeError("fail"))
        strat_b = MockStrategy(name="B", result={"event": "EXIT", "side": "DOWN"})
        strat_c = MockStrategy(name="C", result={"event": "HOLD"})

        hub.register(strat_a)
        hub.register(strat_b)
        hub.register(strat_c)

        result = asyncio.run(
            hub.process_tick(
                fast_price=1.0,
                poly_orderbook={},
                price_history=[],
                lstm_forecast=0.0,
            )
        )

        # Should still get a result from the successful strategies
        assert result is not None
        # ENTRY has highest priority, but strat_a raised, so we should get EXIT (next priority)
        assert result["event"] == "EXIT"
        assert result["strategy"] == "B"
        assert hub._strategy_errors == 1

        # Check warning was logged
        assert any("Strategy A raised exception" in record.message for record in caplog.records)

    def test_result_merging_priority_entry_vs_exit(self, hub, monkeypatch):
        """ENTRY signals should be prioritized over EXIT and HOLD."""
        monkeypatch.setenv("HFT_STRATEGY_TIMEOUT_MS", "0")
        hub._strategy_timeout_sec = None

        # All strategies return different events
        strat_a = MockStrategy(name="A", result={"event": "ENTRY", "side": "UP", "confidence": 0.8})
        strat_b = MockStrategy(name="B", result={"event": "EXIT", "side": "DOWN"})
        strat_c = MockStrategy(name="C", result={"event": "HOLD"})

        hub.register(strat_a)
        hub.register(strat_b)
        hub.register(strat_c)

        result = asyncio.run(
            hub.process_tick(
                fast_price=1.0,
                poly_orderbook={},
                price_history=[],
                lstm_forecast=0.0,
            )
        )

        assert result is not None
        assert result["event"] == "ENTRY"
        assert result["strategy"] == "A"

    def test_result_merging_multiple_entries_highest_confidence(self, hub, monkeypatch):
        """Multiple ENTRY signals: pick highest confidence."""
        monkeypatch.setenv("HFT_STRATEGY_TIMEOUT_MS", "0")
        hub._strategy_timeout_sec = None

        # Three strategies with ENTRY but different confidences
        strat_a = MockStrategy(name="A", result={"event": "ENTRY", "side": "UP", "confidence": 0.5})
        strat_b = MockStrategy(name="B", result={"event": "ENTRY", "side": "DOWN", "confidence": 0.9})
        strat_c = MockStrategy(name="C", result={"event": "ENTRY", "side": "UP", "confidence": 0.7})

        hub.register(strat_a)
        hub.register(strat_b)
        hub.register(strat_c)

        result = asyncio.run(
            hub.process_tick(
                fast_price=1.0,
                poly_orderbook={},
                price_history=[],
                lstm_forecast=0.0,
            )
        )

        assert result is not None
        assert result["event"] == "ENTRY"
        assert result["confidence"] == 0.9
        assert result["side"] == "DOWN"
        assert result["strategy"] == "B"

    def test_timeout_skips_slow_strategy(self, hub, monkeypatch, caplog):
        """Strategy exceeding timeout should be skipped, others succeed."""
        # Set timeout to 50ms
        monkeypatch.setenv("HFT_STRATEGY_TIMEOUT_MS", "50")
        hub._strategy_timeout_sec = 0.05

        # Fast strategy (10ms), slow strategy (200ms)
        strat_fast = MockStrategy(name="fast", result={"event": "ENTRY", "side": "UP"}, delay=0.01)
        strat_slow = MockStrategy(name="slow", result={"event": "EXIT", "side": "DOWN"}, delay=0.2)

        hub.register(strat_fast)
        hub.register(strat_slow)

        result = asyncio.run(
            hub.process_tick(
                fast_price=1.0,
                poly_orderbook={},
                price_history=[],
                lstm_forecast=0.0,
            )
        )

        # Should get result from fast strategy, slow one timed out
        assert result is not None
        assert result["event"] == "ENTRY"
        assert result["strategy"] == "fast"
        assert hub._strategy_timeouts == 1

        # Check timeout warning was logged
        assert any("timed out after" in record.message for record in caplog.records)

    def test_sequential_fallback_when_gather_disabled(self, monkeypatch):
        """HFT_USE_GATHER=0 should fall back to sequential execution."""
        monkeypatch.setenv("HFT_USE_GATHER", "0")
        monkeypatch.setenv("HFT_STRATEGY_TIMEOUT_MS", "0")

        hub = StrategyHub()
        hub.enable_parallel(True)
        # Re-initialize config flags (simulate fresh init)
        hub._use_gather = os.getenv("HFT_USE_GATHER", "1") == "1"
        timeout_ms = float(os.getenv("HFT_STRATEGY_TIMEOUT_MS", "100"))
        hub._strategy_timeout_sec = timeout_ms / 1000.0 if timeout_ms > 0 else None

        # Add strategies with execution order tracking
        execution_order = []

        class OrderedMockStrategy(BaseStrategy):
            def __init__(self, name: str, result: dict[str, Any] | None):
                self.name = name
                self.my_name = name
                self.result = result
                self._entry_max_latency_ms = 100.0

            @property
            def entry_max_latency_ms(self) -> float:
                return float(self._entry_max_latency_ms)

            def reset_for_new_market(self) -> None:
                pass

            def get_trend_state(self) -> dict[str, Any]:
                return {}

            def get_rsi_v5_state(self) -> dict[str, Any]:
                return {}

            async def process_tick(self, **kwargs) -> dict[str, Any] | None:
                execution_order.append(self.my_name)
                await asyncio.sleep(0)  # yield
                return self.result

            def generate_live_signal(self, **kwargs) -> str | None:
                return None

        strat_a = OrderedMockStrategy("A", {"event": "HOLD"})
        strat_b = OrderedMockStrategy("B", {"event": "ENTRY", "side": "UP"})
        strat_c = OrderedMockStrategy("C", {"event": "EXIT"})

        hub.register(strat_a)
        hub.register(strat_b)
        hub.register(strat_c)

        result = asyncio.run(
            hub.process_tick(
                fast_price=1.0,
                poly_orderbook={},
                price_history=[],
                lstm_forecast=0.0,
            )
        )

        # Sequential execution should preserve order
        assert execution_order == ["A", "B", "C"]
        assert result is not None
        assert result["event"] == "ENTRY"

    def test_all_strategies_return_none(self, hub, monkeypatch):
        """If all strategies return None, hub should return None."""
        monkeypatch.setenv("HFT_STRATEGY_TIMEOUT_MS", "0")
        hub._strategy_timeout_sec = None

        strat = MockStrategy(name="single", result=None)
        hub.register(strat)

        result = asyncio.run(
            hub.process_tick(
                fast_price=1.0,
                poly_orderbook={},
                price_history=[],
                lstm_forecast=0.0,
            )
        )

        assert result is None

    def test_merge_close_has_same_priority_as_exit(self, hub, monkeypatch):
        """CLOSE event should have same priority as EXIT (both lower than ENTRY)."""
        monkeypatch.setenv("HFT_STRATEGY_TIMEOUT_MS", "0")
        hub._strategy_timeout_sec = None

        strat_entry = MockStrategy(name="entry", result={"event": "ENTRY", "side": "UP"})
        strat_exit = MockStrategy(name="exit", result={"event": "EXIT", "side": "DOWN"})
        strat_close = MockStrategy(name="close", result={"event": "CLOSE"})

        hub.register(strat_entry)
        hub.register(strat_exit)
        hub.register(strat_close)

        result = asyncio.run(
            hub.process_tick(
                fast_price=1.0,
                poly_orderbook={},
                price_history=[],
                lstm_forecast=0.0,
            )
        )

        assert result is not None
        assert result["event"] == "ENTRY"

        # Remove ENTRY, EXIT and CLOSE should compete
        hub2 = StrategyHub()
        hub2.enable_parallel(True)
        hub2._strategy_timeout_sec = None
        hub2._use_gather = True
        # Create new instances for hub2 (strategies can only be registered once)
        strat_exit2 = MockStrategy(name="exit", result={"event": "EXIT", "side": "DOWN"})
        strat_close2 = MockStrategy(name="close", result={"event": "CLOSE"})
        hub2.register(strat_exit2)
        hub2.register(strat_close2)

        result2 = asyncio.run(
            hub2.process_tick(
                fast_price=1.0,
                poly_orderbook={},
                price_history=[],
                lstm_forecast=0.0,
            )
        )

        # Both EXIT and CLOSE have same priority, first one wins (stable sort preserves registration order)
        assert result2 is not None
        assert result2["event"] == "EXIT"
        assert result2["strategy"] == "exit"
