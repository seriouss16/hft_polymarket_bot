"""Tests for PnLTracker in core/executor.py.

Covers:
- SIM mode: log_trade BUY/SELL, balance accounting, PnL, WR, drawdown.
- LIVE mode: _suppress_buy flag, live_open(), live_close(), rollback_last_open().
- Mixed-side protection, zero-balance halt, regime cooldown.
"""

import time

import pytest

from core.executor import PnLTracker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_tracker(balance=100.0, live=False):
    """Return a fresh PnLTracker with the given initial balance."""
    return PnLTracker(initial_balance=balance, live_mode=live)


# ---------------------------------------------------------------------------
# SIM mode — log_trade BUY
# ---------------------------------------------------------------------------


class TestSimBuy:
    """SIM mode BUY via log_trade."""

    def test_buy_reduces_balance_and_sets_inventory(self):
        """BUY should subtract notional from balance and open inventory."""
        t = make_tracker(100.0)
        t.log_trade("BUY", 0.50, amount_usd=10.0)
        assert t.balance == pytest.approx(90.0)
        assert t.inventory > 0

    def test_buy_exec_price_includes_fee(self):
        """Executed price should be book_px * (1 + fee_rate)."""
        t = make_tracker(100.0)
        t.log_trade("BUY", 0.50, amount_usd=10.0)
        expected_exec = 0.50 * (1 + t.fee_rate)
        assert t.entry_price == pytest.approx(expected_exec)

    def test_buy_returns_open_event(self):
        """log_trade(BUY) should return an OPEN dict."""
        t = make_tracker(100.0)
        result = t.log_trade("BUY", 0.50, amount_usd=10.0)
        assert isinstance(result, dict)
        assert result["event"] == "OPEN"

    def test_buy_up_and_down_variants(self):
        """BUY_UP and BUY_DOWN both record an UP/DOWN position side."""
        t = make_tracker(100.0)
        t.log_trade("BUY_DOWN", 0.50, amount_usd=10.0)
        assert t.position_side == "DOWN"

        t2 = make_tracker(100.0)
        t2.log_trade("BUY_UP", 0.50, amount_usd=10.0)
        assert t2.position_side == "UP"

    def test_buy_blocked_when_insufficient_balance(self):
        """log_trade(BUY) should return None when balance < notional."""
        t = make_tracker(5.0)
        result = t.log_trade("BUY", 0.50, amount_usd=10.0)
        assert result is None
        assert t.inventory == 0.0

    def test_buy_blocked_when_zero_balance(self):
        """log_trade(BUY) should return None and not crash at zero balance."""
        t = make_tracker(0.0)
        result = t.log_trade("BUY", 0.50, amount_usd=10.0)
        assert result is None

    def test_mixed_side_add_blocked(self):
        """Adding a position on the opposite side of an open position must be blocked."""
        t = make_tracker(100.0)
        t.log_trade("BUY_UP", 0.50, amount_usd=10.0)
        result = t.log_trade("BUY_DOWN", 0.50, amount_usd=10.0)
        assert result is None
        assert t.position_side == "UP"

    def test_add_to_same_side_averages_price(self):
        """Adding to an existing same-side position should average entry price."""
        t = make_tracker(100.0)
        t.log_trade("BUY_UP", 0.40, amount_usd=10.0)
        first_price = t.entry_price
        t.log_trade("BUY_UP", 0.60, amount_usd=10.0)
        assert t.entry_price != first_price
        assert t.inventory > 0


# ---------------------------------------------------------------------------
# SIM mode — log_trade SELL
# ---------------------------------------------------------------------------


class TestSimSell:
    """SIM mode SELL via log_trade."""

    def test_sell_realizes_profit(self):
        """Selling at a price above entry should yield positive PnL."""
        t = make_tracker(100.0)
        t.log_trade("BUY", 0.40, amount_usd=10.0)
        t.log_trade("SELL", 0.70)
        assert t.total_pnl > 0

    def test_sell_realizes_loss(self):
        """Selling below entry should yield negative PnL."""
        t = make_tracker(100.0)
        t.log_trade("BUY", 0.60, amount_usd=10.0)
        t.log_trade("SELL", 0.30)
        assert t.total_pnl < 0

    def test_sell_increments_trade_count(self):
        """Each complete round-trip should increment trades_count by 1."""
        t = make_tracker(100.0)
        t.log_trade("BUY", 0.50, amount_usd=10.0)
        t.log_trade("SELL", 0.60)
        assert t.trades_count == 1

    def test_sell_increments_wins_on_profit(self):
        """A profitable SELL should increment the wins counter."""
        t = make_tracker(100.0)
        t.log_trade("BUY", 0.40, amount_usd=10.0)
        t.log_trade("SELL", 0.80)
        assert t.wins == 1

    def test_sell_clears_inventory(self):
        """After a full SELL the inventory should be zero."""
        t = make_tracker(100.0)
        t.log_trade("BUY", 0.50, amount_usd=10.0)
        t.log_trade("SELL", 0.60)
        assert t.inventory == pytest.approx(0.0)

    def test_sell_without_position_is_noop(self):
        """SELL with no open position should return None silently."""
        t = make_tracker(100.0)
        result = t.log_trade("SELL", 0.60)
        assert result is None

    def test_drawdown_tracked_after_loss(self):
        """max_drawdown should increase after a losing trade."""
        t = make_tracker(100.0)
        t.log_trade("BUY", 0.80, amount_usd=10.0)
        t.log_trade("SELL", 0.30)
        assert t.max_drawdown > 0


# ---------------------------------------------------------------------------
# LIVE mode — _suppress_buy
# ---------------------------------------------------------------------------


class TestLiveModeSuppression:
    """In live mode log_trade BUY/SELL must be suppressed."""

    def test_buy_suppressed_in_live_mode(self):
        """log_trade(BUY) in live mode returns a suppressed dict with paper-equivalent fields."""
        t = make_tracker(100.0, live=True)
        result = t.log_trade("BUY", 0.50, amount_usd=10.0)
        assert isinstance(result, dict)
        assert result.get("suppressed") is True
        assert result.get("book_px") == pytest.approx(0.50)
        assert result.get("amount_usd") == pytest.approx(10.0)
        assert result.get("exec_px") == pytest.approx(0.50 * (1.0 + t.fee_rate))
        assert t.inventory == 0.0
        assert t.balance == pytest.approx(100.0)

    def test_buy_suppressed_none_when_insufficient_balance(self):
        """Live suppressed path must match SIM: skip entry when balance < notional."""
        t = make_tracker(5.0, live=True)
        assert t.log_trade("BUY", 0.50, amount_usd=10.0) is None

    def test_sell_suppressed_in_live_mode(self):
        """log_trade(SELL) in live mode also returns a suppressed sentinel."""
        t = make_tracker(100.0, live=True)
        result = t.log_trade("SELL", 0.60)
        assert isinstance(result, dict)
        assert result.get("suppressed") is True

    def test_buy_up_down_suppressed_in_live_mode(self):
        """BUY_UP and BUY_DOWN variants are both suppressed in live mode."""
        for side in ("BUY_UP", "BUY_DOWN"):
            t = make_tracker(100.0, live=True)
            result = t.log_trade(side, 0.50, amount_usd=10.0)
            assert result.get("suppressed") is True, f"Expected suppression for {side}"


# ---------------------------------------------------------------------------
# live_open()
# ---------------------------------------------------------------------------


class TestLiveOpen:
    """live_open() records confirmed CLOB BUY fills."""

    def test_live_open_sets_inventory(self):
        """live_open must set inventory to filled_shares."""
        t = make_tracker(100.0, live=True)
        t.live_open("BUY_DOWN", filled_shares=8.0, avg_price=0.50, amount_usd=4.0)
        assert t.inventory == pytest.approx(8.0)
        assert t.entry_price == pytest.approx(0.50)
        assert t.position_side == "DOWN"

    def test_live_open_reduces_balance_by_amount_usd(self):
        """live_open must deduct amount_usd from balance."""
        t = make_tracker(100.0, live=True)
        t.live_open("BUY_UP", filled_shares=6.0, avg_price=0.60, amount_usd=3.6)
        assert t.balance == pytest.approx(96.4)

    def test_live_open_zero_shares_is_noop(self):
        """live_open with zero filled_shares must not alter state."""
        t = make_tracker(100.0, live=True)
        t.live_open("BUY_UP", filled_shares=0.0, avg_price=0.50, amount_usd=5.0)
        assert t.inventory == 0.0
        assert t.balance == pytest.approx(100.0)

    def test_live_open_add_same_side_averages(self):
        """Adding to an existing live position should average entry price."""
        t = make_tracker(100.0, live=True)
        t.live_open("BUY_UP", filled_shares=5.0, avg_price=0.40, amount_usd=2.0)
        t.live_open("BUY_UP", filled_shares=5.0, avg_price=0.60, amount_usd=3.0)
        assert t.inventory == pytest.approx(10.0)
        assert t.entry_price == pytest.approx(0.50)

    def test_live_open_cash_budget_caps_recorded_cost(self):
        """When amount_usd is below CLOB notional (budget cap), entry uses cash/sh."""
        t = make_tracker(100.0, live=True)
        t.live_open(
            "BUY_DOWN",
            filled_shares=6.7692,
            avg_price=0.6362,
            amount_usd=4.0,
        )
        assert t._buy_cost_usd == pytest.approx(4.0)
        assert t.balance == pytest.approx(96.0)
        assert t.entry_price == pytest.approx(4.0 / 6.7692)

    def test_live_open_blocks_mixed_side(self):
        """live_open must block adding on the opposite side."""
        t = make_tracker(100.0, live=True)
        t.live_open("BUY_UP", filled_shares=5.0, avg_price=0.50, amount_usd=2.5)
        before = t.inventory
        t.live_open("BUY_DOWN", filled_shares=5.0, avg_price=0.50, amount_usd=2.5)
        assert t.inventory == pytest.approx(before)


# ---------------------------------------------------------------------------
# live_close()
# ---------------------------------------------------------------------------


class TestLiveClose:
    """live_close() records confirmed CLOB SELL fills."""

    def test_live_close_profit(self):
        """Selling above avg_price should return positive PnL."""
        t = make_tracker(100.0, live=True)
        t.live_open("BUY_UP", filled_shares=8.0, avg_price=0.50, amount_usd=4.0)
        pnl = t.live_close(filled_shares=8.0, avg_price=0.70)
        assert pnl > 0
        assert t.inventory == pytest.approx(0.0)
        assert t.trades_count == 1
        assert t.wins == 1

    def test_live_close_loss(self):
        """Selling below avg_price should return negative PnL."""
        t = make_tracker(100.0, live=True)
        t.live_open("BUY_UP", filled_shares=8.0, avg_price=0.60, amount_usd=4.8)
        pnl = t.live_close(filled_shares=8.0, avg_price=0.30)
        assert pnl < 0
        assert t.wins == 0

    def test_live_close_partial_leave_remaining_inventory(self):
        """Partial close should reduce inventory proportionally."""
        t = make_tracker(100.0, live=True)
        t.live_open("BUY_UP", filled_shares=10.0, avg_price=0.50, amount_usd=5.0)
        t.live_close(filled_shares=4.0, avg_price=0.55)
        assert t.inventory == pytest.approx(6.0)

    def test_live_close_no_position_returns_zero(self):
        """live_close with no open position should return 0.0."""
        t = make_tracker(100.0, live=True)
        pnl = t.live_close(filled_shares=5.0, avg_price=0.60)
        assert pnl == pytest.approx(0.0)

    def test_live_close_balance_increases_by_proceeds(self):
        """Balance after close should increase by filled_shares * avg_price."""
        t = make_tracker(100.0, live=True)
        t.live_open("BUY_UP", filled_shares=8.0, avg_price=0.50, amount_usd=4.0)
        bal_before = t.balance
        t.live_close(filled_shares=8.0, avg_price=0.60)
        assert t.balance == pytest.approx(bal_before + 8.0 * 0.60)

    def test_live_close_performance_key_recorded(self):
        """live_close with a performance_key should record to strategy_performance."""
        t = make_tracker(100.0, live=True)
        t.live_open("BUY_UP", filled_shares=6.0, avg_price=0.50, amount_usd=3.0)
        t.live_close(filled_shares=6.0, avg_price=0.70, performance_key="latency:latency")
        slices = t.strategy_performance.slices
        assert "latency:latency" in slices


# ---------------------------------------------------------------------------
# rollback_last_open()
# ---------------------------------------------------------------------------


class TestRollbackLastOpen:
    """rollback_last_open() must undo a sim BUY that was rejected by the CLOB."""

    def test_rollback_restores_balance(self):
        """After rollback, balance should be restored by amount_usd."""
        t = make_tracker(100.0)
        t.log_trade("BUY", 0.50, amount_usd=10.0)
        assert t.balance == pytest.approx(90.0)
        t.rollback_last_open(10.0)
        assert t.balance == pytest.approx(100.0)

    def test_rollback_clears_inventory(self):
        """After rollback, inventory and entry state must be zeroed."""
        t = make_tracker(100.0)
        t.log_trade("BUY", 0.50, amount_usd=10.0)
        t.rollback_last_open(10.0)
        assert t.inventory == 0.0
        assert t.entry_price == 0.0
        assert t.position_side is None

    def test_rollback_is_noop_without_position(self):
        """rollback with no open position must not alter balance (double-restore guard)."""
        t = make_tracker(100.0)
        t.rollback_last_open(10.0)
        assert t.balance == pytest.approx(100.0)

    def test_rollback_not_called_twice(self):
        """Calling rollback twice must not inflate balance beyond the original."""
        t = make_tracker(100.0)
        t.log_trade("BUY", 0.50, amount_usd=10.0)
        t.rollback_last_open(10.0)
        t.rollback_last_open(10.0)
        assert t.balance == pytest.approx(100.0)
