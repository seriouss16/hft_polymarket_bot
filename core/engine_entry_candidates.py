"""Primary and momentum-alt entry candidate selection for HFTEngine."""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from core.engine_entry_gates import (low_speed_edge_multiplier,
                                     zscore_monotonic_for_direction)
from core.engine_trend import dynamic_edge_threshold


def entry_momentum_alt_signal(
    eng: Any,
    edge: float,
    trend: str,
    speed: float,
    price_history,
    recent_pnl: float,
    latency_ms: float,
    edge_mult: float,
):
    """Secondary entry path: momentum + monotone z-score + acceleration without full trend age."""
    if not eng._regime_allows_new_entries():
        return None
    if not eng.entry_momentum_alt_enabled:
        return None
    buy_edge_dyn, sell_edge_dyn = dynamic_edge_threshold(
        eng,
        price_history=price_history,
        recent_pnl=recent_pnl,
        latency_ms=latency_ms,
        extra_mult=edge_mult,
    )
    lsm = low_speed_edge_multiplier(speed, eng.entry_low_speed_abs, eng.entry_low_speed_edge_mult)
    buy_edge_dyn *= lsm
    sell_edge_dyn *= lsm
    if abs(edge) < eng.noise_edge * 2.0:
        return None

    strictness = getattr(eng, "zscore_monotonic_strictness", "strict")
    if not zscore_monotonic_for_direction(eng._zscore_samples, eng.entry_zscore_strict_ticks, trend, strictness):
        # Debug logging when signal is blocked due to monotonicity in relaxed mode
        if strictness == "relaxed" and logging.getLogger().isEnabledFor(logging.DEBUG):
            zs = list(eng._zscore_samples)
            recent = (
                zs[-(eng.entry_zscore_strict_ticks + 1) :] if eng.entry_zscore_strict_ticks > 1 else [zs[-2], zs[-1]]
            )
            if trend == "UP":
                violations = sum(1 for i in range(len(recent) - 1) if not (recent[i] < recent[i + 1]))
            else:
                violations = sum(1 for i in range(len(recent) - 1) if not (recent[i] > recent[i + 1]))
            logging.debug(
                "Entry blocked: z-score monotonicity (relaxed mode, %d violations, k=%d, dir=%s)",
                violations,
                eng.entry_zscore_strict_ticks,
                trend,
            )
        return None
    if not eng.entry_speed_acceleration_ok(trend, speed):
        return None
    if trend == "UP" and edge >= buy_edge_dyn * 0.85 and speed >= eng.speed_floor:
        return "BUY_UP"
    if trend == "DOWN" and edge <= -sell_edge_dyn * 0.85 and speed <= -eng.speed_floor:
        return "BUY_DOWN"
    return None


def entry_candidate_from_state(
    eng: Any,
    edge,
    age,
    trend,
    speed,
    price_history,
    recent_pnl=0.0,
    latency_ms=0.0,
    up_mid=0.0,
    down_mid=0.0,
    edge_mult=1.0,
    up_ask=0.0,
    down_ask=0.0,
):
    """Return BUY_UP/BUY_DOWN/None from trend vs oracle (no cooldown / no update_trend here)."""
    if not eng._regime_allows_new_entries():
        return None
    buy_edge_dyn, sell_edge_dyn = dynamic_edge_threshold(
        eng,
        price_history=price_history,
        recent_pnl=recent_pnl,
        latency_ms=latency_ms,
        extra_mult=edge_mult,
    )
    lsm = low_speed_edge_multiplier(speed, eng.entry_low_speed_abs, eng.entry_low_speed_edge_mult)
    buy_edge_dyn *= lsm
    sell_edge_dyn *= lsm
    if abs(edge) < eng.noise_edge:
        return None
    strong = eng._is_strong_oracle_edge(edge)
    aggressive = eng._is_aggressive_oracle_edge(edge)
    if aggressive and os.getenv("HFT_AGGRESSIVE_EXTREME_ASK_BLOCK", "1") == "1":
        hi = float(os.getenv("HFT_AGGRESSIVE_EXTREME_ASK_HI", "0.79"))
        lo = float(os.getenv("HFT_AGGRESSIVE_EXTREME_ASK_LO", "0.21"))
        ua = float(up_ask or 0.0)
        da = float(down_ask or 0.0)
        if trend == "UP" and ua > 0.0 and (ua > hi or ua < lo):
            logging.info(
                "Entry blocked: extreme book level (UP ask=%.4f, aggressive edge)",
                ua,
            )
            return None
        if trend == "DOWN" and da > 0.0 and (da > hi or da < lo):
            logging.info(
                "Entry blocked: extreme book level (DOWN ask=%.4f, aggressive edge)",
                da,
            )
            return None
    if aggressive:
        now_ts = time.time()
        noise_min = float(os.getenv("HFT_AGGRESSIVE_ENTRY_LOG_MIN_SEC"))
        if noise_min <= 0.0 or now_ts - eng._last_entry_noise_log_ts >= noise_min:
            logging.info(
                "🔥 AGGRESSIVE ENTRY candidate: edge=%.2f (>= %.1fx buy_edge=%.2f)",
                edge,
                eng.aggressive_edge_mult,
                eng.buy_edge,
            )
            eng._last_entry_noise_log_ts = now_ts
    age_need = eng.entry_confirm_age_strong if strong else eng.entry_confirm_age
    up_speed_ok = speed >= eng.entry_up_speed_min or (strong and speed >= eng.speed_floor)
    down_speed_ok = speed <= eng.entry_down_speed_max or (strong and speed <= -eng.speed_floor)
    if aggressive and trend == "UP" and edge >= buy_edge_dyn:
        up_speed_ok = up_speed_ok or speed >= eng.aggressive_entry_relax_speed
    if aggressive and trend == "DOWN" and edge <= -sell_edge_dyn:
        down_speed_ok = down_speed_ok or speed >= -eng.aggressive_entry_relax_speed_down
    low = eng.entry_extreme_price_low
    high = eng.entry_extreme_price_high
    if (
        abs(edge) < eng.entry_extreme_min_edge
        and not strong
        and (
            (up_mid > 0.0 and (up_mid < low or up_mid > high))
            or (down_mid > 0.0 and (down_mid < low or down_mid > high))
        )
    ):
        return None
    depth = eng.trend_depth
    dm = eng.entry_depth_mult
    if (
        trend == "UP"
        and age >= age_need
        and depth >= buy_edge_dyn * dm
        and edge >= buy_edge_dyn
        and speed >= eng.speed_floor
        and up_speed_ok
    ):
        if len(eng.edge_window) >= 2:
            last_edges = [e for _, e in list(eng.edge_window)[-2:]]
            if not all(e > 0 for e in last_edges):
                return None
        return "BUY_UP"
    speed_ok_down = speed <= -eng.speed_floor or (
        aggressive and trend == "DOWN" and edge <= -sell_edge_dyn and speed >= -eng.aggressive_entry_relax_speed_down
    )
    if (
        trend == "DOWN"
        and age >= age_need
        and depth >= sell_edge_dyn * dm
        and edge <= -sell_edge_dyn
        and speed_ok_down
        and down_speed_ok
    ):
        if len(eng.edge_window) >= 2:
            last_edges = [e for _, e in list(eng.edge_window)[-2:]]
            if not all(e < 0 for e in last_edges):
                return None
        return "BUY_DOWN"
    if abs(edge) >= eng.buy_edge * eng.aggressive_edge_mult * 1.2:
        sj_min_age = float(os.getenv("HFT_STRONG_JUMP_MIN_TREND_AGE_SEC"))
        if sj_min_age > 0.0 and age < sj_min_age:
            return None
        now_ts = time.time()
        noise_min = float(os.getenv("HFT_AGGRESSIVE_ENTRY_LOG_MIN_SEC"))
        if noise_min <= 0.0 or now_ts - eng._last_entry_noise_log_ts >= noise_min:
            logging.info(
                "🚀 STRONG JUMP detected edge=%.2f -> forcing early entry",
                edge,
            )
            eng._last_entry_noise_log_ts = now_ts
        if trend == "UP" and edge > 0:
            return "BUY_UP"
        if trend == "DOWN" and edge < 0:
            return "BUY_DOWN"
    return None
