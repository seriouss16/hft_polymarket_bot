"""Session profile loader: applies day/night/weekend parameter overrides from env.

Five profiles based on UTC weekday + hour:

  Weekend (Sat/Sun UTC):
    WEEKEND_NIGHT  — 23:00–06:00 UTC: very calm, sparse Poly WS, range-bound.
    WEEKEND_DAY    — 06:00–23:00 UTC: still calm but more active range trading.

  Weekday (Mon–Fri UTC):
    WEEKDAY_NIGHT  — 23:00–06:00 UTC: calm, sparse WS, flat BTC.
    WEEKDAY_DAWN   — 06:00–09:00 UTC: transitional, BTC waking up.
    WEEKDAY_DAY    — 09:00–23:00 UTC: volatile, fast WS, trending moves.

Each profile overrides a subset of env-vars in-process via os.environ so that
all downstream code reading from os.getenv() automatically picks up the new values.
Call strategy_hub.reload_profile_params() after every profile switch to push
the new values into running engine instances.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Final

NIGHT_START_UTC: Final[int] = int(os.getenv("HFT_NIGHT_START_UTC_HOUR", "23"))
NIGHT_END_UTC: Final[int] = int(os.getenv("HFT_NIGHT_END_UTC_HOUR", "6"))
DAWN_END_UTC: Final[int] = int(os.getenv("HFT_DAWN_END_UTC_HOUR", "9"))

# ---------------------------------------------------------------------------
# Profile parameter sets — keys must match HFTEngine.__init__ env reads.
# Values must be strings (same format as env vars).
# ---------------------------------------------------------------------------

_WEEKEND_NIGHT: dict[str, str] = {
    # Very calm: BTC barely moves at night on weekends.
    "HFT_ENTRY_UP_SPEED_MIN": "0.0",
    "HFT_ENTRY_DOWN_SPEED_MAX": "0.0",
    "HFT_SPEED_FLOOR": "0.0",
    # Poly WS extremely sparse on weekend nights — allow up to 3 s staleness.
    "HFT_ENTRY_MAX_LATENCY_MS": "3000.0",
    "HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS": "2500.0",
    # Z-score is meaningless at zero speed.
    "HFT_ENTRY_ZSCORE_TREND_ENABLED": "0",
    "HFT_ENTRY_ZSCORE_STRICT_TICKS": "1",
    # Relax low-speed multiplier — entries need to fire at very low momentum.
    "HFT_ENTRY_LOW_SPEED_EDGE_MULT": "1.2",
    "HFT_ENTRY_LOW_SPEED_ABS": "0.3",
    # Tighter SL/TP for range: book ticks are huge but direction unclear.
    "HFT_POLY_SL_MOVE": "0.0150",
    "HFT_POLY_TP_MOVE": "0.0040",
    # Min hold: 3 s is enough for range ticks.
    "HFT_MIN_HOLD_SEC": "3.0",
    # Loss cooldown: very short — range recovers fast.
    "LOSS_COOLDOWN_SEC": "3",
}

_WEEKEND_DAY: dict[str, str] = {
    # Weekend day: still range-bound but more active (log showed +54% in 18 min).
    # 97% speed=0 ticks — never require directional speed.
    "HFT_ENTRY_UP_SPEED_MIN": "0.0",
    "HFT_ENTRY_DOWN_SPEED_MAX": "0.0",
    "HFT_SPEED_FLOOR": "0.0",
    # Poly WS somewhat better during day but still 1-2 s stale.
    "HFT_ENTRY_MAX_LATENCY_MS": "2500.0",
    "HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS": "2000.0",
    # Z-score: disabled — chaotic at zero speed.
    "HFT_ENTRY_ZSCORE_TREND_ENABLED": "0",
    "HFT_ENTRY_ZSCORE_STRICT_TICKS": "1",
    # Low speed multiplier: very relaxed for range.
    "HFT_ENTRY_LOW_SPEED_EDGE_MULT": "1.1",
    "HFT_ENTRY_LOW_SPEED_ABS": "0.3",
    # Wider SL for large tick jumps (0.39→0.95 in log), TP tuned for range.
    "HFT_POLY_SL_MOVE": "0.0120",
    "HFT_POLY_TP_MOVE": "0.0035",
    "HFT_MIN_HOLD_SEC": "3.0",
    # Very short cooldown — range markets recover quickly after a loss.
    "LOSS_COOLDOWN_SEC": "3",
}

_WEEKDAY_NIGHT: dict[str, str] = {
    # Weekday night: calm BTC, sparse WS updates, zero speed.
    "HFT_ENTRY_UP_SPEED_MIN": "0.0",
    "HFT_ENTRY_DOWN_SPEED_MAX": "0.0",
    "HFT_SPEED_FLOOR": "0.0",
    "HFT_ENTRY_MAX_LATENCY_MS": "2500.0",
    "HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS": "2000.0",
    "HFT_ENTRY_ZSCORE_TREND_ENABLED": "0",
    "HFT_ENTRY_ZSCORE_STRICT_TICKS": "1",
    "HFT_ENTRY_LOW_SPEED_EDGE_MULT": "1.5",
    "HFT_ENTRY_LOW_SPEED_ABS": "0.5",
    "HFT_POLY_SL_MOVE": "0.0100",
    "HFT_POLY_TP_MOVE": "0.0030",
    "HFT_MIN_HOLD_SEC": "3.0",
    "LOSS_COOLDOWN_SEC": "5",
}

_WEEKDAY_DAWN: dict[str, str] = {
    # Weekday dawn: BTC waking up, speed occasionally present but often zero.
    "HFT_ENTRY_UP_SPEED_MIN": "0.0",
    "HFT_ENTRY_DOWN_SPEED_MAX": "0.0",
    "HFT_SPEED_FLOOR": "0.0",
    # Slightly tighter than night — WS getting better.
    "HFT_ENTRY_MAX_LATENCY_MS": "2000.0",
    "HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS": "1500.0",
    # Z-score still chaotic in early morning.
    "HFT_ENTRY_ZSCORE_TREND_ENABLED": "0",
    "HFT_ENTRY_ZSCORE_STRICT_TICKS": "1",
    "HFT_ENTRY_LOW_SPEED_EDGE_MULT": "1.5",
    "HFT_ENTRY_LOW_SPEED_ABS": "0.5",
    "HFT_POLY_SL_MOVE": "0.0100",
    "HFT_POLY_TP_MOVE": "0.0030",
    "HFT_MIN_HOLD_SEC": "3.0",
    "LOSS_COOLDOWN_SEC": "5",
}

_WEEKDAY_DAY: dict[str, str] = {
    # Weekday day: volatile BTC, fast WS, trending moves.
    "HFT_ENTRY_UP_SPEED_MIN": "2.0",
    "HFT_ENTRY_DOWN_SPEED_MAX": "-2.0",
    "HFT_SPEED_FLOOR": "0.02",
    # Fast WS → tight staleness gate.
    "HFT_ENTRY_MAX_LATENCY_MS": "1350.0",
    "HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS": "800.0",
    # Z-score trends well with volatility.
    "HFT_ENTRY_ZSCORE_TREND_ENABLED": "1",
    "HFT_ENTRY_ZSCORE_STRICT_TICKS": "2",
    # Strong low-speed protection against choppy micro-sessions.
    "HFT_ENTRY_LOW_SPEED_EDGE_MULT": "2.0",
    "HFT_ENTRY_LOW_SPEED_ABS": "1.0",
    # Tighter SL/TP: trending markets have clear direction, use momentum.
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
    """Return current UTC hour (0-23)."""
    return _utc_now().hour


def _is_weekend() -> bool:
    """Return True if today is Saturday or Sunday in UTC."""
    return _utc_now().weekday() >= 5  # 5=Sat, 6=Sun


def _is_night_hour(h: int) -> bool:
    """Return True if h falls in the night window (wraps midnight)."""
    if NIGHT_START_UTC < NIGHT_END_UTC:
        return NIGHT_START_UTC <= h < NIGHT_END_UTC
    return h >= NIGHT_START_UTC or h < NIGHT_END_UTC


def current_profile_name() -> str:
    """Return the active profile name based on UTC weekday and hour.

    Returns one of: 'weekend_night', 'weekend_day',
                    'weekday_night', 'weekday_dawn', 'weekday_day'.
    """
    h = _utc_hour()
    weekend = _is_weekend()
    night = _is_night_hour(h)

    if weekend:
        return "weekend_night" if night else "weekend_day"

    # Weekday branches.
    if night:
        return "weekday_night"
    if NIGHT_END_UTC <= h < DAWN_END_UTC:
        return "weekday_dawn"
    return "weekday_day"


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


_PROFILE_MAP: dict[str, tuple[dict[str, str], str]] = {
    "weekend_night": (_WEEKEND_NIGHT, "🌙🏖️  WEEKEND NIGHT"),
    "weekend_day":   (_WEEKEND_DAY,   "☀️🏖️  WEEKEND DAY"),
    "weekday_night": (_WEEKDAY_NIGHT, "🌙     WEEKDAY NIGHT"),
    "weekday_dawn":  (_WEEKDAY_DAWN,  "🌅     WEEKDAY DAWN"),
    "weekday_day":   (_WEEKDAY_DAY,   "☀️      WEEKDAY DAY"),
}

_PROFILE_DESC: dict[str, str] = {
    "weekend_night": "speed=0 SL=1.5% TP=0.4% latency=3000ms cooldown=3s",
    "weekend_day":   "speed=0 SL=1.2% TP=0.35% latency=2500ms cooldown=3s",
    "weekday_night": "speed=0 SL=1.0% TP=0.3% latency=2500ms cooldown=5s",
    "weekday_dawn":  "speed=0 SL=1.0% TP=0.3% latency=2000ms cooldown=5s",
    "weekday_day":   "speed=±2 SL=1.0% TP=0.3% latency=1350ms z-score=ON cooldown=5s",
}


def apply_profile(force: bool = False) -> str:
    """Apply session profile to os.environ if the window changed.

    Returns the profile name now active.
    """
    global _CURRENT_PROFILE
    name = current_profile_name()
    if name == _CURRENT_PROFILE and not force:
        return name
    profile_dict, emoji_label = _PROFILE_MAP[name]
    _apply(profile_dict, name)
    logging.info(
        "%s mode. %s",
        emoji_label,
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
