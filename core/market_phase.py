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
    if os.getenv("HFT_PHASE_SOFT_FLOW_ENABLE", "1") != "1":
        return "latency"

    trend = str(trend_state.get("trend", "FLAT"))
    speed = float(trend_state.get("speed", 0.0))
    edge = float(trend_state.get("edge", 0.0))
    age = float(trend_state.get("age", 0.0))

    soft_min_age = float(os.getenv("HFT_PHASE_SOFT_MIN_TREND_AGE_SEC", "2.0"))
    soft_max_speed = float(os.getenv("HFT_PHASE_SOFT_MAX_ABS_SPEED", "55.0"))
    soft_max_edge = float(os.getenv("HFT_PHASE_SOFT_MAX_ABS_EDGE", "18.0"))
    soft_max_lat = float(os.getenv("HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS", "850.0"))

    vol_edge = float(os.getenv("HFT_PHASE_VOLATILE_MIN_ABS_EDGE", "28.0"))
    vol_speed = float(os.getenv("HFT_PHASE_VOLATILE_MIN_ABS_SPEED", "220.0"))

    if trend == "FLAT":
        return "latency"

    if abs(edge) >= vol_edge or abs(speed) >= vol_speed:
        return "latency"

    if (
        trend in ("UP", "DOWN")
        and age >= soft_min_age
        and abs(speed) <= soft_max_speed
        and abs(edge) <= soft_max_edge
        and float(latency_ms) <= soft_max_lat
    ):
        return "soft_flow"

    return "latency"
