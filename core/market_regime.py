"""Market regime auto-detector based on rolling price-speed and feed-latency metrics.

Classifies the current market into one of three dynamic regimes independent of
the calendar-based session profile:

  CALM    — BTC is flat; speed RMS is low; Poly WS updates are sparse.
             Typical: weekends, overnight hours.  Range-bound trading style.
  MIXED   — Moderate speed; transitional.  Dawn hours or uncertain sessions.
  ACTIVE  — BTC trending; speed RMS is high; WS updates are frequent.
             Typical: weekday day hours.  Momentum/latency-arb style.

The detector accumulates a short rolling window of (|speed|, latency_ms) samples
and emits a regime change event when the smoothed metrics cross configurable
thresholds.  It does NOT override session-profile parameters directly — it only
reports the detected regime so that bot.py can decide whether to apply an
additional override on top of the calendar profile.

Configuration (all read from os.getenv at instantiation time):
  REGIME_WINDOW_TICKS    — rolling window size (default 60 ticks ~ 15 s at 250 ms/tick).
  REGIME_CALM_SPEED_MAX  — |speed| RMS below this → CALM (default 0.5 pts/s).
  REGIME_ACTIVE_SPEED_MIN— |speed| RMS above this → ACTIVE (default 3.0 pts/s).
  REGIME_CALM_STALE_MIN  — median latency above this → reinforces CALM (default 1200 ms).
  REGIME_LOG_MIN_SEC     — minimum seconds between regime-change log messages (default 30).
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Literal

Regime = Literal["CALM", "MIXED", "ACTIVE"]


@dataclass
class RegimeState:
    """Snapshot of the current detected regime and its supporting metrics."""

    regime: Regime = "MIXED"
    speed_rms: float = 0.0
    stale_median_ms: float = 0.0
    samples: int = 0
    changed_at: float = field(default_factory=time.time)


class MarketRegimeDetector:
    """Detect market activity regime from rolling speed and latency samples.

    Usage::

        detector = MarketRegimeDetector()
        # Inside the main tick loop:
        changed = detector.update(speed=trend["speed"], latency_ms=latency_ms)
        if changed:
            strategy_hub.reload_profile_params()
    """

    def __init__(self) -> None:
        """Initialise rolling buffers and thresholds from environment."""
        self._window: int = int(os.getenv("REGIME_WINDOW_TICKS", "60"))
        self._calm_speed: float = float(os.getenv("REGIME_CALM_SPEED_MAX", "0.5"))
        self._active_speed: float = float(os.getenv("REGIME_ACTIVE_SPEED_MIN", "3.0"))
        self._calm_stale: float = float(os.getenv("REGIME_CALM_STALE_MIN_MS", "1200.0"))
        self._log_min_sec: float = float(os.getenv("REGIME_LOG_MIN_SEC", "30.0"))
        # Minimum ticks a new regime must be sustained before triggering a switch.
        # Prevents rapid CALM↔MIXED↔CALM flipping when metrics oscillate near
        # the threshold boundary.  Default: 10 ticks (~2.5 s at 250 ms/tick).
        self._hysteresis_ticks: int = int(os.getenv("REGIME_HYSTERESIS_TICKS", "10"))

        self._speeds: deque[float] = deque(maxlen=self._window)
        self._stales: deque[float] = deque(maxlen=self._window)

        self.state = RegimeState()
        self._last_log_ts: float = 0.0
        self._candidate_regime: Regime = "MIXED"
        self._candidate_ticks: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, speed: float, latency_ms: float) -> bool:
        """Feed one tick of market data and return True if regime changed.

        Args:
            speed:      Current BTC fast-price speed in pts/s (signed).
            latency_ms: Current Poly WS staleness in milliseconds.

        Returns:
            True when the regime transitioned since the last call.
        """
        # Cap instantaneous speed to avoid single-tick spikes (e.g. 107 pts/s
        # from a momentary BTC quote jump) from dominating the RMS window.
        # A single tick contributes at most ACTIVE_SPEED_MIN * 3 to the buffer.
        _speed_cap = self._active_speed * 3.0
        self._speeds.append(min(abs(speed), _speed_cap))
        self._stales.append(latency_ms)

        if len(self._speeds) < max(5, self._window // 4):
            return False

        speed_rms = math.sqrt(sum(s * s for s in self._speeds) / len(self._speeds))
        stale_median = sorted(self._stales)[len(self._stales) // 2]

        new_regime = self._classify(speed_rms, stale_median)

        self.state.speed_rms = speed_rms
        self.state.stale_median_ms = stale_median
        self.state.samples = len(self._speeds)

        # Hysteresis: require the candidate regime to be stable for
        # REGIME_HYSTERESIS_TICKS consecutive ticks before committing.
        if new_regime == self._candidate_regime:
            self._candidate_ticks += 1
        else:
            self._candidate_regime = new_regime
            self._candidate_ticks = 1

        if self._candidate_regime == self.state.regime:
            return False

        if self._candidate_ticks < self._hysteresis_ticks:
            return False

        old = self.state.regime
        self.state.regime = self._candidate_regime
        self.state.changed_at = time.time()
        self._log_change(old, self._candidate_regime, speed_rms, stale_median)
        return True

    def get_regime(self) -> Regime:
        """Return the current regime label."""
        return self.state.regime

    def diagnostics(self) -> dict[str, object]:
        """Return a dict of current metrics for logging."""
        return {
            "regime": self.state.regime,
            "speed_rms": round(self.state.speed_rms, 3),
            "stale_median_ms": round(self.state.stale_median_ms, 0),
            "samples": self.state.samples,
            "thresholds": {
                "calm_speed": self._calm_speed,
                "active_speed": self._active_speed,
                "calm_stale_ms": self._calm_stale,
            },
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _classify(self, speed_rms: float, stale_median: float) -> Regime:
        """Map rolling metrics to a regime label."""
        if speed_rms <= self._calm_speed:
            return "CALM"
        if speed_rms >= self._active_speed:
            return "ACTIVE"
        # Between thresholds: stale feed reinforces CALM.
        if stale_median >= self._calm_stale:
            return "CALM"
        return "MIXED"

    def _log_change(
        self,
        old: Regime,
        new: Regime,
        speed_rms: float,
        stale_ms: float,
    ) -> None:
        """Log regime transitions, rate-limited to REGIME_LOG_MIN_SEC."""
        now = time.time()
        if now - self._last_log_ts < self._log_min_sec:
            return
        self._last_log_ts = now
        emoji = {"CALM": "😴", "MIXED": "🌤️", "ACTIVE": "⚡"}
        logging.info(
            "%s [REGIME] %s → %s | speed_rms=%.3f pts/s stale_median=%.0f ms",
            emoji.get(new, "❓"),
            old,
            new,
            speed_rms,
            stale_ms,
        )
