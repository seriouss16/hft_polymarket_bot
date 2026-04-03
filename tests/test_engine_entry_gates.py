"""Tests for entry gate helpers."""

import math

from core.engine_entry_gates import entry_skew_allows_entry


def test_entry_skew_disabled_when_max_nonpositive():
    assert entry_skew_allows_entry(-math.inf, 0.0, 1e9) is True
    assert entry_skew_allows_entry(-math.inf, -1.0, 1e9) is True


def test_entry_skew_no_lower_bound_when_min_neg_inf():
    lo = float("-inf")
    assert entry_skew_allows_entry(lo, 100.0, -10_000.0) is True
    assert entry_skew_allows_entry(lo, 100.0, 50.0) is True
    assert entry_skew_allows_entry(lo, 100.0, 100.0) is True
    assert entry_skew_allows_entry(lo, 100.0, 101.0) is False


def test_entry_skew_finite_min_max_band():
    assert entry_skew_allows_entry(-3000.0, 100.0, -5000.0) is False
    assert entry_skew_allows_entry(-3000.0, 100.0, -2000.0) is True
    assert entry_skew_allows_entry(-3000.0, 100.0, 50.0) is True
    assert entry_skew_allows_entry(-3000.0, 100.0, 150.0) is False
