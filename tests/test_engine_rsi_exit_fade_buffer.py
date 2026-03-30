"""Fade buffer widens RSI thresholds for RSI_RANGE_EXIT (no time component)."""

from __future__ import annotations

from types import SimpleNamespace

from core.engine_rsi_exit import rsi_range_exit_triggered


def _eng(**kwargs) -> SimpleNamespace:
    base = {
        "rsi_range_exit_band_margin": 5.0,
        "rsi_range_exit_fade_buffer": 0.0,
        "rsi_range_exit_min_profit_usd": 0.1,
        "rsi_range_exit_min_hold_sec": 0.0,
        "rsi_entry_up_low": 30.0,
        "rsi_entry_up_high": 75.0,
        "rsi_entry_down_low": 25.0,
        "rsi_entry_down_high": 40.0,
        "rsi_extreme_high": 95.0,
        "rsi_extreme_low": 5.0,
        "rsi_exit_clamp_high": 99.0,
        "rsi_exit_clamp_low": 1.0,
    }
    base.update(kwargs)
    o = SimpleNamespace(**base)
    # High TP line so band-edge TP branches do not fire in these fade-only tests.
    o._pnl_target_and_stop_lines = lambda: (100.0, 0.0)
    return o


def test_down_fade_requires_higher_rsi_when_buffer_positive():
    """DOWN fade: rx >= down_high + margin + buffer."""
    e0 = _eng(rsi_range_exit_fade_buffer=0.0)
    e8 = _eng(rsi_range_exit_fade_buffer=8.0)
    # margin 5 → threshold 45 without buffer, 53 with buffer
    assert rsi_range_exit_triggered(e0, "DOWN", 46.0, 0.5, hold_sec=1.0) is True
    assert rsi_range_exit_triggered(e8, "DOWN", 46.0, 0.5, hold_sec=1.0) is False
    assert rsi_range_exit_triggered(e8, "DOWN", 54.0, 0.5, hold_sec=1.0) is True


def test_up_fade_requires_lower_rsi_when_buffer_positive():
    """UP fade: rx <= up_low - margin - buffer."""
    e0 = _eng(rsi_range_exit_fade_buffer=0.0)
    e8 = _eng(rsi_range_exit_fade_buffer=8.0)
    # up_low 30, margin 5 → threshold 25 vs 17
    assert rsi_range_exit_triggered(e0, "UP", 24.0, 0.5, hold_sec=1.0) is True
    assert rsi_range_exit_triggered(e8, "UP", 24.0, 0.5, hold_sec=1.0) is False
    assert rsi_range_exit_triggered(e8, "UP", 16.0, 0.5, hold_sec=1.0) is True
