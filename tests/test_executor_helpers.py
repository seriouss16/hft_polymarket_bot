"""Tests for module-level helper functions in core/executor.py.

Covers:
- _up_outcome_quotes_ok: valid/invalid bid-ask pairs.
- mark_price_for_side: UP/DOWN mid-price resolution including fallbacks.
- mark_bid_for_side: conservative bid for UP/DOWN.
- PnLTracker.get_unrealized_pnl: mark-to-market using bid.
- PnLTracker.is_good_regime: regime guard logic.
"""

import pytest

from core.executor import (
    PnLTracker,
    _up_outcome_quotes_ok,
    mark_bid_for_side,
    mark_price_for_side,
)


# ---------------------------------------------------------------------------
# _up_outcome_quotes_ok
# ---------------------------------------------------------------------------


class TestUpOutcomeQuotesOk:
    """_up_outcome_quotes_ok validates UP-outcome bid/ask pairs."""

    def test_valid_tight_spread_returns_true(self):
        """Normal (0.48, 0.52) spread should be accepted."""
        assert _up_outcome_quotes_ok(0.48, 0.52) is True

    def test_bid_zero_returns_false(self):
        """Zero bid is invalid."""
        assert _up_outcome_quotes_ok(0.0, 0.52) is False

    def test_ask_equals_one_is_accepted(self):
        """ask = 1.0 is within the valid upper bound (≤ 1.0)."""
        assert _up_outcome_quotes_ok(0.50, 1.0) is True

    def test_ask_above_one_returns_false(self):
        """ask > 1.0 must be rejected."""
        assert _up_outcome_quotes_ok(0.50, 1.01) is False

    def test_bid_above_ask_returns_false(self):
        """bid ≥ ask is an inconsistent book — must be rejected."""
        assert _up_outcome_quotes_ok(0.55, 0.50) is False

    def test_spread_too_wide_returns_false(self):
        """Spread ≥ 0.45 indicates a pathological book."""
        assert _up_outcome_quotes_ok(0.10, 0.56) is False

    def test_negative_bid_returns_false(self):
        """Negative bid must be rejected."""
        assert _up_outcome_quotes_ok(-0.10, 0.50) is False


# ---------------------------------------------------------------------------
# mark_price_for_side
# ---------------------------------------------------------------------------


class TestMarkPriceForSide:
    """mark_price_for_side returns correct mid price for UP and DOWN sides."""

    def test_up_side_returns_mid_from_explicit_quotes(self):
        """UP mid from bid/ask when both are valid."""
        book = {"bid": 0.48, "ask": 0.52}
        assert mark_price_for_side(book, "UP") == pytest.approx(0.50)

    def test_up_side_falls_back_to_mid_field(self):
        """UP mid falls back to book['mid'] when bid/ask absent."""
        book = {"mid": 0.60}
        assert mark_price_for_side(book, "UP") == pytest.approx(0.60)

    def test_up_side_returns_zero_when_no_data(self):
        """Empty book should return 0.0 for UP."""
        assert mark_price_for_side({}, "UP") == pytest.approx(0.0)

    def test_down_side_returns_mid_from_explicit_down_quotes(self):
        """DOWN mid from down_bid/down_ask when explicit legs present."""
        book = {"down_bid": 0.46, "down_ask": 0.50}
        assert mark_price_for_side(book, "DOWN") == pytest.approx(0.48)

    def test_down_side_inferred_from_up_quotes(self):
        """DOWN mid inferred as complement of UP mid when no down legs."""
        book = {"bid": 0.48, "ask": 0.52}
        result = mark_price_for_side(book, "DOWN")
        assert 0.0 < result < 1.0

    def test_down_side_returns_zero_when_no_data(self):
        """Empty book should return 0.0 for DOWN."""
        assert mark_price_for_side({}, "DOWN") == pytest.approx(0.0)

    def test_none_side_returns_zero(self):
        """Unknown side must return 0.0."""
        book = {"bid": 0.48, "ask": 0.52}
        assert mark_price_for_side(book, None) == pytest.approx(0.0)

    def test_down_mid_value_approximately_complements_up(self):
        """UP + DOWN mids should roughly sum to 1.0 when market is balanced."""
        book = {"bid": 0.49, "ask": 0.51}
        up_mid = mark_price_for_side(book, "UP")
        down_mid = mark_price_for_side(book, "DOWN")
        assert abs(up_mid + down_mid - 1.0) < 0.05


# ---------------------------------------------------------------------------
# mark_bid_for_side
# ---------------------------------------------------------------------------


class TestMarkBidForSide:
    """mark_bid_for_side returns the conservative bid for mark-to-market."""

    def test_up_returns_up_bid(self):
        """For UP side return up_bid directly."""
        book = {"bid": 0.48, "ask": 0.52}
        assert mark_bid_for_side(book, "UP") == pytest.approx(0.48)

    def test_down_returns_down_bid_when_explicit(self):
        """For DOWN side with explicit down_bid/ask return down_bid."""
        book = {"down_bid": 0.46, "down_ask": 0.50}
        assert mark_bid_for_side(book, "DOWN") == pytest.approx(0.46)

    def test_down_inferred_from_up_quotes(self):
        """DOWN bid inferred as 1 - up_ask when no explicit down quotes."""
        book = {"bid": 0.48, "ask": 0.52}
        result = mark_bid_for_side(book, "DOWN")
        expected = max(0.01, min(0.99, 1.0 - 0.52))
        assert result == pytest.approx(expected)

    def test_none_side_returns_zero(self):
        """Unknown side returns 0.0."""
        assert mark_bid_for_side({}, None) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# PnLTracker.get_unrealized_pnl
# ---------------------------------------------------------------------------


class TestGetUnrealizedPnl:
    """get_unrealized_pnl returns mark-to-market PnL at conservative bid."""

    def test_unrealized_profit_when_mark_above_entry(self):
        """Mark above entry should give positive unrealized PnL."""
        t = PnLTracker(initial_balance=100.0)
        t.log_trade("BUY_UP", 0.40, amount_usd=10.0)
        book = {"bid": 0.60, "ask": 0.65}
        assert t.get_unrealized_pnl(book) > 0

    def test_unrealized_loss_when_mark_below_entry(self):
        """Mark below entry should give negative unrealized PnL."""
        t = PnLTracker(initial_balance=100.0)
        t.log_trade("BUY_UP", 0.70, amount_usd=10.0)
        book = {"bid": 0.40, "ask": 0.45}
        assert t.get_unrealized_pnl(book) < 0

    def test_no_position_returns_zero(self):
        """No open position must return 0.0."""
        t = PnLTracker(initial_balance=100.0)
        book = {"bid": 0.60, "ask": 0.65}
        assert t.get_unrealized_pnl(book) == pytest.approx(0.0)

    def test_stale_book_returns_zero(self):
        """Empty book with no mark data returns 0.0 and does not raise."""
        t = PnLTracker(initial_balance=100.0)
        t.log_trade("BUY_UP", 0.50, amount_usd=10.0)
        assert t.get_unrealized_pnl({}) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# PnLTracker.is_good_regime
# ---------------------------------------------------------------------------


class TestIsGoodRegime:
    """is_good_regime allows/blocks entries based on recent PnL history."""

    def test_good_regime_when_no_history(self, monkeypatch):
        """Fewer than 8 trades → always good regime."""
        monkeypatch.setenv("HFT_RECENT_TRADES_FOR_REGIME", "12")
        t = PnLTracker(initial_balance=100.0)
        assert t.is_good_regime() is True

    def test_bad_regime_after_consecutive_losses(self, monkeypatch):
        """Enough consecutive losses should trigger a bad-regime cooldown."""
        monkeypatch.setenv("HFT_RECENT_TRADES_FOR_REGIME", "8")
        monkeypatch.setenv("HFT_BAD_REGIME_WINRATE", "0.48")
        monkeypatch.setenv("HFT_REGIME_COOLDOWN_SEC", "100")
        t = PnLTracker(initial_balance=100.0)
        for _ in range(8):
            t.log_trade("BUY", 0.70, amount_usd=10.0)
            t.log_trade("SELL", 0.30)
        assert t.is_good_regime() is False

    def test_good_regime_after_winning_streak(self, monkeypatch):
        """Positive win streak should restore good-regime status."""
        monkeypatch.setenv("HFT_RECENT_TRADES_FOR_REGIME", "12")
        t = PnLTracker(initial_balance=100.0)
        for _ in range(8):
            t.log_trade("BUY", 0.40, amount_usd=10.0)
            t.log_trade("SELL", 0.80)
        assert t.is_good_regime() is True

    def test_regime_cooldown_blocks_entries(self, monkeypatch):
        """During an active regime cooldown, is_good_regime must return False."""
        import time

        monkeypatch.setenv("HFT_RECENT_TRADES_FOR_REGIME", "8")
        t = PnLTracker(initial_balance=100.0)
        t.regime_cooldown_until = time.time() + 9999
        assert t.is_good_regime() is False
