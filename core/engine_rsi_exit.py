"""RSI band exit and clamp helpers for HFTEngine."""

from __future__ import annotations

import os
from typing import Any


def rsi_slope_per_tick(rsi_tick_history) -> float:
    """Approximate RSI slope over the last few engine ticks."""
    if len(rsi_tick_history) < 3:
        return 0.0
    r = list(rsi_tick_history)
    return (r[-1] - r[-3]) / 2.0


def exit_rsi(rsi: float, rsi_exit_clamp_high: float, rsi_exit_clamp_low: float) -> float:
    """Clamp RSI for exit logic to limit spurious 100/0 from short price history."""
    hi = float(rsi_exit_clamp_high)
    lo = float(rsi_exit_clamp_low)
    if hi > lo:
        return min(max(float(rsi), lo), hi)
    return float(rsi)


def rsi_range_exit_triggered(
    eng: Any,
    position_side,
    current_rsi,
    unrealized,
    hold_sec: float = 0.0,
) -> bool:
    """Return True when RSI band exit is allowed (take-profit at band or fade exit past margin)."""
    margin = eng.rsi_range_exit_band_margin
    min_p = eng.rsi_range_exit_min_profit_usd
    tp_line, _ = eng._pnl_target_and_stop_lines()
    min_hold = float(eng.rsi_range_exit_min_hold_sec)
    fade_need_pos = os.getenv("HFT_RSI_RANGE_EXIT_FADE_REQUIRE_POSITIVE") == "1"
    rx = exit_rsi(current_rsi, eng.rsi_exit_clamp_high, eng.rsi_exit_clamp_low)
    if position_side == "UP":
        if rx >= eng.rsi_entry_up_high and unrealized >= tp_line:
            return True
        if rx <= eng.rsi_entry_up_low - margin:
            if min_hold > 0.0 and hold_sec < min_hold:
                return False
            cond = unrealized > min_p or rx <= eng.rsi_extreme_low
            if fade_need_pos and unrealized <= 0.0:
                return unrealized > min_p
            return cond
        return False
    if position_side == "DOWN":
        if rx <= eng.rsi_entry_down_low and unrealized >= tp_line:
            return True
        if rx >= eng.rsi_entry_down_high + margin:
            if min_hold > 0.0 and hold_sec < min_hold:
                return False
            cond = unrealized > min_p or rx >= eng.rsi_extreme_high
            if fade_need_pos and unrealized <= 0.0:
                return unrealized > min_p
            return cond
        return False
    return False
