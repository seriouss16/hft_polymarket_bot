"""Smoke tests: extracted helpers must find all attributes on HFTEngine.

Regression guard after refactors that split engine.py — production enables
trailing TP/SL and RSI range exits while tests often disable flags; both paths
must have corresponding instance attributes loaded from env at init.
"""

from __future__ import annotations

from core.engine import HFTEngine
from core.engine_rsi_exit import rsi_range_exit_triggered
from core.engine_sizing import (
    reset_trailing_state,
    trailing_sl_triggered,
    trailing_tp_triggered,
    update_trailing_state,
)
from core.executor import PnLTracker


def test_trailing_helpers_with_sl_tp_enabled(set_env, monkeypatch):
    """Mirrors production (runtime.env): trailing on — update_trailing_state must not raise."""
    monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
    monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
    pnl = PnLTracker(initial_balance=100.0, live_mode=False)
    eng = HFTEngine(pnl)
    update_trailing_state(eng, 0.05)
    update_trailing_state(eng, 0.10)
    trailing_tp_triggered(eng, 0.02, 100.0)
    trailing_sl_triggered(eng, 0.02, 100.0)
    reset_trailing_state(eng)


def test_rsi_range_exit_triggered_does_not_raise(set_env):
    pnl = PnLTracker(initial_balance=100.0, live_mode=False)
    eng = HFTEngine(pnl)
    assert rsi_range_exit_triggered(eng, "UP", 50.0, 0.0, 0.0) is False
    assert rsi_range_exit_triggered(eng, "DOWN", 50.0, 0.0, 0.0) is False
