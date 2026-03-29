"""Tests for LiveExecutionEngine in core/live_engine.py.

Covers:
- TrackedOrder properties: remaining, is_stale.
- _poll_order: full fill, partial fill accumulation, BUY partial < min_shares exit,
  SELL partial < min_shares FAK exit, reprice, emergency exit path.
- close_position: sub-minimum FAK path, GTC fallback to FAK on failure.
- execute(): skip on bad ask, bad spread, unsupported signal, insufficient budget,
  immediate fill return, awaits poll on non-immediate.
- probe_chain_shares_for_close / wait_for_exit_readiness for EXIT desync.
- _place_fak_sell in test_mode: returns (size, 0.50).
- get_open_orders: returns [] in test_mode.
- _recover_fill_after_cancel: test_mode noop, matched/canceled+fill sync.

Note: _ORDER_STALE_SEC and _ORDER_MAX_REPRICE are module-level constants read at
import time.  Tests that need to override them use ``patch`` against
``core.live_engine._ORDER_STALE_SEC`` / ``_ORDER_MAX_REPRICE`` directly.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import pytest

from core.live_engine import (
    BUY,
    SELL_SIDE,
    LiveExecutionEngine,
    LiveRiskManager,
    OrderStatus,
    TrackedOrder,
)

# _ORDER_STALE_SEC is read at module import time from the env default (3.0 s).
# conftest monkeypatching of env vars does NOT retroactively change already-read
# module constants.  We patch the constant itself in tests that need a specific
# stale threshold.  For TrackedOrder property tests we simply use an age that
# exceeds the real default (3.0 s).
_STALE_AGE = 4.0  # seconds — larger than the default _ORDER_STALE_SEC of 3.0 s


TOKEN = "tok_abc123"
POLY_MIN = 5.0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_engine(monkeypatch) -> LiveExecutionEngine:
    """Return a LiveExecutionEngine in test_mode (no real CLOB connection)."""
    monkeypatch.setenv("POLY_CLOB_MIN_SHARES", str(POLY_MIN))
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
    entry_best_ask=None,
) -> TrackedOrder:
    """Return a TrackedOrder with configurable state."""
    o = TrackedOrder(
        order_id="ord-1",
        token_id=TOKEN,
        side=side,
        price=0.50,
        size=size,
        entry_best_ask=entry_best_ask,
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

    def test_is_stale_false_when_fresh(self):
        """is_stale is False when the order was placed just now.

        conftest sets LIVE_ORDER_STALE_SEC=0.05 s; age ~0 is well below that.
        """
        o = make_order()
        assert not o.is_stale

    def test_is_stale_true_for_pending_after_timeout(self):
        """is_stale is True for PENDING orders whose age exceeds _ORDER_STALE_SEC.

        We use age_offset=1.0 s which is well past the 0.05 s threshold set in conftest.
        """
        o = make_order(status=OrderStatus.PENDING, age_offset=_STALE_AGE)
        assert o.is_stale

    def test_is_stale_true_for_partial_after_timeout(self):
        """is_stale also applies to PARTIAL orders past the stale window."""
        o = make_order(status=OrderStatus.PARTIAL, age_offset=_STALE_AGE)
        assert o.is_stale

    def test_is_stale_false_for_filled(self):
        """FILLED orders are never stale regardless of age."""
        o = make_order(status=OrderStatus.FILLED, age_offset=100.0)
        assert not o.is_stale

    def test_is_stale_false_for_cancelled(self):
        """CANCELLED orders are not considered stale."""
        o = make_order(status=OrderStatus.CANCELLED, age_offset=100.0)
        assert not o.is_stale


# ---------------------------------------------------------------------------
# LiveExecutionEngine — test_mode basics
# ---------------------------------------------------------------------------


class TestEngineTestMode:
    """Basic test_mode behaviour: no real CLOB calls."""

    def test_engine_initialises_in_test_mode(self, monkeypatch):
        """Engine should be constructable in test_mode without credentials.

        When py_clob_client is installed a ClobClient object may be created even
        in test_mode (credentials are empty strings).  The key assertion is that
        test_mode is set and no credential-derivation is attempted.
        """
        eng = make_engine(monkeypatch)
        assert eng.test_mode is True
        # _active_orders must start empty.
        assert eng._active_orders == {}

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

    async def test_full_fill_sets_status_filled(self, monkeypatch):
        """Poll should mark order FILLED and set filled_size = size."""
        eng = make_engine(monkeypatch)
        order = make_order(size=8.0)
        eng._active_orders[order.order_id] = order

        with patch.object(eng, "_get_order_fill", return_value=("matched", 8.0)):
            await eng._poll_order(order)

        assert order.status == OrderStatus.FILLED
        assert order.filled_size == pytest.approx(8.0)

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

    async def test_partial_then_full_accumulates_correctly(self, monkeypatch):
        """Two polls: partial then matched → filled_size equals full size."""
        eng = make_engine(monkeypatch)
        order = make_order(size=8.0)
        eng._active_orders[order.order_id] = order

        responses = iter([("partially_matched", 3.0), ("matched", 8.0)])
        # Keep stale threshold high so the order never goes stale mid-test.
        with patch("core.live_engine._ORDER_STALE_SEC", 9999.0):
            with patch.object(eng, "_get_order_fill", side_effect=lambda _: next(responses)):
                await eng._poll_order(order)

        assert order.status == OrderStatus.FILLED
        assert order.filled_size == pytest.approx(8.0)

    async def test_partial_fill_resets_stale_timer(self, monkeypatch):
        """A new partial fill update should reset placed_at to ~now."""
        eng = make_engine(monkeypatch)
        order = make_order(size=8.0)
        order.placed_at = time.time() - 50
        eng._active_orders[order.order_id] = order

        responses = iter([("partially_matched", 3.0), ("matched", 8.0)])
        with patch("core.live_engine._ORDER_STALE_SEC", 9999.0):
            with patch.object(eng, "_get_order_fill", side_effect=lambda _: next(responses)):
                await eng._poll_order(order)

        assert order.placed_at > time.time() - 5


# ---------------------------------------------------------------------------
# _poll_order — BUY stale with partial < min_shares
# ---------------------------------------------------------------------------


class TestPollOrderBuySubMinPartial:
    """When a BUY goes stale with filled_size < POLY_CLOB_MIN_SHARES the engine
    must cancel the BUY, FAK-sell the already-filled shares, and report zero fill."""

    async def test_buy_partial_below_min_triggers_fak_exit(self, monkeypatch):
        """Stale BUY with 3/8 filled (< 5 min) must cancel + FAK SELL."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)

        order = make_order(size=8.0, filled=3.0, status=OrderStatus.PARTIAL,
                           age_offset=_STALE_AGE)
        eng._active_orders[order.order_id] = order

        fak_called_with: list[float] = []

        async def fake_fak_sell(token_id, size):
            """Record call args and simulate success."""
            fak_called_with.append(size)
            return size

        with patch("core.live_engine._ORDER_STALE_SEC", 0.0):
            with patch.object(eng, "_get_order_fill", return_value=("partially_matched", 3.0)):
                with patch.object(eng, "_fak_sell", side_effect=fake_fak_sell):
                    await eng._poll_order(order)

        assert len(fak_called_with) == 1
        assert fak_called_with[0] == pytest.approx(3.0)
        assert order.filled_size == pytest.approx(0.0)
        assert order.status == OrderStatus.CANCELLED

    async def test_buy_partial_at_min_does_not_trigger_sub_min_fak(self, monkeypatch):
        """BUY partial exactly at min_shares should NOT trigger the sub-min FAK exit."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)

        order = make_order(size=8.0, filled=5.0, status=OrderStatus.PARTIAL,
                           age_offset=_STALE_AGE)
        eng._active_orders[order.order_id] = order

        fak_called: list = []

        async def fake_fak_sell(token_id, size):
            fak_called.append(size)
            return size

        emergency_called: list = []

        async def fake_emergency(tracked):
            emergency_called.append(tracked)

        with patch("core.live_engine._ORDER_STALE_SEC", 0.0):
            with patch("core.live_engine._ORDER_MAX_REPRICE", 0):
                with patch.object(eng, "_get_order_fill",
                                  return_value=("partially_matched", 5.0)):
                    with patch.object(eng, "_fak_sell", side_effect=fake_fak_sell):
                        with patch.object(eng, "_emergency_exit_order",
                                          side_effect=fake_emergency):
                            await eng._poll_order(order)

        assert len(fak_called) == 0
        assert len(emergency_called) == 1


# ---------------------------------------------------------------------------
# _poll_order — SELL partial remainder < min_shares
# ---------------------------------------------------------------------------


class TestPollOrderSellSubMinRemainder:
    """When SELL has partial fill leaving remainder < min_shares use FAK."""

    async def test_sell_sub_min_remainder_uses_fak(self, monkeypatch):
        """SELL with 4 filled / 8 total → 4 remaining < 5 min → FAK exit."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)

        order = make_order(side=SELL_SIDE, size=8.0, filled=4.0,
                           status=OrderStatus.PARTIAL, age_offset=_STALE_AGE)
        eng._active_orders[order.order_id] = order

        fak_called_with: list[float] = []

        async def fake_fak_sell(token_id, size):
            fak_called_with.append(size)
            return size

        with patch("core.live_engine._ORDER_STALE_SEC", 0.0):
            with patch.object(eng, "_get_order_fill",
                              return_value=("partially_matched", 4.0)):
                with patch.object(eng, "get_best_prices", return_value=(0.45, 0.55)):
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

    async def test_stale_order_triggers_reprice(self, monkeypatch):
        """First stale event should reprice the order, not emergency-exit."""
        eng = make_engine(monkeypatch)
        order = make_order(size=8.0, age_offset=_STALE_AGE)
        eng._active_orders[order.order_id] = order

        reprice_calls: list = []

        def fake_place(token_id, side, price, size):
            reprice_calls.append(price)
            return "new-id", True

        with patch("core.live_engine._ORDER_STALE_SEC", 0.0):
            with patch("core.live_engine._ORDER_MAX_REPRICE", 2):
                with patch.object(eng, "_get_order_fill", return_value=("live", 0.0)):
                    with patch.object(eng, "get_best_prices", return_value=(0.45, 0.55)):
                        with patch.object(eng, "_place_limit_raw", side_effect=fake_place):
                            await eng._poll_order(order)

        assert len(reprice_calls) >= 1

    async def test_reprice_preserves_accumulated_filled_size(self, monkeypatch):
        """filled_size from before reprice must be preserved after reprice.

        Uses filled=6 (>= POLY_CLOB_MIN_SHARES=5) so the sub-min BUY-exit branch
        is not triggered and the order goes through the normal reprice path.
        """
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)
        # filled=6 >= poly_min=5 → goes to reprice path, not FAK-exit.
        order = make_order(size=10.0, filled=6.0, status=OrderStatus.PARTIAL,
                           age_offset=_STALE_AGE)
        eng._active_orders[order.order_id] = order

        def fake_place(token_id, side, price, size):
            return "new-id", True

        with patch("core.live_engine._ORDER_STALE_SEC", 0.0):
            with patch("core.live_engine._ORDER_MAX_REPRICE", 2):
                with patch.object(eng, "_get_order_fill",
                                  return_value=("partially_matched", 6.0)):
                    with patch.object(eng, "get_best_prices", return_value=(0.45, 0.55)):
                        with patch.object(eng, "_place_limit_raw", side_effect=fake_place):
                            await eng._poll_order(order)

        assert order.filled_size >= 6.0

    async def test_stale_buy_reprice_aborts_when_slippage_exceeded(self, monkeypatch):
        """Stale BUY is cancelled when best ask moved beyond LIVE_MAX_BUY_REPRICE_SLIPPAGE."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        monkeypatch.setenv("LIVE_MAX_BUY_REPRICE_SLIPPAGE", "0.02")
        eng = make_engine(monkeypatch)
        order = make_order(
            size=8.0,
            age_offset=_STALE_AGE,
            entry_best_ask=0.50,
        )
        eng._active_orders[order.order_id] = order
        reprice_prices: list = []

        def fake_place(token_id, side, price, size):
            reprice_prices.append(price)
            return "new-id", True

        with patch("core.live_engine._ORDER_STALE_SEC", 0.0):
            with patch.object(eng, "_get_order_fill", return_value=("live", 0.0)):
                with patch.object(
                    eng,
                    "get_best_prices",
                    return_value=(0.40, 0.53),
                ):
                    with patch.object(eng, "_place_limit_raw", side_effect=fake_place):
                        await eng._poll_order(order)

        assert order.status == OrderStatus.CANCELLED
        assert order.filled_size == pytest.approx(0.0)
        assert reprice_prices == []
        assert eng._last_buy_skip_reason == "slippage_abort"


# ---------------------------------------------------------------------------
# close_position
# ---------------------------------------------------------------------------


class TestClosePosition:
    """close_position routing logic."""

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

    async def test_above_min_size_uses_gtc(self, monkeypatch):
        """close_position with size >= min uses GTC limit order."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)

        gtc_calls: list = []

        def fake_place(token_id, side, price, size):
            gtc_calls.append((side, size))
            return "sell-id", True

        async def fake_poll(tracked):
            """Simulate CLOB confirming the full SELL after placement."""
            tracked.status = OrderStatus.FILLED
            tracked.filled_size = tracked.size
            eng._active_orders.pop(tracked.order_id, None)

        with patch.object(eng, "get_best_prices", return_value=(0.60, 0.65)):
            with patch.object(eng, "_place_limit_raw", side_effect=fake_place):
                with patch.object(eng, "_poll_order", side_effect=fake_poll):
                    filled, price = await eng.close_position(TOKEN, 6.0)

        assert len(gtc_calls) == 1
        assert gtc_calls[0][0] == SELL_SIDE
        assert filled == pytest.approx(6.0)

    async def test_gtc_failure_falls_back_to_fak(self, monkeypatch):
        """If GTC placement fails, close_position should fall back to FAK."""
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

    async def test_zero_size_returns_zero(self, monkeypatch):
        """close_position(size=0) must return (0.0, 0.0) immediately."""
        eng = make_engine(monkeypatch)
        filled, price = await eng.close_position(TOKEN, 0.0)
        assert filled == pytest.approx(0.0)
        assert price == pytest.approx(0.0)

    async def test_close_uses_confirmed_buy_fill_not_caller_size(self, monkeypatch):
        """close_position must place SELL for _confirmed_buys fill when caller differs."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)
        eng._confirmed_buys[TOKEN] = 6.15

        gtc_calls: list = []

        def fake_place(token_id, side, price, size):
            gtc_calls.append(size)
            return "sell-id", True

        async def fake_poll(tracked):
            tracked.status = OrderStatus.FILLED
            tracked.filled_size = tracked.size
            eng._active_orders.pop(tracked.order_id, None)

        with patch.object(eng, "get_best_prices", return_value=(0.60, 0.65)):
            with patch.object(eng, "_place_limit_raw", side_effect=fake_place):
                with patch.object(eng, "_poll_order", side_effect=fake_poll):
                    filled, price = await eng.close_position(TOKEN, 7.4)

        assert len(gtc_calls) == 1
        assert gtc_calls[0] == pytest.approx(6.15)
        assert filled == pytest.approx(6.15)


# ---------------------------------------------------------------------------
# execute() — skip conditions
# ---------------------------------------------------------------------------


class TestExecuteSkips:
    """execute() must return (0.0, 0.0) on various skip conditions."""

    async def test_skip_ask_too_low(self, monkeypatch):
        """best_ask below min_entry_ask must skip."""
        eng = make_engine(monkeypatch)
        with patch.object(eng, "get_best_prices", return_value=(0.01, 0.02)):
            result = await eng.execute("BUY_DOWN", TOKEN, budget_usd=10.0)
        assert result == (0.0, 0.0)

    async def test_skip_ask_too_high(self, monkeypatch):
        """best_ask above max_entry_ask must skip."""
        eng = make_engine(monkeypatch)
        with patch.object(eng, "get_best_prices", return_value=(0.98, 0.995)):
            result = await eng.execute("BUY_DOWN", TOKEN, budget_usd=10.0)
        assert result == (0.0, 0.0)

    async def test_skip_bad_spread(self, monkeypatch):
        """Spread exceeding max_spread must skip."""
        eng = LiveExecutionEngine(
            private_key=None, funder=None, test_mode=True,
            min_order_size=4.0, max_spread=0.05,
        )
        with patch.object(eng, "get_best_prices", return_value=(0.30, 0.60)):
            result = await eng.execute("BUY_DOWN", TOKEN, budget_usd=10.0)
        assert result == (0.0, 0.0)

    async def test_skip_unsupported_signal(self, monkeypatch):
        """A signal other than BUY_UP/BUY_DOWN must skip."""
        eng = make_engine(monkeypatch)
        with patch.object(eng, "get_best_prices", return_value=(0.48, 0.52)):
            result = await eng.execute("SELL", TOKEN, budget_usd=10.0)
        assert result == (0.0, 0.0)

    async def test_skip_insufficient_budget_for_min_shares(self, monkeypatch):
        """Budget that converts to fewer shares than POLY_CLOB_MIN_SHARES must skip."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)
        # At ask=0.90, 1.0 USD gives ~1.1 shares which is < 5 min.
        with patch.object(eng, "get_best_prices", return_value=(0.85, 0.90)):
            result = await eng.execute("BUY_DOWN", TOKEN, budget_usd=1.0)
        assert result == (0.0, 0.0)


# ---------------------------------------------------------------------------
# execute() — success paths
# ---------------------------------------------------------------------------


class TestExecuteSuccess:
    """execute() success paths."""

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

    async def test_cached_top_of_book_skips_get_best_prices(self, monkeypatch):
        """Passing best_bid/best_ask must not call get_best_prices."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)
        http_calls: list = []

        def fake_get_best(token_id):
            http_calls.append(token_id)
            return (0.48, 0.52)

        def fake_place(token_id, side, price, size):
            return "order-cached", True

        with patch.object(eng, "get_best_prices", side_effect=fake_get_best):
            with patch.object(eng, "_place_limit_raw", side_effect=fake_place):
                filled, avg_price = await eng.execute(
                    "BUY_DOWN",
                    TOKEN,
                    budget_usd=10.0,
                    best_bid=0.48,
                    best_ask=0.52,
                )

        assert http_calls == []
        assert filled > 0
        assert avg_price > 0

    async def test_non_immediate_awaits_poll(self, monkeypatch):
        """execute() must await _poll_order when immediate=False and report fill."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)

        def fake_place(token_id, side, price, size):
            return "order-2", False

        poll_awaited: list = []

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

    async def test_placement_failure_returns_skip(self, monkeypatch):
        """execute() returns (0.0, 0.0) when order placement fails."""
        monkeypatch.setenv("POLY_CLOB_MIN_SHARES", "5")
        eng = make_engine(monkeypatch)

        with patch.object(eng, "get_best_prices", return_value=(0.48, 0.52)):
            with patch.object(eng, "_place_limit_raw", return_value=(None, False)):
                result = await eng.execute("BUY_DOWN", TOKEN, budget_usd=10.0)

        assert result == (0.0, 0.0)


# ---------------------------------------------------------------------------
# Exit reconciliation: chain probe and wait_for_exit_readiness
# ---------------------------------------------------------------------------


class TestExitReconcile:
    """probe_chain_shares_for_close and wait_for_exit_readiness."""

    async def test_probe_chain_syncs_confirmed_buys(self, monkeypatch):
        """probe_chain_shares_for_close must set _confirmed_buys when balance exceeds dust."""
        monkeypatch.setenv("LIVE_CLOSE_CHAIN_PROBE_DELAYS_SEC", "0")
        eng = make_engine(monkeypatch)
        eng.test_mode = False

        def fake_bal(token_id):
            return 1.56

        monkeypatch.setattr(eng, "fetch_conditional_balance", fake_bal)
        got = await eng.probe_chain_shares_for_close(TOKEN)
        assert got == pytest.approx(1.56)
        assert eng._confirmed_buys[TOKEN] == pytest.approx(1.56)

    async def test_probe_chain_below_dust_returns_zero(self, monkeypatch):
        """Balances at or below dust must not sync _confirmed_buys."""
        monkeypatch.setenv("LIVE_CHAIN_EXIT_DUST_SHARES", "0.5")
        monkeypatch.setenv("LIVE_CLOSE_CHAIN_PROBE_DELAYS_SEC", "0")
        eng = make_engine(monkeypatch)
        eng.test_mode = False
        monkeypatch.setattr(eng, "fetch_conditional_balance", lambda tid: 0.1)
        got = await eng.probe_chain_shares_for_close(TOKEN)
        assert got == pytest.approx(0.0)
        assert TOKEN not in eng._confirmed_buys

    async def test_wait_for_exit_readiness_noop_in_test_mode(self, monkeypatch):
        """wait_for_exit_readiness must return immediately in test_mode."""
        eng = make_engine(monkeypatch)
        assert eng.test_mode is True
        await eng.wait_for_exit_readiness(TOKEN, timeout_sec=0.01)

    async def test_await_sellable_balance_none_in_test_mode(self, monkeypatch):
        """_await_sellable_balance must return None in test_mode without polling."""
        eng = make_engine(monkeypatch)
        assert eng.test_mode is True
        got = await eng._await_sellable_balance(TOKEN, 5.0)
        assert got is None


class TestRecoverFillAfterCancel:
    """_recover_fill_after_cancel detects fills that race with cancel-before-reprice."""

    async def test_skipped_in_test_mode(self, monkeypatch):
        """Live engine in test_mode must not sleep or call CLOB sync."""
        eng = make_engine(monkeypatch)
        order = make_order()
        assert await eng._recover_fill_after_cancel(order, "oid-x") is False

    async def test_matched_status_sets_filled(self, monkeypatch):
        """CLOB matched + size_matched must mark tracked order FILLED."""
        eng = make_engine(monkeypatch)
        eng.test_mode = False
        order = make_order(side=SELL_SIDE, size=10.0, filled=0.0)
        monkeypatch.setattr(eng, "_get_order_fill", lambda oid: ("matched", 10.0))
        with patch("core.live_engine.asyncio.sleep", new_callable=AsyncMock):
            ok = await eng._recover_fill_after_cancel(order, "oid1")
        assert ok is True
        assert order.status == OrderStatus.FILLED
        assert order.filled_size == pytest.approx(10.0)

    async def test_cancelled_with_full_size_matched(self, monkeypatch):
        """Canceled status with size_matched to full size must return True."""
        eng = make_engine(monkeypatch)
        eng.test_mode = False
        order = make_order(side=SELL_SIDE, size=10.0309, filled=0.0)
        monkeypatch.setattr(
            eng,
            "_get_order_fill",
            lambda oid: ("canceled", 10.0309),
        )
        with patch("core.live_engine.asyncio.sleep", new_callable=AsyncMock):
            ok = await eng._recover_fill_after_cancel(order, "oid1")
        assert ok is True
        assert order.status == OrderStatus.FILLED
        assert order.filled_size == pytest.approx(10.0309)


class TestLiveRiskManager:
    """Daily loss guard stops trading at the configured threshold."""

    def test_negative_limit_stops_at_exact_threshold(self):
        """When max_daily_loss is -2, pnl -2.0 must block further trades."""
        rm = LiveRiskManager(max_daily_loss=-2.0, pnl=-1.5, trades=1)
        assert rm.can_trade() is True
        rm.pnl = -2.0
        assert rm.can_trade() is False

    def test_negative_limit_allows_above_threshold(self):
        """PnL slightly above the limit still allows trading."""
        rm = LiveRiskManager(max_daily_loss=-2.0, pnl=-1.99, trades=0)
        assert rm.can_trade() is True
