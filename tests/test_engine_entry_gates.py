"""Tests for entry gate helpers."""

from core.engine_entry_gates import entry_skew_allows_entry


def test_entry_skew_disabled_when_max_nonpositive():
    assert entry_skew_allows_entry(0.0, 1e9) is True
    assert entry_skew_allows_entry(-1.0, 1e9) is True


def test_entry_skew_one_sided_upper_bound():
    assert entry_skew_allows_entry(100.0, -10_000.0) is True
    assert entry_skew_allows_entry(100.0, 0.0) is True
    assert entry_skew_allows_entry(100.0, 50.0) is True
    assert entry_skew_allows_entry(100.0, 100.0) is True
    assert entry_skew_allows_entry(100.0, 101.0) is False
