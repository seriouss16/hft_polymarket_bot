"""Classify market conditions and choose latency vs soft-flow engine profile."""

from __future__ import annotations

import os
from typing import Any, Literal

ProfileName = Literal["latency", "soft_flow"]


def select_engine_profile(trend_state: dict[str, Any], latency_ms: float) -> ProfileName:
    """Choose engine profile from the prior tick trend snapshot and current staleness.

    Uses UP/DOWN with bounded speed and edge for soft_flow; large edge/speed or FLAT
    trend selects aggressive latency profile.
    """
    if os.getenv("HFT_PHASE_SOFT_FLOW_ENABLE") != "1":
        return "latency"

    trend = str(trend_state.get("trend", "FLAT"))
    speed = float(trend_state.get("speed", 0.0))
    edge = float(trend_state.get("edge", 0.0))
    age = float(trend_state.get("age", 0.0))

    soft_min_age = float(os.getenv("HFT_PHASE_SOFT_MIN_TREND_AGE_SEC"))
    soft_min_edge = float(os.getenv("HFT_PHASE_SOFT_MIN_ABS_EDGE"))
    soft_max_speed = float(os.getenv("HFT_PHASE_SOFT_MAX_ABS_SPEED"))
    soft_max_edge = float(os.getenv("HFT_PHASE_SOFT_MAX_ABS_EDGE"))
    soft_max_lat = float(os.getenv("HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS"))

    vol_edge = float(os.getenv("HFT_PHASE_VOLATILE_MIN_ABS_EDGE"))
    vol_speed = float(os.getenv("HFT_PHASE_VOLATILE_MIN_ABS_SPEED"))

    if trend == "FLAT":
        return "latency"

    if abs(edge) >= vol_edge or abs(speed) >= vol_speed:
        return "latency"

    if (
        trend in ("UP", "DOWN")
        and age >= soft_min_age
        and abs(edge) >= soft_min_edge
        and abs(speed) <= soft_max_speed
        and abs(edge) <= soft_max_edge
        and float(latency_ms) <= soft_max_lat
    ):
        return "soft_flow"

    return "latency"


def diagnose_phase(trend_state: dict[str, Any], latency_ms: float) -> dict[str, Any]:
    """Return threshold comparisons, selected profile, and consistency check for logging.

    ``soft_eligible`` is True when all conditions for soft_flow are met; it should match
    ``selected == soft_flow`` when ``select_engine_profile`` uses the same rules.
    """
    trend = str(trend_state.get("trend", "FLAT"))
    speed = float(trend_state.get("speed", 0.0))
    edge = float(trend_state.get("edge", 0.0))
    age = float(trend_state.get("age", 0.0))

    soft_min_age = float(os.getenv("HFT_PHASE_SOFT_MIN_TREND_AGE_SEC"))
    soft_min_edge = float(os.getenv("HFT_PHASE_SOFT_MIN_ABS_EDGE"))
    soft_max_speed = float(os.getenv("HFT_PHASE_SOFT_MAX_ABS_SPEED"))
    soft_max_edge = float(os.getenv("HFT_PHASE_SOFT_MAX_ABS_EDGE"))
    soft_max_lat = float(os.getenv("HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS"))

    vol_edge = float(os.getenv("HFT_PHASE_VOLATILE_MIN_ABS_EDGE"))
    vol_speed = float(os.getenv("HFT_PHASE_VOLATILE_MIN_ABS_SPEED"))

    soft_disabled = os.getenv("HFT_PHASE_SOFT_FLOW_ENABLE") != "1"
    directional = trend in ("UP", "DOWN")
    volatile_edge = abs(edge) >= vol_edge
    volatile_speed = abs(speed) >= vol_speed
    age_ok = age >= soft_min_age
    edge_min_ok = abs(edge) >= soft_min_edge
    speed_ok = abs(speed) <= soft_max_speed
    edge_ok = abs(edge) <= soft_max_edge
    lat_ok = float(latency_ms) <= soft_max_lat

    soft_eligible = (
        not soft_disabled
        and directional
        and not volatile_edge
        and not volatile_speed
        and age_ok
        and edge_min_ok
        and speed_ok
        and edge_ok
        and lat_ok
    )

    selected = select_engine_profile(trend_state, latency_ms)
    logic_ok = soft_eligible == (selected == "soft_flow")

    blockers: list[str] = []
    if soft_disabled:
        blockers.append("soft_flow_disabled")
    elif trend == "FLAT":
        blockers.append("flat_trend")
    elif volatile_edge:
        blockers.append("volatile_edge")
    elif volatile_speed:
        blockers.append("volatile_speed")
    else:
        if not age_ok:
            blockers.append("age_below_soft_min")
        if not edge_min_ok:
            blockers.append("edge_below_soft_min")
        if not speed_ok:
            blockers.append("speed_above_soft_max")
        if not edge_ok:
            blockers.append("edge_above_soft_max")
        if not lat_ok:
            blockers.append("stale_above_soft_max")

    return {
        "selected": selected,
        "soft_eligible": soft_eligible,
        "logic_ok": logic_ok,
        "blockers": blockers,
        "thresholds": {
            "soft_min_age": soft_min_age,
            "soft_min_abs_edge": soft_min_edge,
            "soft_max_abs_speed": soft_max_speed,
            "soft_max_abs_edge": soft_max_edge,
            "soft_max_latency_ms": soft_max_lat,
            "volatile_min_abs_edge": vol_edge,
            "volatile_min_abs_speed": vol_speed,
        },
        "observed": {
            "trend": trend,
            "speed": speed,
            "edge": edge,
            "age": age,
            "staleness_ms": float(latency_ms),
        },
        "checks": {
            "directional": directional,
            "age_ok": age_ok,
            "edge_min_ok": edge_min_ok,
            "speed_ok": speed_ok,
            "edge_ok": edge_ok,
            "latency_ok": lat_ok,
        },
    }
