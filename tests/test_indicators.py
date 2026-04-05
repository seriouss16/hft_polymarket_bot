"""Tests for technical indicators and reaction score."""

from __future__ import annotations

import unittest
from collections import deque

import numpy as np

from data.aggregator import IncrementalZScore
from ml.indicators import (IncrementalADX, IncrementalRSI, compute_adx_last,
                           compute_macd_last, compute_reaction_score,
                           compute_rsi, ema_series)


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


class TestIncrementalZScore(unittest.TestCase):
    """Test IncrementalZScore matches full-window z-score and handles edge cases."""

    def _compute_zscore_direct(self, values: list[float], window: int) -> float:
        """Helper: compute z-score using numpy on last window values."""
        if len(values) < 2:
            return 0.0
        window_vals = values[-window:] if len(values) > window else values
        arr = np.array(window_vals, dtype=np.float64)
        if arr.size < 2:
            return 0.0
        mean = float(arr.mean())
        std = float(arr.std()) + 1e-9
        return float((arr[-1] - mean) / std)

    def test_incremental_zscore_matches_full_window(self):
        """Incremental z-score should match full-window numpy computation."""
        window = 96
        np.random.seed(42)
        prices = 100.0 + np.random.normal(0, 1.0, 150)

        inc_z = IncrementalZScore(window_size=window)
        history = []

        for price in prices:
            inc_z.update(price)
            history.append(price)

            # Only compare when we have at least 2 values for both methods
            if len(history) >= 2:
                expected = self._compute_zscore_direct(history, window)
                result = inc_z.get_zscore()

                # When window is not yet full, both should still produce valid comparisons
                # After window fills, results should match closely
                if len(history) >= window:
                    self.assertFalse(np.isnan(result), f"NaN result at price {price}")
                    self.assertTrue(np.isfinite(result), f"Infinite result at price {price}")
                    self.assertAlmostEqual(
                        result, expected, places=5, msg=f"Mismatch at price {price}: inc={result}, full={expected}"
                    )

    def test_incremental_zscore_warmup_returns_0(self):
        """With fewer than 2 values, z-score should return 0.0."""
        inc_z = IncrementalZScore(window_size=96)
        inc_z.update(100.0)
        self.assertEqual(inc_z.get_zscore(), 0.0)

        inc_z.update(101.0)
        # Now we have 2 values, should produce a finite z-score
        z = inc_z.get_zscore()
        self.assertTrue(np.isfinite(z))
        self.assertNotEqual(z, 0.0)  # Should be non-zero for different values

    def test_incremental_zscore_constant_price(self):
        """Z-score should be 0 for constant price series (zero std)."""
        inc_z = IncrementalZScore(window_size=10)
        constant = 100.0
        for _ in range(15):
            inc_z.update(constant)
        z = inc_z.get_zscore()
        self.assertAlmostEqual(z, 0.0, places=5)

    def test_incremental_zscore_sliding_window(self):
        """Test that old values drop out of the window correctly."""
        window = 5
        inc_z = IncrementalZScore(window_size=window)

        # Feed exactly window values
        values1 = [100.0, 101.0, 102.0, 103.0, 104.0]
        for v in values1:
            inc_z.update(v)

        expected1 = self._compute_zscore_direct(values1, window)
        z1 = inc_z.get_zscore()
        self.assertAlmostEqual(z1, expected1, places=5)

        # Add more values to push out old ones
        values2 = [105.0, 106.0, 107.0]
        for v in values2:
            inc_z.update(v)

        # After adding 3 more, total 8 values, window should contain last 5: [103,104,105,106,107]
        all_values = values1 + values2
        expected2 = self._compute_zscore_direct(all_values, window)
        z2 = inc_z.get_zscore()
        self.assertAlmostEqual(z2, expected2, places=5)

    def test_incremental_zscore_numerical_stability_large_values(self):
        """Test stability with large price values (e.g., BTC at 80000)."""
        window = 50
        inc_z = IncrementalZScore(window_size=window)

        # Simulate BTC prices around 80000
        np.random.seed(42)
        prices = 80000.0 + np.random.normal(0, 100.0, 100)

        for price in prices:
            inc_z.update(price)
            z = inc_z.get_zscore()
            self.assertTrue(np.isfinite(z), f"Non-finite z-score at price {price}")
            # Z-score should be within reasonable range for a stable series
            self.assertLess(abs(z), 10.0, f"Extreme z-score: {z}")

    def test_incremental_zscore_reset(self):
        """Reset should clear state and allow reuse."""
        window = 10
        inc_z = IncrementalZScore(window_size=window)

        # Feed some data
        for i in range(20):
            inc_z.update(100.0 + i)

        z1 = inc_z.get_zscore()
        self.assertTrue(np.isfinite(z1))

        # Reset
        inc_z.reset()
        self.assertEqual(inc_z.count, 0)
        self.assertEqual(inc_z.sum_x, 0.0)
        self.assertEqual(inc_z.sum_x2, 0.0)
        self.assertEqual(inc_z.get_zscore(), 0.0)

        # Re-feed data and verify same result
        for i in range(20):
            inc_z.update(100.0 + i)
        z2 = inc_z.get_zscore()
        self.assertAlmostEqual(z1, z2, places=5)

    def test_incremental_zscore_single_value_after_warmup(self):
        """After warm-up, single value updates should produce valid z-scores."""
        window = 10
        inc_z = IncrementalZScore(window_size=window)

        # Fill window
        base = 100.0
        for _ in range(window):
            inc_z.update(base)

        # Z-score should be 0 (all same)
        z = inc_z.get_zscore()
        self.assertAlmostEqual(z, 0.0, places=5)

        # Add a spike
        inc_z.update(base + 10.0)
        z_spike = inc_z.get_zscore()
        self.assertTrue(np.isfinite(z_spike))
        self.assertGreater(z_spike, 0.0)  # Positive z-score for above-mean

    def test_incremental_zscore_negative_and_fractional_prices(self):
        """Test with fractional and negative values (if allowed by the system)."""
        window = 20
        inc_z = IncrementalZScore(window_size=window)

        values = [0.01, 0.02, 0.015, 0.03, 0.025] * 10

        for v in values:
            inc_z.update(v)

        z = inc_z.get_zscore()
        self.assertTrue(np.isfinite(z))

    def test_incremental_zscore_matches_full_on_random_walk(self):
        """Test against full computation on random walk data."""
        window = 96
        inc_z = IncrementalZScore(window_size=window)

        # Generate random walk
        np.random.seed(42)
        prices = 100.0 + np.cumsum(np.random.normal(0, 0.1, 300))
        history = []

        for price in prices:
            inc_z.update(price)
            history.append(price)

            if len(history) >= 2:
                expected = self._compute_zscore_direct(history, window)
                result = inc_z.get_zscore()
                # Only assert when window is full and expected is computed
                if len(history) >= window and np.isfinite(expected):
                    self.assertAlmostEqual(result, expected, places=5, msg=f"Mismatch at step {len(history)}")

    def test_incremental_zscore_fastpriceaggregator_integration(self):
        """Test FastPriceAggregator uses incremental z-score correctly."""
        import os

        from data.aggregator import FastPriceAggregator

        # Force incremental mode
        os.environ["HFT_USE_INCREMENTAL_ZSCORE"] = "1"
        # Use small window for testing
        os.environ["HFT_ZSCORE_WINDOW"] = "10"

        agg = FastPriceAggregator()
        self.assertTrue(agg.use_incremental)
        self.assertIsNotNone(agg._zscore_calculator)

        # Feed 60 prices to satisfy the 50-tick threshold in get_zscore
        prices = [100.0 + i * 0.1 for i in range(60)]
        for i, p in enumerate(prices):
            # Provide a timestamp to avoid asyncio loop requirement
            agg.update("coinbase", p, ts=float(i))

        z = agg.get_zscore()
        self.assertTrue(np.isfinite(z))

        # Compare with direct computation on the last 10 values
        expected = self._compute_zscore_direct(prices, 10)
        self.assertAlmostEqual(z, expected, places=5)

        # Clean up
        del os.environ["HFT_USE_INCREMENTAL_ZSCORE"]
        del os.environ["HFT_ZSCORE_WINDOW"]

    def test_incremental_zscore_disable_incremental_falls_back_to_original(self):
        """Test that disabling incremental mode falls back to original O(n) implementation."""
        import os

        from data.aggregator import FastPriceAggregator

        # Disable incremental mode
        os.environ["HFT_USE_INCREMENTAL_ZSCORE"] = "0"
        # Use small window for testing
        os.environ["HFT_ZSCORE_WINDOW"] = "10"

        agg = FastPriceAggregator()
        self.assertFalse(agg.use_incremental)
        self.assertIsNone(agg._zscore_calculator)

        # Feed 60 prices to satisfy the 50-tick threshold
        prices = [100.0 + i * 0.1 for i in range(60)]
        for i, p in enumerate(prices):
            # Provide a timestamp to avoid asyncio loop requirement
            agg.update("coinbase", p, ts=float(i))

        z = agg.get_zscore()
        self.assertTrue(np.isfinite(z))

        # Clean up
        del os.environ["HFT_USE_INCREMENTAL_ZSCORE"]
        del os.environ["HFT_ZSCORE_WINDOW"]


if __name__ == "__main__":
    unittest.main()
