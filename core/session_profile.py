"""Session profile loader: applies night/day parameter overrides from env.

Two profiles based on UTC weekday + hour:

  NIGHT — applied when any of the following is true:
            • Weekday night: 23:00–06:00 UTC (Mon–Fri).
            • Weekend (Sat/Sun): the entire 24 h period.
          Low volatility, sparse Poly WS, range-bound BTC.
          Relaxed latency gates, no speed requirement, short cooldown.

  DAY   — weekday daytime: 06:00–23:00 UTC (Mon–Fri only).
          Higher volatility, fast WS, occasional directional momentum.
          Tighter latency, optional z-score, slightly longer cooldown.

Each profile overrides a subset of env-vars in-process via os.environ so that
all downstream code reading from os.getenv() automatically picks up the new
values.  Call strategy_hub.reload_profile_params() after every switch to push
the new values into running engine instances.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Final

NIGHT_START_UTC: Final[int] = int(os.getenv("HFT_NIGHT_START_UTC_HOUR", "23"))
NIGHT_END_UTC: Final[int] = int(os.getenv("HFT_NIGHT_END_UTC_HOUR", "6"))

# ---------------------------------------------------------------------------
# Profile parameter sets — keys must match HFTEngine.__init__ env reads.
# Values must be strings (same format as env vars).
# ---------------------------------------------------------------------------

_NIGHT: dict[str, str] = {
    # No speed requirement: BTC barely moves at night or on weekends.
    "HFT_ENTRY_UP_SPEED_MIN": "0.0",
    "HFT_ENTRY_DOWN_SPEED_MAX": "0.0",
    "HFT_SPEED_FLOOR": "0.0",
    # Poly WS is sparse: allow up to 3 s staleness before blocking entries.
    "HFT_ENTRY_MAX_LATENCY_MS": "3000.0",
    "HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS": "2500.0",
    # Z-score is meaningless at near-zero speed.
    "HFT_ENTRY_ZSCORE_TREND_ENABLED": "0",
    "HFT_ENTRY_ZSCORE_STRICT_TICKS": "1",
    # Very relaxed low-speed multiplier — entries must fire at minimal momentum.
    "HFT_ENTRY_LOW_SPEED_EDGE_MULT": "1.2",
    "HFT_ENTRY_LOW_SPEED_ABS": "0.3",
    # SL/TP tuned for range: hold short, exit on small reversal.
    "HFT_POLY_SL_MOVE": "0.0150",
    "HFT_POLY_TP_MOVE": "0.0040",
    "HFT_MIN_HOLD_SEC": "3.0",
    # Very short cooldown — range recovers fast after a loss.
    "LOSS_COOLDOWN_SEC": "3",
}

_DAY: dict[str, str] = {
    # Weekday day: BTC shows occasional directional momentum.
    "HFT_ENTRY_UP_SPEED_MIN": "2.0",
    "HFT_ENTRY_DOWN_SPEED_MAX": "-2.0",
    "HFT_SPEED_FLOOR": "0.02",
    # Tighter staleness gate — Poly WS is fast during business hours.
    "HFT_ENTRY_MAX_LATENCY_MS": "1350.0",
    "HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS": "800.0",
    # Z-score trends well with intraday volatility.
    "HFT_ENTRY_ZSCORE_TREND_ENABLED": "1",
    "HFT_ENTRY_ZSCORE_STRICT_TICKS": "2",
    # Stronger low-speed protection against choppy micro-sessions.
    "HFT_ENTRY_LOW_SPEED_EDGE_MULT": "2.0",
    "HFT_ENTRY_LOW_SPEED_ABS": "1.0",
    # SL/TP: trending direction gives clear signals.
    "HFT_POLY_SL_MOVE": "0.0100",
    "HFT_POLY_TP_MOVE": "0.0030",
    "HFT_MIN_HOLD_SEC": "3.0",
    "LOSS_COOLDOWN_SEC": "5",
}

_CURRENT_PROFILE: str | None = None


def _utc_now() -> datetime:
    """Return current UTC datetime."""
    return datetime.now(timezone.utc)


def _utc_hour() -> int:
    """Return current UTC hour (0–23)."""
    return _utc_now().hour


def _is_weekend() -> bool:
    """Return True if today is Saturday or Sunday in UTC."""
    return _utc_now().weekday() >= 5  # 5=Sat, 6=Sun


def _is_night_hour(h: int) -> bool:
    """Return True if h falls inside the night window (wraps past midnight)."""
    if NIGHT_START_UTC < NIGHT_END_UTC:
        return NIGHT_START_UTC <= h < NIGHT_END_UTC
    return h >= NIGHT_START_UTC or h < NIGHT_END_UTC


def current_profile_name() -> str:
    """Return 'night' or 'day' based on UTC weekday and hour.

    Weekends (Sat/Sun UTC) are treated as NIGHT for the entire 24 h period
    because crypto markets are quietest then and exchanges are less active.
    """
    if _is_weekend():
        return "night"
    return "night" if _is_night_hour(_utc_hour()) else "day"


def _apply(profile: dict[str, str], name: str) -> None:
    """Write profile values into os.environ."""
    for key, val in profile.items():
        os.environ[key] = val
    logging.info(
        "[SESSION] Profile applied: %s — %d overrides.",
        name.upper(),
        len(profile),
    )
    for key, val in profile.items():
        logging.debug("  [SESSION] %s=%s", key, val)


_PROFILE_MAP: dict[str, dict[str, str]] = {
    "night": _NIGHT,
    "day": _DAY,
}

_PROFILE_EMOJI: dict[str, str] = {
    "night": "🌙",
    "day": "☀️",
}

_PROFILE_DESC: dict[str, str] = {
    "night": "speed=0 SL=1.5% TP=0.4% latency=3000ms cooldown=3s (weekends all day)",
    "day": "speed=±2 z-score=ON SL=1.0% TP=0.3% latency=1350ms cooldown=5s",
}


def apply_profile(force: bool = False) -> str:
    """Apply session profile to os.environ if the window changed.

    Returns the profile name now active.
    """
    global _CURRENT_PROFILE
    name = current_profile_name()
    if name == _CURRENT_PROFILE and not force:
        return name
    profile_dict = _PROFILE_MAP[name]
    _apply(profile_dict, name)
    label = "WEEKEND " if _is_weekend() else ""
    logging.info(
        "%s %sNIGHT mode. %s" if name == "night" else "%s %sDAY mode. %s",
        _PROFILE_EMOJI[name],
        label,
        _PROFILE_DESC[name],
    )
    _CURRENT_PROFILE = name
    return name


def maybe_switch_profile() -> str | None:
    """Check if the session profile changed and switch if needed.

    Returns the new profile name if a switch occurred, else None.
    Call strategy_hub.reload_profile_params() after a non-None return.
    """
    global _CURRENT_PROFILE
    name = current_profile_name()
    if name == _CURRENT_PROFILE:
        return None
    apply_profile(force=True)
    return name
