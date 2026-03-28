"""Session profile loader: applies day/night/dawn parameter overrides from env.

Three profiles based on UTC hour:
  Night: 23:00–06:00 UTC — calm BTC, sparse WS updates, zero speed.
  Dawn:  06:00–09:00 UTC — transitional, BTC waking up, mixed speed.
  Day:   09:00–23:00 UTC — volatile BTC, fast WS updates, trending moves.

Each profile overrides a subset of env-vars in-process via os.environ so that
all downstream code reading from os.getenv() automatically picks up the new values.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Final

NIGHT_START_UTC: Final[int] = int(os.getenv("HFT_NIGHT_START_UTC_HOUR", "23"))
NIGHT_END_UTC: Final[int] = int(os.getenv("HFT_NIGHT_END_UTC_HOUR", "6"))
DAWN_END_UTC: Final[int] = int(os.getenv("HFT_DAWN_END_UTC_HOUR", "9"))

# Parameters that differ between profiles.
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
    # Low-speed edge multiplier: relax so entries at edge 6+ pts pass.
    "HFT_ENTRY_LOW_SPEED_EDGE_MULT": "1.5",
    "HFT_ENTRY_LOW_SPEED_ABS": "0.5",
    # Speed floor: disabled so signals fire even when BTC is flat.
    "HFT_SPEED_FLOOR": "0.0",
    # Soft-flow max latency: WS can be 1-2 s stale → raise.
    "HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS": "2000.0",
    # Imbalance filter: moderate — night entries allowed with weaker book.
    "HFT_CEX_IMBALANCE_UP_MIN": "0.60",
    "HFT_CEX_IMBALANCE_DOWN_MAX": "0.40",
}

_DAWN: dict[str, str] = {
    # Transitional: BTC waking up, speed occasionally present but often zero.
    "HFT_ENTRY_UP_SPEED_MIN": "0.0",
    "HFT_ENTRY_DOWN_SPEED_MAX": "0.0",
    # Slightly tighter latency than night but still allows sparse WS.
    "HFT_ENTRY_MAX_LATENCY_MS": "2000.0",
    # Z-score: disabled — early morning z-score still chaotic.
    "HFT_ENTRY_ZSCORE_TREND_ENABLED": "0",
    "HFT_ENTRY_ZSCORE_STRICT_TICKS": "1",
    # Moderate low-speed protection.
    "HFT_ENTRY_LOW_SPEED_EDGE_MULT": "1.5",
    "HFT_ENTRY_LOW_SPEED_ABS": "0.5",
    "HFT_SPEED_FLOOR": "0.0",
    "HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS": "1500.0",
    # Tighter imbalance: dawn entries need stronger book confirmation.
    # Avoids low-imbalance DOWN traps (imb=0.03/0.06 seen causing -$0.45/-$0.59).
    "HFT_CEX_IMBALANCE_UP_MIN": "0.65",
    "HFT_CEX_IMBALANCE_DOWN_MAX": "0.35",
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
    # Strong low-speed protection for choppy intraday micro-sessions.
    "HFT_ENTRY_LOW_SPEED_EDGE_MULT": "2.0",
    "HFT_ENTRY_LOW_SPEED_ABS": "1.0",
    # Speed floor: require at least minimal momentum.
    "HFT_SPEED_FLOOR": "0.02",
    # Soft-flow max latency: daytime feeds are fast.
    "HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS": "800.0",
    # Standard imbalance gates for daytime liquid book.
    "HFT_CEX_IMBALANCE_UP_MIN": "0.70",
    "HFT_CEX_IMBALANCE_DOWN_MAX": "0.30",
}

_CURRENT_PROFILE: str | None = None


def _utc_hour() -> int:
    """Return current UTC hour (0-23)."""
    return datetime.now(timezone.utc).hour


def current_profile_name() -> str:
    """Return 'night', 'dawn', or 'day' based on current UTC hour."""
    h = _utc_hour()
    # Night: wraps midnight, e.g. 23:00–05:59.
    if NIGHT_START_UTC < NIGHT_END_UTC:
        night = NIGHT_START_UTC <= h < NIGHT_END_UTC
    else:
        night = h >= NIGHT_START_UTC or h < NIGHT_END_UTC
    if night:
        return "night"
    # Dawn: NIGHT_END_UTC .. DAWN_END_UTC (e.g. 06:00–08:59).
    if NIGHT_END_UTC <= h < DAWN_END_UTC:
        return "dawn"
    return "day"


def _apply(profile: dict[str, str], name: str) -> None:
    """Write profile values into os.environ."""
    for key, val in profile.items():
        os.environ[key] = val
    logging.info(
        "[SESSION] Profile applied: %s — %d overrides active.",
        name.upper(),
        len(profile),
    )
    for key, val in profile.items():
        logging.debug("  [SESSION] %s=%s", key, val)


def apply_profile(force: bool = False) -> str:
    """Apply session profile to os.environ if the window changed.

    Returns the profile name now active ('night', 'dawn', or 'day').
    """
    global _CURRENT_PROFILE
    name = current_profile_name()
    if name == _CURRENT_PROFILE and not force:
        return name
    if name == "night":
        _apply(_NIGHT, "night")
        logging.info(
            "🌙 [SESSION] NIGHT mode (UTC %02d:00–%02d:00). "
            "Speed gates OFF, latency 2500ms, z-score OFF, imb ≥0.40/≤0.60.",
            NIGHT_START_UTC, NIGHT_END_UTC,
        )
    elif name == "dawn":
        _apply(_DAWN, "dawn")
        logging.info(
            "🌅 [SESSION] DAWN mode (UTC %02d:00–%02d:00). "
            "Speed gates OFF, latency 2000ms, z-score OFF, imb ≥0.35/≤0.65 (stricter).",
            NIGHT_END_UTC, DAWN_END_UTC,
        )
    else:
        _apply(_DAY, "day")
        logging.info(
            "☀️  [SESSION] DAY mode (UTC %02d:00–%02d:00). "
            "Speed gates ±2 pts/s, latency 1350ms, z-score STRICT_TICKS=2, imb ≥0.30/≤0.70.",
            DAWN_END_UTC, NIGHT_START_UTC,
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
