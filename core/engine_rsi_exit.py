"""RSI band exit and clamp helpers for HFTEngine."""

from __future__ import annotations

import os
from typing import Any

import numpy as np


def rsi_slope_per_tick(rsi_tick_history) -> float:
    """Approximate RSI slope over the last few engine ticks."""
    if len(rsi_tick_history) < 3:
        return 0.0
    r = list(rsi_tick_history)
    return (r[-1] - r[-3]) / 2.0


def exit_rsi(rsi: float, rsi_exit_clamp_high: float, rsi_exit_clamp_low: float) -> float:
    """Clamp RSI for exit logic to limit spurious 100/0 from short price history.
    
    Args:
        rsi: The RSI value to clamp (typically 0-100 but may exceed due to short history).
        rsi_exit_clamp_high: Upper clamp bound (must be > rsi_exit_clamp_low).
        rsi_exit_clamp_low: Lower clamp bound (must be < rsi_exit_clamp_high).
    
    Returns:
        Clamped RSI value between lo and hi.
        
    Raises:
        ValueError: If rsi_exit_clamp_high <= rsi_exit_clamp_low (invalid configuration).
    """
    hi = float(rsi_exit_clamp_high)
    lo = float(rsi_exit_clamp_low)
    
    # Validate configuration: high must be greater than low
    if hi <= lo:
        raise ValueError(
            f"Invalid RSI exit clamp configuration: high ({hi}) must be > low ({lo}). "
            "Check HFT_RSI_EXIT_CLAMP_HIGH and HFT_RSI_EXIT_CLAMP_LOW in config."
        )
    
    return float(np.clip(rsi, lo, hi))


def rsi_range_exit_triggered(
    eng: Any,
    position_side,
    current_rsi,
    unrealized,
    hold_sec: float = 0.0,
    dynamic_upper: float | None = None,
    dynamic_lower: float | None = None,
) -> bool:
    """Return True when RSI band exit is allowed (take-profit at band or fade exit past margin).
    
    Uses dynamic RSI bands if provided, otherwise falls back to static entry bands.
    Dynamic bands adapt to volatility for more responsive exit thresholds.
    """
    margin = eng.rsi_range_exit_band_margin
    fade_buf = float(getattr(eng, "rsi_range_exit_fade_buffer", 0.0) or 0.0)
    min_p = eng.rsi_range_exit_min_profit_usd
    tp_line, _ = eng._pnl_target_and_stop_lines()
    min_hold = float(eng.rsi_range_exit_min_hold_sec)
    fade_need_pos = os.getenv("HFT_RSI_RANGE_EXIT_FADE_REQUIRE_POSITIVE") == "1"
    rx = exit_rsi(current_rsi, eng.rsi_exit_clamp_high, eng.rsi_exit_clamp_low)
    
    # Choose dynamic or static bands
    if dynamic_upper is not None and dynamic_lower is not None:
        upper_band = dynamic_upper
        lower_band = dynamic_lower
        # Debug logging for dynamic bands
        if os.getenv("HFT_DEBUG_LOG_ENABLED") == "1":
            import json
            try:
                with open(os.getenv("HFT_DEBUG_LOG_PATH", "/dev/null"), "a") as f:
                    f.write(json.dumps({
                        "event": "rsi_exit_dynamic_bands",
                        "position_side": position_side,
                        "current_rsi": current_rsi,
                        "dynamic_upper": float(dynamic_upper),
                        "dynamic_lower": float(dynamic_lower),
                        "clamped_rsi": float(rx),
                    }) + "\n")
            except:
                pass
    else:
        # Fallback to static entry bands (backward compatibility)
        if position_side == "UP":
            upper_band = eng.rsi_entry_up_high
            lower_band = eng.rsi_entry_up_low
        else:  # DOWN
            upper_band = eng.rsi_entry_down_high
            lower_band = eng.rsi_entry_down_low
        # Debug logging for static bands
        if os.getenv("HFT_DEBUG_LOG_ENABLED") == "1":
            import json
            try:
                with open(os.getenv("HFT_DEBUG_LOG_PATH", "/dev/null"), "a") as f:
                    f.write(json.dumps({
                        "event": "rsi_exit_static_bands",
                        "position_side": position_side,
                        "current_rsi": current_rsi,
                        "static_upper": float(upper_band),
                        "static_lower": float(lower_band),
                        "clamped_rsi": float(rx),
                    }) + "\n")
            except:
                pass
    
    if position_side == "UP":
        if rx >= upper_band and unrealized >= tp_line:
            return True
        if rx <= lower_band - margin - fade_buf:
            if min_hold > 0.0 and hold_sec < min_hold:
                return False
            cond = unrealized > min_p or rx <= eng.rsi_extreme_low
            if fade_need_pos and unrealized <= 0.0:
                return unrealized > min_p
            return cond
        return False
    if position_side == "DOWN":
        if rx <= lower_band and unrealized >= tp_line:
            return True
        if rx >= upper_band + margin + fade_buf:
            if min_hold > 0.0 and hold_sec < min_hold:
                return False
            cond = unrealized > min_p or rx >= eng.rsi_extreme_high
            if fade_need_pos and unrealized <= 0.0:
                return unrealized > min_p
            return cond
        return False
    return False
