"""Tests for LiveExecutionEngine in core/live_engine.py.

Covers:
- TrackedOrder properties: remaining, is_stale.
- _poll_order: full fill, partial fill accumulation, BUY partial < min_shares exit,
  SELL partial < min_shares FAK exit, reprice, emergency exit path.
- close_position: sub-minimum FAK path, GTC fallback to FAK on failure.
- execute(): skip on bad ask, bad spread, unsupported signal, insufficient budget,
  immediate fill return, awaits poll on non-immediate.
- _place_fak_sell in test_mode: returns (size, 0.50).
- get_open_orders: returns [] in test_mode.
"""

import asyncio
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.live_engine import (
    BUY,
    SELL_SIDE,
    LiveExecutionEngine,
    OrderStatus,
    TrackedOrder,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TOKEN = "tok_abc123"
POLY_MIN = 5.0


def make_engine(monkeypatch) -> LiveExecutionEngine:
    """Return a LiveExecutionEngine in test_mode (no real CLOB connection)."""
    monkeypatch.setenv("POLY_CLOB_MIN_SHARES", str(POLY_MIN))
    monkeypatch.setenv("LIVE_ORDER_FILL_POLL_SEC", "0.01")
    monkeypatch.setenv("LIVE_ORDER_STALE_SEC", "0.05")
    monkeypatch.setenv("LIVE_ORDER_MAX_REPRICE", "2")
    return LiveExecutionEngine(
        private_key=None,
        funder=None,
        test_mode=True,
        min_order_size=4.0,
        max_spread=0.10,
    )


def make_order(
    side=BUY,
    size=8.0,
    filled=0.0,
    status=OrderStatus.PENDING,
    age_offset=0.0,
) -> TrackedOrder:
    """Return a TrackedOrder with configurable state."""
    o = TrackedOrder(
        order_id="ord-1",
        token_id=TOKEN,
        side=side,
        price=0.50,
        size=size,
    )
    o.filled_size = filled
    o.status = status
    if age_offset > 0:
        o.placed_at = time.time() - age_offset
    return o


# ---------------------------------------------------------------------------
# TrackedOrder unit tests
# ---------------------------------------------------------------------------


class TestTrackedOrder:
    """TrackedOrder property logic."""

    def test_remaining_is_size_minus_filled(self):
        """remaining = size - filled_size."""
        o = make_order(size=8.0, filled=3.0)
        assert o.remaining == pytest.approx(5.0)

    def test_remaining_never_negative(self):
        """remaining is clamped to 0 even if filled_size > size."""
        o = make_order(size=8.0, filled=10.0)
        assert o.remaining == pytest.approx(0.0)

    def test_is_stale_false_when_fresh(self, monkeypatch):
        """is_stale is False when the order was placed recently."""
        monkeypatch.setenv("LIVE_ORDER_STALE_SEC", "60")
        o = make_order()
        assert not o.is_stale

    def test_is_stale_true_for_pending_after_timeout(self, monkeypatch):
        """is_stale is True for PENDING orders past the stale window."""
        monkeypatch.setenv("LIVE_ORDER_STALE_SEC", "0.0")
        o = make_order(status=OrderStatus.PENDING, age_offset=1.0)
        assert o.is_stale

    def test_is_stale_true_for_partial_after_timeout(self, monkeypatch):
        """is_stale also applies to PARTIAL status (not just PENDING)."""
        monkeypatch.setenv("LIVE_ORDER_STALE_SEC", "0.0")
        o = make_order(status=OrderStatus.PARTIAL, age_offset=1.0)
        assert o.is_stale

    def test_is_stale_false_for_filled(self, monkeypatch):
        """FILLED orders are never stale regardless of age."""
        monkeypatch.setenv("LIVE_ORDER_STALE_SEC", "0.0")
        o = make_order(status=OrderStatus.FILLED, age_offset=100.0)
        assert not o.is_stale


# ---------------------------------------------------------------------------
# LiveExecutionEngine — test_mode basics
# ---------------------------------------------------------------------------


class TestEngineTestMode:
    """Basic test_mode behaviour: no real CLOB calls."""

    def test_engine_initialises_in_test_mode(self, monkeypatch):
        """Engine should be constructable in test_mode without credentials."""
        eng = make_engine(monkeypatch)
        assert eng.test_mode is True
        assert eng.client is None

    def test_place_fak_sell_returns_size_in_test_mode(self, monkeypatch):
        """_place_fak_sell returns (size, 0.50) simulation values."""
        eng = make_engine(monkeypatch)
        filled, price = eng._place_fak_sell(TOKEN, 3.0)
        assert filled == pytest.approx(3.0)
        assert price == pytest.approx(0.50)

    def test_get_open_orders_returns_empty_in_test_mode(self, monkeypatch):
        """get_open_orders returns [] when in test_mode."""
        eng = make_engine(monkeypatch)
        assert eng.get_open_orders(TOKEN) == []

    def test_cancel_order_returns_true_in_test_mode(self, monkeypatch):
        """_cancel_order is a no-op in test_mode and returns True."""
        eng = make_engine(monkeypatch)
        assert eng._cancel_order("some-id") is True


# ---------------------------------------------------------------------------
# _poll_order — full fill
# ---------------------------------------------------------------------------


class TestPollOrderFullFill:
    """_poll_order terminates correctly on a full fill response."""

    @pytest.mark.asyncio
    async def test_full_fill_sets_status_filled(self, monkeypatch):
        """Poll should mark order FILLED and set filled_size = size."""
        eng = make_engine(monkeypatch)
        order = make_order(size=8.0)
        eng._active_orders[order.order_id] = order

        with patch.object(eng, "_get_order_fill", return_value=("matched", 8.0)):
            await eng._poll_order(order)

        assert order.status == OrderStatus.FILLED
        assert order.filled_size == pytest.approx(8.0)

    @pytest.mark.asyncio
    async def test_full_fill_removes_from_active_orders(self, monkeypatch):
        """After full fill the order should be removed from _active_orders."""
        eng = make_engine(monkeypatch)
        order = make_order(size=8.0)
        eng._active_orders[order.order_id] = order

        with patch.object(eng, "_get_order_fill", return_value=("filled", 8.0)):
            await eng._poll_order(order)

        assert order.order_id not in eng._active_orders


# ---------------------------------------------------------------------------
# _poll_order — partial fill accumulation
# ---------------------------------------------------------------------------


class TestPollOrderPartialFill:
    """_poll_order accumulates partial fills across poll cycles."""

    @pytest.mark.asyncio
    async def test_partial_then_full_accumulates_correctly(self, monkeypatch):
        """Two polls: partial then matched → filled_size equals full size."""
        monkeypatch.setenv("LIVE_ORDER_STALE_SEC", "60")
        eng = make_engine(monkeypatch)
        order = make_order(size=8.0)
        eng._active_orders[order.order_id] = order

        responses = iter([("partially_matched", 3.0), ("matched", 8.0)])
        with patch.object(eng, "_get_order_fill", side_effect=lambda _: next(responses)):
            await eng._poll_order(order)

        assert order.status == OrderStatus.FILLED
        assert order.filled_size == pytest.approx(8.0)

    @pytest.mark.asyncio
    async def test_partial_fill_resets_stale_timer(self, monkeypatch):
        """A new partial fill update should reset placed_at."""
        monkeypatch.setenv("LIVE_ORDER_STALE_SEC", "60")
        eng = make_engine(monkeypatch)
        order = make_order(size=8.0)
        order.placed_at = time.time() - 50
        eng._active_orders[order.order_id] = order

        responses = iter([("partially_matched", 3.0), ("matched", 8.0)])
        with patch.object(eng, "_get_order_fill", side_effect=lambda _: next(responses)):
            await eng._poll_order(order)

        assert order.placed_at > time.time() - 5


# ---------------------------------------------------------------------------
# _poll_order — BUY stale with partial < min_shares
# ---------------------------------------------------------------------------


class TestPollOrderBuySubMinPartial:
    """When a BUY goes stale with filled_size < POLY_CLOB_MIN_SHARES the engine
    must cancel the BUY, FAK-sell the already-filled shares, and report zero fill."""

    @pytest.mark.asyncio
    async def test_buy_partial_below_min_triggers_fak_exit(self, monkeypatch):
        """Stale BUY with 3/8 filled (< 5 min) must cancel + FAK SELL."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        monkeypatch.setenv("LIVE_ORDER_STALE_SEC", "0.0")
        eng = make_engine(monkeypatch)

        order = make_order(size=8.0, filled=3.0, status=OrderStatus.PARTIAL, age_offset=1.0)
        eng._active_orders[order.order_id] = order

        fak_called_with: list[float] = []

        async def fake_fak_sell(token_id, size):
            """Record call args and simulate success."""
            fak_called_with.append(size)
            return size

        with patch.object(eng, "_get_order_fill", return_value=("partially_matched", 3.0)):
            with patch.object(eng, "_fak_sell", side_effect=fake_fak_sell):
                await eng._poll_order(order)

        assert len(fak_called_with) == 1
        assert fak_called_with[0] == pytest.approx(3.0)
        assert order.filled_size == pytest.approx(0.0)
        assert order.status == OrderStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_buy_partial_at_min_does_not_trigger_fak(self, monkeypatch):
        """BUY partial exactly at min_shares should NOT trigger the sub-min FAK exit."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        monkeypatch.setenv("LIVE_ORDER_STALE_SEC", "0.0")
        monkeypatch.setenv("LIVE_ORDER_MAX_REPRICE", "0")
        eng = make_engine(monkeypatch)

        order = make_order(size=8.0, filled=5.0, status=OrderStatus.PARTIAL, age_offset=1.0)
        eng._active_orders[order.order_id] = order

        fak_called = []

        async def fake_fak_sell(token_id, size):
            fak_called.append(size)
            return size

        emergency_called = []

        async def fake_emergency(tracked):
            emergency_called.append(tracked)

        with patch.object(eng, "_get_order_fill", return_value=("partially_matched", 5.0)):
            with patch.object(eng, "_fak_sell", side_effect=fake_fak_sell):
                with patch.object(eng, "_emergency_exit_order", side_effect=fake_emergency):
                    await eng._poll_order(order)

        # The sub-min FAK-exit branch should NOT have triggered.
        assert len(fak_called) == 0
        # Emergency exit should have been called (max_reprice=0).
        assert len(emergency_called) == 1


# ---------------------------------------------------------------------------
# _poll_order — SELL partial remainder < min_shares
# ---------------------------------------------------------------------------


class TestPollOrderSellSubMinRemainder:
    """When SELL has partial fill leaving remainder < min_shares use FAK."""

    @pytest.mark.asyncio
    async def test_sell_sub_min_remainder_uses_fak(self, monkeypatch):
        """SELL with 4 filled / 8 total → 4 remaining < 5 min → FAK exit."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        monkeypatch.setenv("LIVE_ORDER_STALE_SEC", "0.0")
        eng = make_engine(monkeypatch)

        order = make_order(side=SELL_SIDE, size=8.0, filled=4.0,
                           status=OrderStatus.PARTIAL, age_offset=1.0)
        eng._active_orders[order.order_id] = order

        fak_called_with: list[float] = []

        async def fake_fak_sell(token_id, size):
            fak_called_with.append(size)
            return size

        with patch.object(eng, "_get_order_fill", return_value=("partially_matched", 4.0)):
            with patch.object(eng, "_fak_sell", side_effect=fake_fak_sell):
                await eng._poll_order(order)

        assert len(fak_called_with) == 1
        assert fak_called_with[0] == pytest.approx(4.0)
        assert order.status == OrderStatus.FILLED
        assert order.filled_size == pytest.approx(8.0)


# ---------------------------------------------------------------------------
# _poll_order — reprice
# ---------------------------------------------------------------------------


class TestPollOrderReprice:
    """Stale order triggers reprice before emergency exit."""

    @pytest.mark.asyncio
    async def test_stale_order_triggers_reprice(self, monkeypatch):
        """First stale event should reprice the order, not emergency-exit."""
        monkeypatch.setenv("LIVE_ORDER_STALE_SEC", "0.0")
        monkeypatch.setenv("LIVE_ORDER_MAX_REPRICE", "2")
        eng = make_engine(monkeypatch)

        order = make_order(size=8.0, age_offset=1.0)
        eng._active_orders[order.order_id] = order

        reprice_calls: list = []
        original_place = eng._place_limit_raw

        def fake_place(token_id, side, price, size):
            reprice_calls.append(price)
            return "new-id", True

        with patch.object(eng, "_get_order_fill", return_value=("live", 0.0)):
            with patch.object(eng, "get_best_prices", return_value=(0.45, 0.55)):
                with patch.object(eng, "_place_limit_raw", side_effect=fake_place):
                    await eng._poll_order(order)

        assert len(reprice_calls) >= 1

    @pytest.mark.asyncio
    async def test_reprice_preserves_accumulated_filled_size(self, monkeypatch):
        """filled_size from before reprice must be preserved after reprice."""
        monkeypatch.setenv("LIVE_ORDER_STALE_SEC", "0.0")
        monkeypatch.setenv("LIVE_ORDER_MAX_REPRICE", "2")
        eng = make_engine(monkeypatch)

        order = make_order(size=8.0, filled=3.0, status=OrderStatus.PARTIAL, age_offset=1.0)
        eng._active_orders[order.order_id] = order

        def fake_place(token_id, side, price, size):
            return "new-id", True

        with patch.object(eng, "_get_order_fill", return_value=("partially_matched", 3.0)):
            with patch.object(eng, "get_best_prices", return_value=(0.45, 0.55)):
                with patch.object(eng, "_place_limit_raw", side_effect=fake_place):
                    await eng._poll_order(order)

        assert order.filled_size >= pytest.approx(3.0)


# ---------------------------------------------------------------------------
# close_position
# ---------------------------------------------------------------------------


class TestClosePosition:
    """close_position routing logic."""

    @pytest.mark.asyncio
    async def test_sub_min_size_uses_fak(self, monkeypatch):
        """close_position with size < POLY_CLOB_MIN_SHARES must use FAK."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)

        fak_called_with: list = []

        def fake_fak_sell(token_id, size):
            fak_called_with.append(size)
            return (size, 0.45)

        with patch.object(eng, "_place_fak_sell", side_effect=fake_fak_sell):
            filled, price = await eng.close_position(TOKEN, 3.5)

        assert len(fak_called_with) == 1
        assert fak_called_with[0] == pytest.approx(3.5)
        assert filled == pytest.approx(3.5)

    @pytest.mark.asyncio
    async def test_above_min_size_uses_gtc(self, monkeypatch):
        """close_position with size >= min uses GTC limit order."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)

        gtc_calls: list = []
        original_place = eng._place_limit_raw

        def fake_place(token_id, side, price, size):
            gtc_calls.append((side, size))
            return "sell-id", True

        with patch.object(eng, "get_best_prices", return_value=(0.60, 0.65)):
            with patch.object(eng, "_place_limit_raw", side_effect=fake_place):
                filled, price = await eng.close_position(TOKEN, 6.0)

        assert len(gtc_calls) == 1
        assert gtc_calls[0][0] == SELL_SIDE

    @pytest.mark.asyncio
    async def test_gtc_failure_falls_back_to_fak(self, monkeypatch):
        """If GTC placement fails, close_position should try FAK."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)

        fak_called: list = []

        def fake_fak_sell(token_id, size):
            fak_called.append(size)
            return (size, 0.48)

        with patch.object(eng, "get_best_prices", return_value=(0.60, 0.65)):
            with patch.object(eng, "_place_limit_raw", return_value=(None, False)):
                with patch.object(eng, "_place_fak_sell", side_effect=fake_fak_sell):
                    filled, price = await eng.close_position(TOKEN, 6.0)

        assert len(fak_called) == 1
        assert filled == pytest.approx(6.0)

    @pytest.mark.asyncio
    async def test_zero_size_returns_zero(self, monkeypatch):
        """close_position(size=0) must return (0.0, 0.0) immediately."""
        eng = make_engine(monkeypatch)
        filled, price = await eng.close_position(TOKEN, 0.0)
        assert filled == pytest.approx(0.0)
        assert price == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# execute() — skip conditions
# ---------------------------------------------------------------------------


class TestExecuteSkips:
    """execute() must return (0.0, 0.0) on various skip conditions."""

    @pytest.mark.asyncio
    async def test_skip_ask_too_low(self, monkeypatch):
        """best_ask below min_entry_ask must skip."""
        monkeypatch.setenv("HFT_MIN_ENTRY_ASK", "0.08")
        eng = make_engine(monkeypatch)
        with patch.object(eng, "get_best_prices", return_value=(0.01, 0.02)):
            result = await eng.execute("BUY_DOWN", TOKEN, budget_usd=10.0)
        assert result == (0.0, 0.0)

    @pytest.mark.asyncio
    async def test_skip_ask_too_high(self, monkeypatch):
        """best_ask above max_entry_ask must skip."""
        monkeypatch.setenv("HFT_MAX_ENTRY_ASK", "0.99")
        eng = make_engine(monkeypatch)
        with patch.object(eng, "get_best_prices", return_value=(0.98, 0.995)):
            result = await eng.execute("BUY_DOWN", TOKEN, budget_usd=10.0)
        assert result == (0.0, 0.0)

    @pytest.mark.asyncio
    async def test_skip_bad_spread(self, monkeypatch):
        """Spread exceeding max_spread must skip."""
        eng = LiveExecutionEngine(
            private_key=None, funder=None, test_mode=True,
            min_order_size=4.0, max_spread=0.05,
        )
        with patch.object(eng, "get_best_prices", return_value=(0.30, 0.60)):
            result = await eng.execute("BUY_DOWN", TOKEN, budget_usd=10.0)
        assert result == (0.0, 0.0)

    @pytest.mark.asyncio
    async def test_skip_unsupported_signal(self, monkeypatch):
        """A signal other than BUY_UP/BUY_DOWN must skip."""
        eng = make_engine(monkeypatch)
        with patch.object(eng, "get_best_prices", return_value=(0.48, 0.52)):
            result = await eng.execute("SELL", TOKEN, budget_usd=10.0)
        assert result == (0.0, 0.0)

    @pytest.mark.asyncio
    async def test_skip_insufficient_budget_for_min_shares(self, monkeypatch):
        """Budget that converts to fewer shares than POLY_CLOB_MIN_SHARES must skip."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)
        # At ask=0.90, need 5*0.90=4.50 USD; budget=1.0 gives ~1.1 shares → skip.
        with patch.object(eng, "get_best_prices", return_value=(0.85, 0.90)):
            result = await eng.execute("BUY_DOWN", TOKEN, budget_usd=1.0)
        assert result == (0.0, 0.0)


class TestExecuteSuccess:
    """execute() success paths."""

    @pytest.mark.asyncio
    async def test_immediate_fill_returns_shares_and_price(self, monkeypatch):
        """execute() with immediate fill must return (shares, price) > 0."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)

        def fake_place(token_id, side, price, size):
            return "order-1", True

        with patch.object(eng, "get_best_prices", return_value=(0.48, 0.52)):
            with patch.object(eng, "_place_limit_raw", side_effect=fake_place):
                filled, avg_price = await eng.execute("BUY_DOWN", TOKEN, budget_usd=10.0)

        assert filled > 0
        assert avg_price > 0

    @pytest.mark.asyncio
    async def test_non_immediate_awaits_poll(self, monkeypatch):
        """execute() must await _poll_order when immediate=False and report fill."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)

        def fake_place(token_id, side, price, size):
            return "order-2", False

        poll_awaited = []

        async def fake_poll(tracked):
            tracked.status = OrderStatus.FILLED
            tracked.filled_size = tracked.size
            poll_awaited.append(True)

        with patch.object(eng, "get_best_prices", return_value=(0.48, 0.52)):
            with patch.object(eng, "_place_limit_raw", side_effect=fake_place):
                with patch.object(eng, "_poll_order", side_effect=fake_poll):
                    filled, avg_price = await eng.execute("BUY_UP", TOKEN, budget_usd=10.0)

        assert len(poll_awaited) == 1
        assert filled > 0

    @pytest.mark.asyncio
    async def test_placement_failure_returns_skip(self, monkeypatch):
        """execute() returns (0.0, 0.0) when order placement fails."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)

        with patch.object(eng, "get_best_prices", return_value=(0.48, 0.52)):
            with patch.object(eng, "_place_limit_raw", return_value=(None, False)):
                result = await eng.execute("BUY_DOWN", TOKEN, budget_usd=10.0)

        assert result == (0.0, 0.0)
