"""Tests for technical indicators and reaction score."""

from __future__ import annotations

import unittest

import numpy as np

from ml.indicators import (
    compute_macd_last,
    compute_reaction_score,
    compute_rsi,
    ema_series,
)


class TestReactionAndMacd(unittest.TestCase):
    """Exercise reaction score, EMA, MACD, and RSI helpers."""

    def test_compute_reaction_score_clamped_to_0_100(self):
        """Return value stays in 0–100 for extreme inputs."""
        r = compute_reaction_score(
            150.0,
            100.0,
            100.0,
            1000.0,
            ma_rel_scale=0.001,
            macd_hist_scale=1.0,
            w_rsi=0.45,
            w_ma=0.30,
            w_macd=0.25,
        )
        self.assertGreaterEqual(r, 0.0)
        self.assertLessEqual(r, 100.0)

    def test_compute_reaction_score_neutral_macd_is_mid(self):
        """When MACD histogram is zero, MACD term contributes ~50 toward the blend."""
        r = compute_reaction_score(
            50.0,
            100.0,
            100.0,
            0.0,
            ma_rel_scale=0.001,
            macd_hist_scale=25.0,
            w_rsi=0.0,
            w_ma=0.0,
            w_macd=1.0,
        )
        self.assertGreaterEqual(r, 49.0)
        self.assertLessEqual(r, 51.0)

    def test_ema_series_length_matches_input(self):
        """EMA series length matches input length."""
        p = np.linspace(100.0, 110.0, 50)
        e = ema_series(p, 12)
        self.assertEqual(len(e), len(p))

    def test_macd_last_on_trending_series_is_finite(self):
        """MACD outputs are finite on a synthetic uptrend."""
        p = np.linspace(80000.0, 80500.0, 80)
        m, s, h = compute_macd_last(p, fast=12, slow=26, signal=9)
        self.assertTrue(np.isfinite(m) and np.isfinite(s) and np.isfinite(h))

    def test_rsi_on_oscillating_series_is_finite(self):
        """RSI is finite on a mildly oscillating series."""
        p = 1.0 + 0.01 * np.sin(np.linspace(0.0, 4.0, 40))
        r = float(compute_rsi(p, period=14))
        self.assertTrue(np.isfinite(r))
        self.assertGreaterEqual(r, 0.0)
        self.assertLessEqual(r, 100.0)


if __name__ == "__main__":
    unittest.main()
