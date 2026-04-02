"""Tests for technical indicators and reaction score."""

from __future__ import annotations

from collections import deque
import unittest

import numpy as np

from ml.indicators import (
    IncrementalADX,
    IncrementalRSI,
    compute_adx_last,
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

    def test_adx_short_series_returns_nan(self):
        """ADX needs enough bars; short history yields NaN."""
        p = np.linspace(80000.0, 80100.0, 10)
        a = compute_adx_last(p, period=14)
        self.assertFalse(np.isfinite(a))

    def test_adx_on_strong_uptrend_is_finite_and_positive(self):
        """ADX is in 0–100 on a long synthetic uptrend."""
        p = np.linspace(80000.0, 82000.0, 120)
        a = float(compute_adx_last(p, period=14))
        self.assertTrue(np.isfinite(a))
        self.assertGreaterEqual(a, 0.0)
        self.assertLessEqual(a, 100.0)


class TestIncrementalRSI(unittest.TestCase):
    """Test IncrementalRSI matches compute_rsi and handles edge cases."""

    def test_incremental_rsi_matches_full_history(self):
        """Incremental RSI should match compute_rsi on same data."""
        period = 14
        prices = np.linspace(100.0, 110.0, 100) + np.random.normal(0, 0.5, 100)
        inc_rsi = IncrementalRSI(period=period)
        expected = compute_rsi(prices, period=period)
        
        # Feed all prices
        for p in prices:
            inc_rsi.update(p)
        
        result = inc_rsi.get_last_rsi()
        # Allow tolerance for floating point accumulation differences (1 decimal place is sufficient for trading)
        self.assertAlmostEqual(result, expected, places=1)

    def test_incremental_rsi_warmup_returns_50(self):
        """During warm-up (fewer than period deltas), RSI should return 50.0."""
        inc_rsi = IncrementalRSI(period=14)
        result = inc_rsi.update(100.0)
        self.assertEqual(result, 50.0)
        result = inc_rsi.update(101.0)
        self.assertEqual(result, 50.0)

    def test_incremental_rsi_constant_price(self):
        """RSI should be 100 for constant price (no down moves) after warm-up."""
        inc_rsi = IncrementalRSI(period=5)
        constant = 100.0
        # Feed more than period prices
        for _ in range(10):
            inc_rsi.update(constant)
        result = inc_rsi.get_last_rsi()
        self.assertAlmostEqual(result, 100.0, places=1)

    def test_incremental_rsi_single_tick(self):
        """Single tick after warm-up should produce valid RSI."""
        inc_rsi = IncrementalRSI(period=3)
        prices = [100.0, 101.0, 102.0, 103.0]
        for p in prices:
            inc_rsi.update(p)
        result = inc_rsi.get_last_rsi()
        self.assertTrue(0.0 <= result <= 100.0)
        self.assertTrue(np.isfinite(result))

    def test_incremental_rsi_reset(self):
        """Reset should clear state and allow reuse."""
        inc_rsi = IncrementalRSI(period=5)
        prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
        for p in prices:
            inc_rsi.update(p)
        result1 = inc_rsi.get_last_rsi()
        
        inc_rsi.reset()
        self.assertEqual(inc_rsi.get_last_rsi(), 50.0)
        
        for p in prices:
            inc_rsi.update(p)
        result2 = inc_rsi.get_last_rsi()
        self.assertAlmostEqual(result1, result2, places=5)

    def test_incremental_rsi_reset_with_new_period(self):
        """Reset with new period should change the period."""
        inc_rsi = IncrementalRSI(period=5)
        self.assertEqual(inc_rsi.period, 5)
        inc_rsi.reset(period=10)
        self.assertEqual(inc_rsi.period, 10)
        self.assertFalse(inc_rsi._initialized)

    def test_incremental_rsi_reset_mid_warmup_replaces_warmup_deque(self):
        """reset(period=...) must not keep _warmup_deltas with old maxlen."""
        inc_rsi = IncrementalRSI(period=5)
        inc_rsi.update(100.0)
        inc_rsi.update(101.0)
        self.assertTrue(hasattr(inc_rsi, "_warmup_deltas"))
        self.assertEqual(inc_rsi._warmup_deltas.maxlen, 5)
        inc_rsi.reset(period=3)
        self.assertFalse(hasattr(inc_rsi, "_warmup_deltas"))
        inc_rsi.update(100.0)
        inc_rsi.update(101.0)
        self.assertEqual(inc_rsi._warmup_deltas.maxlen, 3)


class TestIncrementalADX(unittest.TestCase):
    """Test IncrementalADX matches compute_adx_last and handles edge cases."""

    def test_incremental_adx_matches_full_history(self):
        """Incremental ADX should match compute_adx_last on same data."""
        period = 14
        # Need enough prices: at least 2*period + 1 for compute_adx_last
        n = 2 * period + 10
        prices = np.linspace(100.0, 110.0, n) + np.random.normal(0, 0.3, n)
        
        inc_adx = IncrementalADX(period=period)
        expected = compute_adx_last(prices, period=period)
        
        # Feed prices with synthetic OHLC (rolling high/low)
        window = deque(maxlen=period)
        for i, price in enumerate(prices):
            window.append(price)
            if i == 0:
                high = low = close = price
            else:
                high = max(window)
                low = min(window)
                close = price
            inc_adx.update(high, low, close)
        
        result = inc_adx.get_last_adx()
        self.assertAlmostEqual(result, expected, places=3)

    def test_incremental_adx_warmup_returns_nan(self):
        """During warm-up (fewer than period bars), ADX should return NaN."""
        inc_adx = IncrementalADX(period=5)
        result = inc_adx.update(100.0, 99.0, 100.0)
        self.assertTrue(np.isnan(result))
        
        # Add a few more bars but still less than period
        for _ in range(3):
            result = inc_adx.update(101.0, 100.0, 101.0)
            self.assertTrue(np.isnan(result))

    def test_incremental_adx_strong_uptrend(self):
        """ADX should be finite and positive on a strong trend."""
        period = 14
        prices = np.linspace(100.0, 120.0, 100)  # Strong uptrend
        inc_adx = IncrementalADX(period=period)
        
        window = deque(maxlen=period)
        for i, price in enumerate(prices):
            window.append(price)
            if i == 0:
                high = low = close = price
            else:
                high = max(window)
                low = min(window)
                close = price
            inc_adx.update(high, low, close)
        
        result = inc_adx.get_last_adx()
        self.assertTrue(np.isfinite(result))
        self.assertGreater(result, 10.0)  # Strong trend should have ADX > 10

    def test_incremental_adx_reset(self):
        """Reset should clear state and allow reuse."""
        period = 10
        prices = np.linspace(100.0, 110.0, 50)
        inc_adx = IncrementalADX(period=period)
        
        window = deque(maxlen=period)
        for i, price in enumerate(prices):
            window.append(price)
            if i == 0:
                high = low = close = price
            else:
                high = max(window)
                low = min(window)
                close = price
            inc_adx.update(high, low, close)
        
        result1 = inc_adx.get_last_adx()
        self.assertTrue(np.isfinite(result1))
        
        inc_adx.reset()
        self.assertTrue(np.isnan(inc_adx.get_last_adx()))
        
        # Re-feed data
        window = deque(maxlen=period)
        for i, price in enumerate(prices):
            window.append(price)
            if i == 0:
                high = low = close = price
            else:
                high = max(window)
                low = min(window)
                close = price
            inc_adx.update(high, low, close)
        
        result2 = inc_adx.get_last_adx()
        self.assertAlmostEqual(result1, result2, places=3)

    def test_incremental_adx_reset_with_new_period(self):
        """Reset with new period should change the period."""
        inc_adx = IncrementalADX(period=5)
        self.assertEqual(inc_adx.period, 5)
        inc_adx.reset(period=10)
        self.assertEqual(inc_adx.period, 10)
        self.assertIsNone(inc_adx._atr)

    def test_incremental_adx_reset_rebuilds_deque_maxlen(self):
        """reset(period=...) must replace TR/DM/DX buffers with new maxlen."""
        inc_adx = IncrementalADX(period=14)
        self.assertEqual(inc_adx._tr_buffer.maxlen, 14)
        inc_adx.reset(period=7)
        self.assertEqual(inc_adx.period, 7)
        self.assertEqual(inc_adx._tr_buffer.maxlen, 7)
        self.assertEqual(inc_adx._pdm_buffer.maxlen, 7)
        self.assertEqual(inc_adx._mdm_buffer.maxlen, 7)
        self.assertEqual(inc_adx._dx_buffer.maxlen, 7)
        self.assertEqual(len(inc_adx._tr_buffer), 0)

    def test_incremental_adx_constant_price(self):
        """ADX should be low (near 0) on flat price after warm-up."""
        period = 5
        constant = 100.0
        inc_adx = IncrementalADX(period=period)
        
        window = deque(maxlen=period)
        for i in range(2 * period):
            window.append(constant)
            if i == 0:
                high = low = close = constant
            else:
                high = max(window)
                low = min(window)
                close = constant
            inc_adx.update(high, low, close)
        
        result = inc_adx.get_last_adx()
        # Flat market should have very low ADX
        self.assertLess(result, 5.0)


if __name__ == "__main__":
    unittest.main()
