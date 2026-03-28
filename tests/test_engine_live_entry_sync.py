"""Tests for live OPEN clock sync on HFTEngine."""

from __future__ import annotations

import pytest

from core.engine import HFTEngine
from core.executor import PnLTracker


def _minimal_book() -> dict:
    """Return a valid binary orderbook dict for mid extraction."""
    return {
        "ask": 0.55,
        "bid": 0.45,
        "down_bid": 0.42,
        "down_ask": 0.52,
        "btc_oracle": 100_000.0,
    }


class TestLiveEntrySync:
    """Deferred entry_time and baselines after suppressed log_trade."""

    def test_apply_live_entry_after_fill_sets_clock_and_clears_pending(self):
        """apply_live_entry_after_fill should set entry_time and clear pending flag."""
        pnl = PnLTracker(initial_balance=100.0, live_mode=True)
        eng = HFTEngine(pnl, is_test_mode=True)
        eng._live_entry_sync_pending = True
        eng.entry_context = {"strategy_name": "latency_arbitrage"}
        pnl.live_open("BUY_UP", 5.0, 0.5, 2.5)
        book = _minimal_book()
        eng.apply_live_entry_after_fill(
            book,
            fast_price=100_100.0,
            book_px=0.55,
            exec_px=0.51,
            shares_filled=5.0,
            cost_usd=2.55,
        )
        assert eng._live_entry_sync_pending is False
        assert eng.entry_time > 0.0
        assert eng.last_trade_time == eng.entry_time
        assert eng.entry_poly_mid == pytest.approx(100_000.0)
        assert eng.entry_outcome_mid > 0.0
        assert eng.entry_context.get("entry_exec_px") == pytest.approx(0.51)

    def test_rollback_live_open_signal_clears_pending(self):
        """rollback_live_open_signal should clear deferred OPEN state."""
        pnl = PnLTracker(initial_balance=100.0, live_mode=True)
        eng = HFTEngine(pnl, is_test_mode=True)
        eng._live_entry_sync_pending = True
        eng.entry_context = {"entry_edge": 1.0}
        eng.rollback_live_open_signal()
        assert eng._live_entry_sync_pending is False
        assert eng.entry_time == 0.0
        assert eng.entry_context == {}

    def test_apply_is_noop_without_pending(self):
        """apply_live_entry_after_fill should not mutate state when not pending."""
        pnl = PnLTracker(initial_balance=100.0, live_mode=True)
        eng = HFTEngine(pnl, is_test_mode=True)
        eng._live_entry_sync_pending = False
        eng.entry_time = 0.0
        pnl.live_open("BUY_UP", 5.0, 0.5, 2.5)
        eng.apply_live_entry_after_fill(
            _minimal_book(),
            fast_price=100_100.0,
            book_px=0.55,
            exec_px=0.51,
            shares_filled=5.0,
            cost_usd=2.55,
        )
        assert eng.entry_time == 0.0
