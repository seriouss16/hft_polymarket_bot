"""Simple test for incremental ADX update logic."""

from __future__ import annotations

import unittest

import numpy as np

from ml.indicators import IncrementalADX, compute_adx_last


class TestIncrementalADXUpdateLogic(unittest.TestCase):
    """Test the incremental ADX update pattern used in engine."""

    def test_incremental_update_only_adds_new_prices(self):
        """Simulate the engine's incremental update logic and verify only new prices are processed."""
        period = 14
        adx_tick_len = 60

        # Create a mock ADX calculator
        adx_calc = IncrementalADX(period=period)

        # Simulate price history over several ticks
        all_prices = list(range(100, 200))  # 100 prices

        # Track how many times update is called
        update_count = [0]
        original_update = adx_calc.update

        def counting_update(high, low, close):
            update_count[0] += 1
            return original_update(high, low, close)

        adx_calc.update = counting_update

        # Simulate first tick: process prices 0-59 (first 60 prices)
        px_adx = all_prices[:adx_tick_len]
        last_processed_index = -1
        current_len = len(px_adx)

        # Process all prices from 0 to 59
        for i in range(current_len):
            price = px_adx[i]
            if i == 0:
                high_i = low_i = close_i = price
            else:
                start = max(0, i - period + 1)
                window = px_adx[start : i + 1]
                high_i = float(np.max(window))
                low_i = float(np.min(window))
                close_i = price
            adx_calc.update(high_i, low_i, close_i)
            last_processed_index = i

        count_after_first = update_count[0]
        # Should have processed 60 prices
        self.assertEqual(count_after_first, 60)

        # Get ADX value
        adx1 = adx_calc.get_last_adx()

        # Simulate second tick: add one more price (index 60)
        px_adx = all_prices[: adx_tick_len + 1]
        current_len = len(px_adx)

        # Process only new prices (incremental)
        if current_len > last_processed_index:
            for i in range(last_processed_index + 1, current_len):
                price = px_adx[i]
                start = max(0, i - period + 1)
                window = px_adx[start : i + 1]
                high_i = float(np.max(window))
                low_i = float(np.min(window))
                close_i = price
                adx_calc.update(high_i, low_i, close_i)
            last_processed_index = current_len - 1

        count_after_second = update_count[0]
        new_updates = count_after_second - count_after_first

        # Should have processed only 1 new price (incremental!)
        self.assertEqual(new_updates, 1, f"Expected 1 update, got {new_updates}")

        # ADX should still be valid and match full recomputation
        adx2 = adx_calc.get_last_adx()
        expected_adx = compute_adx_last(np.array(px_adx), period=period)
        self.assertAlmostEqual(adx2, expected_adx, places=3)

    def test_history_shrink_triggers_reset(self):
        """When price history shrinks, ADX calculator should reset."""
        period = 14
        adx_calc = IncrementalADX(period=period)

        # Build up some history
        prices1 = list(range(100, 160))
        for p in prices1:
            adx_calc.update(p, p, p)

        # Simulate shrink
        last_processed = 59
        current_len = 10

        if current_len < last_processed:
            adx_calc.reset(period=period)
            last_processed = -1

        # After reset, ADX should be NaN
        self.assertTrue(np.isnan(adx_calc.get_last_adx()))

    def test_window_reconstruction_correctness(self):
        """The incremental update should match full recomputation over multiple ticks."""
        period = 14
        adx_tick_len = 60

        # Generate random prices
        np.random.seed(42)
        all_prices = np.linspace(100.0, 110.0, 100) + np.random.normal(0, 0.2, 100)

        # Method 1: Full recomputation
        expected = compute_adx_last(all_prices[:adx_tick_len], period=period)

        # Method 2: Incremental simulation
        adx_calc = IncrementalADX(period=period)
        last_processed = -1

        # Simulate first tick: process all prices from 0 to 59
        px_adx = all_prices[:adx_tick_len]
        current_len = len(px_adx)
        for i in range(current_len):
            price = px_adx[i]
            start = max(0, i - period + 1)
            window = px_adx[start : i + 1]
            high_i = float(np.max(window))
            low_i = float(np.min(window))
            close_i = price
            adx_calc.update(high_i, low_i, close_i)
        last_processed = current_len - 1
        result1 = adx_calc.get_last_adx()

        # Should match expected
        self.assertAlmostEqual(result1, expected, places=3)

        # Simulate adding more prices incrementally
        for tick in range(1, 5):
            new_idx = adx_tick_len + tick
            px_adx = all_prices[:new_idx]
            current_len = len(px_adx)

            if current_len > last_processed:
                for i in range(last_processed + 1, current_len):
                    price = px_adx[i]
                    start = max(0, i - period + 1)
                    window = px_adx[start : i + 1]
                    high_i = float(np.max(window))
                    low_i = float(np.min(window))
                    close_i = price
                    adx_calc.update(high_i, low_i, close_i)
                last_processed = current_len - 1

        # Final result should match full recomputation on all prices
        final_expected = compute_adx_last(all_prices[:new_idx], period=period)
        final_result = adx_calc.get_last_adx()
        self.assertAlmostEqual(final_result, final_expected, places=3)


if __name__ == "__main__":
    unittest.main()
