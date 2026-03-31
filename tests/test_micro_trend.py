"""Micro-trend edge window metrics (short horizon vs oracle line)."""

from __future__ import annotations

from collections import deque

import pytest

from core.engine_trend import micro_trend_metrics


def test_micro_trend_positive_edge_shrinking_is_toward_target():
    """Positive edge falling toward zero → toward oracle cross."""
    now = 1_000_000.0
    edge_window = deque(
        [
            (now - 1.0, 10.0),
            (now - 0.5, 5.0),
            (now, 0.5),
        ],
        maxlen=120,
    )
    m = micro_trend_metrics(edge_window, now=now, window_sec=2.0)
    assert m["toward_target"] is True
    assert m["micro_slope"] is not None and m["micro_slope"] < 0
    assert m["cross_eta_sec"] is not None


def test_micro_trend_diverging_from_zero_not_toward_target():
    """Edge growing in magnitude → not moving toward cross."""
    now = 1_000_000.0
    edge_window = deque(
        [
            (now - 1.0, 2.0),
            (now - 0.5, 5.0),
            (now, 10.0),
        ],
        maxlen=120,
    )
    m = micro_trend_metrics(edge_window, now=now, window_sec=2.0)
    assert m["toward_target"] is False


def test_micro_trend_empty_window_returns_nones():
    assert micro_trend_metrics(deque(), now=0.0, window_sec=2.0)["micro_slope"] is None
