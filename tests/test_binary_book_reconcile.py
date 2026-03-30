"""Tests for complementary UP/DOWN CLOB book reconciliation."""

from __future__ import annotations

from core.live_common import reconcile_binary_outcome_books


def test_reconcile_trusts_more_extreme_side_stale_up_fresh_down():
    """Stale UP (~0.76) + resolution DOWN (~0.99): must not derive DOWN from UP."""
    book = {
        "bid": 0.76,
        "ask": 0.77,
        "down_bid": 0.99,
        "down_ask": 1.0,
    }
    assert reconcile_binary_outcome_books(book) is True
    # Trusted DOWN: UP = complement of DOWN
    assert book["bid"] < book["ask"]
    assert abs((book["bid"] + book["ask"]) / 2.0 + (book["down_bid"] + book["down_ask"]) / 2.0 - 1.0) < 0.06
    assert book["down_bid"] == 0.99
    assert book["down_ask"] == 1.0


def test_reconcile_noop_when_mids_already_sum_to_one():
    book = {
        "bid": 0.45,
        "ask": 0.47,
        "down_bid": 0.53,
        "down_ask": 0.55,
    }
    assert reconcile_binary_outcome_books(book, tol=0.05) is False


def test_reconcile_fills_down_from_up_when_only_up_valid():
    book = {
        "bid": 0.40,
        "ask": 0.42,
        "down_bid": 0.0,
        "down_ask": 0.0,
    }
    assert reconcile_binary_outcome_books(book) is True
    assert 0.01 <= book["down_bid"] < book["down_ask"] <= 0.99
