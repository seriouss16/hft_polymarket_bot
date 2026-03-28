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
    # Lower entry threshold for night: smaller edge moves are profitable when volatility is low.
    "HFT_BUY_EDGE": "3.0",
    "HFT_NOISE_EDGE": "0.5",
    # Poly WS is sparse: allow ~3.8 s staleness before blocking entries (logs showed
    # 3.0–3.5 s spikes around slot changes).
    "HFT_ENTRY_MAX_LATENCY_MS": "3800.0",
    "HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS": "2200.0",
    # Cross-feed skew is recv-order noise, not true clock skew; 0 ms blocks almost
    # all entries when CB and Poly WS drift. Night uses a wide tolerance.
    "HFT_ENTRY_MAX_SKEW_MS": "2000.0",
    # Allow 5c asks when the book pins cheap contracts (0.08 blocked valid signals).
    "HFT_ENTRY_MIN_ASK_UP": "0.05",
    "HFT_ENTRY_MIN_ASK_DOWN": "0.05",
    # Late-slot books often pin UP/DOWN near 0.98; 0.97 blocked valid signals in logs.
    "HFT_ENTRY_MAX_ASK_UP": "0.97",
    "HFT_ENTRY_MAX_ASK_DOWN": "0.97",
    # Slightly wider than day; spread_gate still applies per profile.
    "HFT_MAX_ENTRY_SPREAD": "0.08",
    # Z-score is meaningless at near-zero speed.
    "HFT_ENTRY_ZSCORE_TREND_ENABLED": "0",
    "HFT_ENTRY_ZSCORE_STRICT_TICKS": "1",
    # Very relaxed low-speed multiplier — entries must fire at minimal momentum.
    "HFT_ENTRY_LOW_SPEED_EDGE_MULT": "1.0",
    "HFT_ENTRY_LOW_SPEED_ABS": "0.2",
    # Phase router: trend "speed" is BTC pts/s noise in thousands on 5m slots — thresholds
    # 500/800 forced latency almost always (volatile_speed). Align with observed logs.
    "HFT_PHASE_SOFT_MIN_ABS_EDGE": "3.0",
    "HFT_PHASE_SOFT_MAX_ABS_SPEED": "15000.0",
    "HFT_PHASE_SOFT_MAX_ABS_EDGE": "35.0",
    "HFT_PHASE_VOLATILE_MIN_ABS_SPEED": "25000.0",
    "HFT_PHASE_VOLATILE_MIN_ABS_EDGE": "50.0",
    "HFT_PHASE_SOFT_MIN_TREND_AGE_SEC": "1.0",
    # SL/TP tuned for range: hold short, exit on small reversal.
    # Tighter SL to cut losers faster, keep TP for scalping.
    "HFT_POLY_SL_MOVE": "0.0120",
    "HFT_POLY_TP_MOVE": "0.0035",
    "HFT_MIN_HOLD_SEC": "3.0",
    # Very short cooldown — range recovers fast after a loss.
    "LOSS_COOLDOWN_SEC": "3",
    # Anchor filter: counter-direction needs a smaller confirmation delta at night
    # because BTC barely moves; even 0.02% deviation from anchor is significant.
    "HFT_ANCHOR_COUNTER_MIN_DELTA_PCT": "0.0002",
    # Reaction score night profile: short periods to warm up quickly on sparse ticks;
    # RSI dominates (0.60) because MA/MACD are noisy until history fills up.
    # MA scale 0.0003 = 0.03%: BTC moves cents at night, not dollars.
    # MACD hist scale 3.0 USD: EMA(8)-EMA(17) histogram is tiny in calm markets.
    "HFT_REACTION_MA_PERIOD": "14",
    "HFT_REACTION_MACD_FAST": "8",
    "HFT_REACTION_MACD_SLOW": "17",
    "HFT_REACTION_MACD_SIGNAL": "6",
    "HFT_REACTION_MA_REL_SCALE": "0.0003",
    "HFT_REACTION_MACD_HIST_SCALE": "3.0",
    "HFT_REACTION_W_RSI": "0.60",
    "HFT_REACTION_W_MA": "0.25",
    "HFT_REACTION_W_MACD": "0.15",
    # RSI slope-filter: night uses relaxed slope (0.0) because Rx changes slowly
    # on sparse Poly ticks.  Entry bands themselves enforce directionality.
    # UP_ENTRY_MAX=55: enter UP only when Rx < 55 (BTC not overbought).
    # DOWN_ENTRY_MIN=45: enter DOWN only when Rx > 45 (BTC not oversold).
    "HFT_RSI_UP_ENTRY_MAX": "55.0",
    "HFT_RSI_UP_SLOPE_MIN": "0.0",
    "HFT_RSI_DOWN_ENTRY_MIN": "45.0",
    "HFT_RSI_DOWN_SLOPE_MAX": "0.0",
    # Regime filter: night WR is naturally lower due to random reversals in flat BTC.
    # Lower good-regime threshold (0.38) and raise bad-regime floor (0.28) so a
    # small losing streak does not lock the bot for hours.  Shorter memory window
    # (6 trades) reacts faster to regime recovery after a bad burst.
    # Cooldown 30 s instead of 60 s: night slots are only 5 min, losing 60 s is costly.
    "HFT_GOOD_REGIME_WINRATE": "0.38",
    "HFT_BAD_REGIME_WINRATE": "0.28",
    "HFT_RECENT_TRADES_FOR_REGIME": "6",
    "HFT_REGIME_COOLDOWN_SEC": "30",
    # DD gate: night balance is small; allow up to 30% drawdown before meta-blocking.
    # At $4 capital, 15% DD = $0.60 — too tight for 6–12 trades in a quiet session.
    "MAX_DRAWDOWN_PCT": "0.30",
}

_DAY: dict[str, str] = {
    # Weekday day: BTC shows occasional directional momentum.
    "HFT_ENTRY_UP_SPEED_MIN": "2.0",
    "HFT_ENTRY_DOWN_SPEED_MAX": "-2.0",
    "HFT_SPEED_FLOOR": "0.02",
    # Tighter staleness gate — Poly WS is fast during business hours.
    "HFT_ENTRY_MAX_LATENCY_MS": "1350.0",
    "HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS": "800.0",
    # Phase router: stricter than night; must reset env when switching from weekend night.
    "HFT_PHASE_SOFT_MIN_ABS_EDGE": "4.0",
    "HFT_PHASE_SOFT_MAX_ABS_SPEED": "400.0",
    "HFT_PHASE_SOFT_MAX_ABS_EDGE": "18.0",
    "HFT_PHASE_VOLATILE_MIN_ABS_SPEED": "600.0",
    "HFT_PHASE_VOLATILE_MIN_ABS_EDGE": "35.0",
    "HFT_PHASE_SOFT_MIN_TREND_AGE_SEC": "1.0",
    "HFT_ENTRY_MAX_ASK_UP": "0.97",
    "HFT_ENTRY_MAX_ASK_DOWN": "0.97",
    "HFT_MAX_ENTRY_SPREAD": "0.05",
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
    # Regime filter: day session has higher WR potential; standard thresholds.
    # Memory of 8 trades and 60 s cooldown are correct for active hours.
    "HFT_GOOD_REGIME_WINRATE": "0.42",
    "HFT_BAD_REGIME_WINRATE": "0.35",
    "HFT_RECENT_TRADES_FOR_REGIME": "8",
    "HFT_REGIME_COOLDOWN_SEC": "60",
    # DD gate: tighter during active sessions where drawdowns recover faster.
    "MAX_DRAWDOWN_PCT": "0.20",
    # Anchor filter: intraday moves faster, require stronger confirmation
    # before trading counter to the slot opening price.
    "HFT_ANCHOR_COUNTER_MIN_DELTA_PCT": "0.0005",
    # Reaction score day profile: standard periods, MACD/MA get more weight
    # because trends are real and histograms are meaningful in active hours.
    # MA scale 0.0006 = 0.06%: intraday BTC moves are larger relative to price.
    # MACD hist scale 15.0 USD: EMA(12)-EMA(26) histogram can reach 10-30 USD intraday.
    "HFT_REACTION_MA_PERIOD": "21",
    "HFT_REACTION_MACD_FAST": "12",
    "HFT_REACTION_MACD_SLOW": "26",
    "HFT_REACTION_MACD_SIGNAL": "9",
    "HFT_REACTION_MA_REL_SCALE": "0.0006",
    "HFT_REACTION_MACD_HIST_SCALE": "15.0",
    "HFT_REACTION_W_RSI": "0.45",
    "HFT_REACTION_W_MA": "0.30",
    "HFT_REACTION_W_MACD": "0.25",
    # RSI slope-filter for day: require positive slope for UP, negative for DOWN.
    # Day BTC moves are real and directional — slope confirms momentum direction.
    # Tighter entry ceiling (UP_ENTRY_MAX=50) for more selective UP entries at day.
    "HFT_RSI_UP_ENTRY_MAX": "52.0",
    "HFT_RSI_UP_SLOPE_MIN": "0.3",
    "HFT_RSI_DOWN_ENTRY_MIN": "48.0",
    "HFT_RSI_DOWN_SLOPE_MAX": "-0.3",
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
    """Return 'night' or 'day' for the active session profile.

    Manual override via .env (evaluated each call so hot-edits work):
      DAY_MODE=1  NIGHT_MODE=0  → forced DAY, no auto-switching.
      DAY_MODE=0  NIGHT_MODE=1  → forced NIGHT, no auto-switching.
      DAY_MODE=0  NIGHT_MODE=0  → automatic (UTC time + weekend logic).
      DAY_MODE=1  NIGHT_MODE=1  → automatic (both set = no override).
    """
    day_mode = os.getenv("DAY_MODE", "0").strip()
    night_mode = os.getenv("NIGHT_MODE", "0").strip()
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
    day_mode = os.getenv("DAY_MODE", "0").strip()
    night_mode = os.getenv("NIGHT_MODE", "0").strip()
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
