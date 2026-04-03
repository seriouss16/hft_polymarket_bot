"""Entry gate helpers extracted from HFTEngine (latency, skew, z-score, liquidity, ...)."""

from __future__ import annotations

import os
from collections import deque
from typing import Any

import numpy as np


def max_entry_latency_ms_all_profiles(profile_snapshots: dict[str, Any]) -> float:
    """Return the largest entry_max_latency_ms across profiles for feed warnings."""
    lat = profile_snapshots["latency"]["entry_max_latency_ms"]
    soft = profile_snapshots["soft_flow"]["entry_max_latency_ms"]
    return max(float(lat), float(soft))


def entry_ask_allows_open(ask_px: float, max_entry_ask: float) -> bool:
    """Return False when best ask is at or above max entry price (no buys at 99¢+)."""
    return float(ask_px) < max_entry_ask


def entry_outcome_price_allows(
    side: str,
    up_ask: float,
    down_ask: float,
    *,
    entry_min_ask_up_cap: float,
    entry_max_ask_up_cap: float,
    entry_min_ask_down_cap: float,
    entry_max_ask_down_cap: float,
) -> bool:
    """Return True only when outcome ask is inside configured entry bounds."""

    def _ask_within_bounds(ask: float, min_cap: float, max_cap: float) -> bool:
        ask_val = float(ask)
        min_active = 0.0 < float(min_cap) < 1.0
        max_active = 0.0 < float(max_cap) < 1.0
        if min_active and ask_val < float(min_cap):
            return False
        if max_active and ask_val > float(max_cap):
            return False
        return True

    if side == "UP":
        return _ask_within_bounds(
            ask=up_ask,
            min_cap=entry_min_ask_up_cap,
            max_cap=entry_max_ask_up_cap,
        )
    if side == "DOWN":
        return _ask_within_bounds(
            ask=down_ask,
            min_cap=entry_min_ask_down_cap,
            max_cap=entry_max_ask_down_cap,
        )
    return True


def entry_latency_allows_entry(entry_max_latency_ms: float, latency_ms: float) -> bool:
    """Block entries when max feed staleness_ms exceeds entry_max_latency_ms."""
    if entry_max_latency_ms <= 0.0:
        return True
    return float(latency_ms) <= entry_max_latency_ms


def entry_skew_allows_entry(entry_max_skew_ms: float, skew_ms: float) -> bool:
    """Block entries only when positive skew exceeds the limit (0 disables the gate).

    ``skew_ms`` is (coinbase_recv - poly_recv) in ms. Negative skew means Poly was
    received after Coinbase (favorable for timing); it is never blocked by this gate.
    """
    if entry_max_skew_ms <= 0.0:
        return True
    return float(skew_ms) <= entry_max_skew_ms


def entry_edge_jump_ok(
    edge_now: float,
    edge_speed: float,
    *,
    entry_max_edge_jump_pts: float,
    entry_edge_jump_bypass_abs_speed: float,
    edge_window: deque,
) -> bool:
    """Return False when oracle edge jumps too far in one tick (bad CEX print vs Poly)."""
    if (
        entry_edge_jump_bypass_abs_speed > 0.0
        and abs(float(edge_speed)) >= entry_edge_jump_bypass_abs_speed
    ):
        return True
    if entry_max_edge_jump_pts <= 0.0:
        return True
    if len(edge_window) < 2:
        return True
    prev_edge = float(edge_window[-2][1])
    return abs(float(edge_now) - prev_edge) <= entry_max_edge_jump_pts


def entry_aggressive_trend_age_ok(
    edge_now: float,
    trend_age: float,
    *,
    buy_edge: float,
    aggressive_edge_mult: float,
    entry_aggressive_min_trend_age_sec: float,
) -> bool:
    """Require extra seconds after trend start when edge is in aggressive magnitude."""
    if entry_aggressive_min_trend_age_sec <= 0.0:
        return True
    if abs(edge_now) < buy_edge * aggressive_edge_mult:
        return True
    return float(trend_age) >= entry_aggressive_min_trend_age_sec


def entry_trend_flip_settled_ok(trend_age: float, trend_flip_min_age_sec: float) -> bool:
    """Avoid entries right after a trend cross (chop / saw)."""
    if trend_flip_min_age_sec <= 0.0:
        return True
    return float(trend_age) >= trend_flip_min_age_sec


def entry_rsi_slope_allows(
    side: str,
    current_rsi: float,
    last_rsi_slope: float,
    *,
    entry_rsi_slope_filter_enabled: bool,
    rsi_up_entry_max: float,
    rsi_up_slope_min: float,
    rsi_down_entry_min: float,
    rsi_down_slope_max: float,
) -> bool:
    """Require RSI oversold/overbought with favorable slope for UP/DOWN entries."""
    if not entry_rsi_slope_filter_enabled:
        return True
    slope = float(last_rsi_slope)
    if side == "UP":
        return current_rsi < rsi_up_entry_max and slope > rsi_up_slope_min
    if side == "DOWN":
        return current_rsi > rsi_down_entry_min and slope < rsi_down_slope_max
    return True


def record_entry_samples(eng: Any, speed: float, zscore: float) -> None:
    """Append latest trend speed and z-score for acceleration and z-trend filters."""
    eng._speed_samples.append(float(speed))
    eng._zscore_samples.append(float(zscore))


def entry_liquidity_spread_ok(
    spread_up: float,
    spread_down: float,
    edge: float,
    trend_dir: str,
    *,
    entry_liquidity_max_spread: float,
    spread_gate_up_relax_mult: float,
    wide_spread_min_edge: float,
) -> bool:
    """Return False when UP/DOWN book spread is too wide unless oracle edge is very large."""
    if entry_liquidity_max_spread <= 0.0:
        return True
    mx = entry_liquidity_max_spread
    if trend_dir == "UP" and spread_gate_up_relax_mult > 1.0:
        mx = mx * spread_gate_up_relax_mult
    strong = abs(edge) >= wide_spread_min_edge
    if trend_dir == "UP":
        return spread_up <= mx or strong
    if trend_dir == "DOWN":
        return spread_down <= mx or strong
    return True


def entry_speed_acceleration_ok(
    trend_dir: str,
    speed: float,
    speed_samples: deque,
    *,
    entry_accel_enabled: bool,
    entry_accel_min: float,
) -> bool:
    """Require edge-speed acceleration in the trade direction when enabled."""
    if not entry_accel_enabled:
        return True
    if len(speed_samples) < 4:
        return True
    prev = list(speed_samples)[-4:-1]
    acc = float(speed) - float(np.mean(prev))
    if trend_dir == "UP":
        return acc >= entry_accel_min
    if trend_dir == "DOWN":
        return acc <= -entry_accel_min
    return True


def entry_zscore_trend_ok(
    trend_dir: str,
    edge_speed: float,
    zscore_samples: deque,
    *,
    entry_zscore_trend_enabled: bool,
    entry_zscore_strict_ticks: int,
    entry_zscore_bypass_abs_speed: float,
) -> bool:
    """Require z-score to move monotonically with the intended side for several ticks."""
    if (
        entry_zscore_bypass_abs_speed > 0.0
        and abs(float(edge_speed)) >= entry_zscore_bypass_abs_speed
    ):
        return True
    if not entry_zscore_trend_enabled:
        return True
    k = max(1, entry_zscore_strict_ticks)
    if len(zscore_samples) < 2:
        return True
    zs = list(zscore_samples)
    if k == 1:
        if trend_dir == "UP":
            return zs[-1] > zs[-2]
        if trend_dir == "DOWN":
            return zs[-1] < zs[-2]
        return True
    recent = zs[-(k + 1) :]
    if len(recent) < 2:
        return True
    if trend_dir == "UP":
        return all(recent[i] < recent[i + 1] for i in range(len(recent) - 1))
    if trend_dir == "DOWN":
        return all(recent[i] > recent[i + 1] for i in range(len(recent) - 1))
    return True


def zscore_monotonic_for_direction(
    zscore_samples: deque,
    entry_zscore_strict_ticks: int,
    trend_dir: str,
) -> bool:
    """Return True if recent z-score ticks are strictly monotone in the trade direction."""
    k = max(1, entry_zscore_strict_ticks)
    if len(zscore_samples) < 2:
        return False
    zs = list(zscore_samples)
    if k == 1:
        if trend_dir == "UP":
            return zs[-1] > zs[-2]
        if trend_dir == "DOWN":
            return zs[-1] < zs[-2]
        return False
    recent = zs[-(k + 1) :]
    if len(recent) < 2:
        return False
    if trend_dir == "UP":
        return all(recent[i] < recent[i + 1] for i in range(len(recent) - 1))
    if trend_dir == "DOWN":
        return all(recent[i] > recent[i + 1] for i in range(len(recent) - 1))
    return False


def low_speed_edge_multiplier(speed: float, entry_low_speed_abs: float, entry_low_speed_edge_mult: float) -> float:
    """Raise required oracle edge when edge speed is low (fade / chop risk)."""
    if abs(float(speed)) < entry_low_speed_abs:
        return entry_low_speed_edge_mult
    return 1.0


def latency_expiry_edge_multiplier(
    eng: Any,
    latency_ms: float,
    seconds_to_expiry: float | None,
) -> float:
    """Raise required edge when feed staleness_ms is high or the market slot is near expiry."""
    if eng.no_entry_guards:
        return 1.0
    m = 1.0
    if latency_ms > eng.latency_high_ms:
        m *= eng.latency_high_edge_mult
    elif latency_ms > 250.0:
        m *= 1.10
    if (
        seconds_to_expiry is not None
        and seconds_to_expiry >= 0.0
        and seconds_to_expiry < eng.expiry_tight_sec
    ):
        m *= eng.expiry_edge_mult
    return m


def entry_slot_window_allows(eng: Any, seconds_to_expiry: float | None) -> bool:
    """Allow entries only outside first and last slot guard windows."""
    if seconds_to_expiry is None:
        return True
    sec_to_end = max(0.0, float(seconds_to_expiry))
    if eng.no_entry_last_sec > 0.0 and sec_to_end <= eng.no_entry_last_sec:
        return False
    interval = max(1.0, float(eng.slot_interval_sec))
    sec_from_start = max(0.0, interval - sec_to_end)
    if eng.no_entry_first_sec > 0.0 and sec_from_start <= eng.no_entry_first_sec:
        return False
    return True


def _price_to_beat_filter_enabled() -> bool:
    return os.getenv("HFT_PRICE_TO_BEAT_FILTER_ENABLED") == "1" or os.getenv(
        "HFT_ANCHOR_FILTER_ENABLED"
    ) == "1"


def _price_to_beat_counter_min_delta_pct() -> float:
    raw = (
        os.getenv("HFT_PRICE_TO_BEAT_COUNTER_MIN_DELTA_PCT")
        or os.getenv("HFT_ANCHOR_COUNTER_MIN_DELTA_PCT")
        or "0.0005"
    )
    return float(raw)


def price_to_beat_gate(fast_price: float, slot_price_to_beat: float) -> tuple[bool, bool]:
    """Return (up_allowed, down_allowed) using slot start price (Gamma priceToBeat) vs fast CEX."""
    enabled = _price_to_beat_filter_enabled()
    if not enabled or slot_price_to_beat <= 0.0 or fast_price <= 0.0:
        return True, True
    delta_pct = (fast_price - slot_price_to_beat) / slot_price_to_beat
    min_delta = _price_to_beat_counter_min_delta_pct()
    if delta_pct > 0.0:
        ok_up = True
        ok_down = abs(delta_pct) >= min_delta
    elif delta_pct < 0.0:
        ok_up = abs(delta_pct) >= min_delta
        ok_down = True
    else:
        ok_up = True
        ok_down = True
    return ok_up, ok_down
