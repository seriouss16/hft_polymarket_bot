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

Profile key sets live in config files (not hardcoded here):
  - hft_bot/config/runtime_night.env
  - hft_bot/config/runtime_day.env

Important: switching profile only *sets* keys listed in that profile. Keys present
in NIGHT but missing from DAY would otherwise stay at night values in os.environ
after night→day — DAY must repeat any parameter that must differ from night
(edge, skew, min ask, slope filter, etc.).
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

NIGHT_START_UTC: Final[int] = int(os.getenv("HFT_NIGHT_START_UTC_HOUR"))
NIGHT_END_UTC: Final[int] = int(os.getenv("HFT_NIGHT_END_UTC_HOUR"))

_CONFIG_DIR: Final[Path] = Path(__file__).resolve().parent.parent / "config"
_RUNTIME_NIGHT_ENV: Final[Path] = _CONFIG_DIR / "runtime_night.env"
_RUNTIME_DAY_ENV: Final[Path] = _CONFIG_DIR / "runtime_day.env"


def _parse_profile_env_file(path: Path) -> dict[str, str]:
    """Parse KEY=VALUE lines into a dict (same rules as bot._load_env_file)."""
    if not path.is_file():
        raise FileNotFoundError(f"Session profile file missing: {path}")
    out: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if not key:
            continue
        out[key] = val
    return out


# ---------------------------------------------------------------------------
# Profile parameter sets — keys must match HFTEngine.__init__ env reads.
# Values must be strings (same format as env vars).
# ---------------------------------------------------------------------------

_NIGHT: dict[str, str] = _parse_profile_env_file(_RUNTIME_NIGHT_ENV)
_DAY: dict[str, str] = _parse_profile_env_file(_RUNTIME_DAY_ENV)

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
    """Return 'night' or 'day' for the active session profile.

    Manual override via .env (evaluated each call so hot-edits work):
      DAY_MODE=1  NIGHT_MODE=0  → forced DAY, no auto-switching.
      DAY_MODE=0  NIGHT_MODE=1  → forced NIGHT, no auto-switching.
      DAY_MODE=0  NIGHT_MODE=0  → automatic (UTC time + weekend logic).
      DAY_MODE=1  NIGHT_MODE=1  → automatic (both set = no override).
    """
    day_mode = os.getenv("DAY_MODE") or "0"
    night_mode = os.getenv("NIGHT_MODE") or "0"
    if day_mode == "1" and night_mode != "1":
        return "day"
    if night_mode == "1" and day_mode != "1":
        return "night"
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
    "night": "speed=0 zscore=OFF min_ask↓ stale≤3800ms SL/TP/RSI=reaction=DAY baseline (weekends all day)",
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
    day_mode = os.getenv("DAY_MODE") or "0"
    night_mode = os.getenv("NIGHT_MODE") or "0"
    forced = (day_mode == "1") != (night_mode == "1")
    label = "[FORCED] " if forced else ("WEEKEND " if _is_weekend() else "")
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
