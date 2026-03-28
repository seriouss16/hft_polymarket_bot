"""Session profile loader: applies day/night parameter overrides from env.

Night profile: 23:00–06:00 UTC (calm BTC, sparse WS updates, low speed).
Day  profile: 06:00–23:00 UTC (volatile BTC, fast updates, trending moves).

Each profile overrides a subset of env-vars in-process via os.environ so that
all downstream code reading from os.getenv() automatically picks up the new
values.  The mapping is defined in NIGHT_PROFILE / DAY_PROFILE dicts.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timezone
from typing import Final

NIGHT_START_UTC: Final[int] = int(os.getenv("HFT_NIGHT_START_UTC_HOUR", "23"))
NIGHT_END_UTC: Final[int] = int(os.getenv("HFT_NIGHT_END_UTC_HOUR", "6"))

# Parameters that differ between day and night.
# Values must be strings (same format as env vars).
_NIGHT: dict[str, str] = {
    # Calm BTC: speed is 0 most of the time → disable directional speed gates.
    "HFT_ENTRY_UP_SPEED_MIN": "0.0",
    "HFT_ENTRY_DOWN_SPEED_MAX": "0.0",
    # Polymarket WS is sparse at night → allow up to 2.5 s book staleness.
    "HFT_ENTRY_MAX_LATENCY_MS": "2500.0",
    # Z-score is chaotic at zero speed → disable monotonicity check.
    "HFT_ENTRY_ZSCORE_TREND_ENABLED": "0",
    "HFT_ENTRY_ZSCORE_STRICT_TICKS": "1",
    # Low-speed edge multiplier: at night edge can stay ~4-8 pts for long periods.
    # Keep MULT=1.5 (relaxed vs day 2.0) so entries at edge 6+ pts are not blocked.
    "HFT_ENTRY_LOW_SPEED_EDGE_MULT": "1.5",
    "HFT_ENTRY_LOW_SPEED_ABS": "0.5",
    # Speed floor: disable so signals fire even when BTC price is flat.
    "HFT_SPEED_FLOOR": "0.0",
    # Soft-flow max latency: WS can be 1-2 s stale → raise.
    "HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS": "2000.0",
}

_DAY: dict[str, str] = {
    # Volatile BTC: require directional momentum before entry.
    "HFT_ENTRY_UP_SPEED_MIN": "2.0",
    "HFT_ENTRY_DOWN_SPEED_MAX": "-2.0",
    # Fast WS updates during day → tight staleness gate.
    "HFT_ENTRY_MAX_LATENCY_MS": "1350.0",
    # Z-score trends nicely during day volatility → enable strict check.
    "HFT_ENTRY_ZSCORE_TREND_ENABLED": "1",
    "HFT_ENTRY_ZSCORE_STRICT_TICKS": "2",
    # Strong low-speed protection for choppy micro-sessions.
    "HFT_ENTRY_LOW_SPEED_EDGE_MULT": "2.0",
    "HFT_ENTRY_LOW_SPEED_ABS": "1.0",
    # Speed floor: require at least minimal momentum.
    "HFT_SPEED_FLOOR": "0.02",
    # Soft-flow max latency: daytime feeds are fast.
    "HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS": "800.0",
}

_CURRENT_PROFILE: str | None = None


def _utc_hour() -> int:
    """Return current UTC hour (0-23)."""
    return datetime.now(timezone.utc).hour


def is_night() -> bool:
    """Return True if current UTC hour falls within the night window."""
    h = _utc_hour()
    if NIGHT_START_UTC < NIGHT_END_UTC:
        return NIGHT_START_UTC <= h < NIGHT_END_UTC
    # Wraps midnight: e.g. 23..6 → night from 23:00 to 05:59.
    return h >= NIGHT_START_UTC or h < NIGHT_END_UTC


def current_profile_name() -> str:
    """Return 'night' or 'day' based on current UTC time."""
    return "night" if is_night() else "day"


def _apply(profile: dict[str, str], name: str) -> None:
    """Write profile values into os.environ."""
    for key, val in profile.items():
        os.environ[key] = val
    logging.info(
        "🌙 [SESSION] Profile applied: %s — %d overrides active.",
        name.upper(),
        len(profile),
    )
    for key, val in profile.items():
        logging.debug("  [SESSION] %s=%s", key, val)


def apply_profile(force: bool = False) -> str:
    """Apply day or night profile to os.environ if the session window changed.

    Returns the profile name that is now active ('night' or 'day').
    Logs only when the profile changes (or force=True).
    """
    global _CURRENT_PROFILE
    name = current_profile_name()
    if name == _CURRENT_PROFILE and not force:
        return name
    if name == "night":
        _apply(_NIGHT, "night")
        logging.info(
            "🌙 [SESSION] NIGHT mode active (UTC %02d:00–%02d:00). "
            "Speed gates OFF, latency limit 2500ms, z-score filter OFF.",
            NIGHT_START_UTC, NIGHT_END_UTC,
        )
    else:
        _apply(_DAY, "day")
        logging.info(
            "☀️  [SESSION] DAY mode active (UTC %02d:00–%02d:00). "
            "Speed gates ON (±2 pts/s), latency limit 1350ms, z-score STRICT_TICKS=2.",
            NIGHT_END_UTC, NIGHT_START_UTC,
        )
    _CURRENT_PROFILE = name
    return name


def maybe_switch_profile() -> str | None:
    """Check if UTC hour crossed a session boundary and switch profile if needed.

    Returns the new profile name if a switch occurred, else None.
    """
    global _CURRENT_PROFILE
    name = current_profile_name()
    if name == _CURRENT_PROFILE:
        return None
    apply_profile(force=True)
    return name
