"""Tests for entry gate helpers."""

import math
from collections import deque

from core.engine_entry_gates import entry_skew_allows_entry, zscore_monotonic_for_direction


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


# Tests for zscore_monotonic_for_direction with different strictness levels
def test_zscore_monotonic_strict_allows_perfect_monotonic():
    """Strict mode should allow perfectly monotonic sequences."""
    zs = deque([1.0, 2.0, 3.0, 4.0])
    assert zscore_monotonic_for_direction(zs, 2, "UP", "strict") is True
    assert zscore_monotonic_for_direction(zs, 3, "UP", "strict") is True
    zs = deque([4.0, 3.0, 2.0, 1.0])
    assert zscore_monotonic_for_direction(zs, 2, "DOWN", "strict") is True
    assert zscore_monotonic_for_direction(zs, 3, "DOWN", "strict") is True


def test_zscore_monotonic_strict_rejects_non_monotonic():
    """Strict mode should reject sequences with any violation."""
    zs = deque([1.0, 3.0, 2.0, 4.0])  # Violation at index 1->2
    assert zscore_monotonic_for_direction(zs, 3, "UP", "strict") is False
    zs = deque([4.0, 2.0, 3.0, 1.0])  # Violation at index 1->2
    assert zscore_monotonic_for_direction(zs, 3, "DOWN", "strict") is False


def test_zscore_monotonic_relaxed_allows_one_violation():
    """Relaxed mode should allow sequences with at most 1 violation."""
    # One violation in 3-ticks window (k=2 means 3 points)
    zs = deque([1.0, 3.0, 2.0, 4.0])  # Violation: 3->2
    assert zscore_monotonic_for_direction(zs, 2, "UP", "relaxed") is True  # 1 violation allowed
    # Two violations should still fail
    zs = deque([1.0, 3.0, 2.0, 1.0, 4.0])  # Violations: 3->2, 2->1 (down), 1->4 ok
    assert zscore_monotonic_for_direction(zs, 4, "UP", "relaxed") is False  # 2 violations


def test_zscore_monotonic_off_always_allows():
    """Off mode should always return True regardless of sequence."""
    zs = deque([1.0, 3.0, 2.0, 4.0])
    assert zscore_monotonic_for_direction(zs, 2, "UP", "off") is True
    zs = deque([4.0, 2.0, 3.0, 1.0])
    assert zscore_monotonic_for_direction(zs, 3, "DOWN", "off") is True


def test_zscore_monotonic_k_equals_1():
    """Test k=1 case (only compare last two points)."""
    zs = deque([1.0, 2.0])
    assert zscore_monotonic_for_direction(zs, 1, "UP", "strict") is True
    assert zscore_monotonic_for_direction(zs, 1, "UP", "relaxed") is True
    assert zscore_monotonic_for_direction(zs, 1, "UP", "off") is True

    zs = deque([2.0, 1.0])
    assert zscore_monotonic_for_direction(zs, 1, "DOWN", "strict") is True
    assert zscore_monotonic_for_direction(zs, 1, "DOWN", "relaxed") is True
    assert zscore_monotonic_for_direction(zs, 1, "DOWN", "off") is True

    zs = deque([1.0, 2.0])
    assert zscore_monotonic_for_direction(zs, 1, "DOWN", "strict") is False
    assert zscore_monotonic_for_direction(zs, 1, "DOWN", "relaxed") is False
    assert zscore_monotonic_for_direction(zs, 1, "DOWN", "off") is True  # off overrides


def test_zscore_monotonic_insufficient_samples():
    """Test with insufficient samples."""
    zs = deque([1.0])
    assert zscore_monotonic_for_direction(zs, 2, "UP", "strict") is False
    assert zscore_monotonic_for_direction(zs, 2, "UP", "relaxed") is False
    assert zscore_monotonic_for_direction(zs, 2, "UP", "off") is True  # off returns True early


def test_zscore_monotonic_unknown_strictness_defaults_to_strict():
    """Unknown strictness value should default to strict behavior."""
    zs = deque([1.0, 3.0, 2.0, 4.0])
    assert zscore_monotonic_for_direction(zs, 2, "UP", "unknown") is False  # violation
    zs = deque([1.0, 2.0, 3.0])
    assert zscore_monotonic_for_direction(zs, 2, "UP", "unknown") is True  # monotonic
