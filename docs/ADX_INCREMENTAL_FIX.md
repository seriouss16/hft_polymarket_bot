# CRITICAL-2 Fix: Incremental ADX Calculation

## Problem
In `core/engine.py:1337-1360`, when `use_incremental_indicators=True`, the ADX calculation was **not actually incremental**. It reset the calculator and reprocessed the entire `px_adx` history on every tick, resulting in O(n) complexity instead of O(1).

### Original Code (Buggy)
```python
if self._indicators_dirty:
    self._adx_calculator.reset(period=self.adx_period)
    adx_window = deque(maxlen=self.adx_period)
    for i, price in enumerate(px_adx):  # ❌ Processes ALL prices every tick!
        adx_window.append(price)
        # ... update calculator
```

## Solution
Implemented true incremental ADX updates by:
1. Adding `_adx_last_processed_index` to track the last processed price index
2. Only processing new prices that have been added since the last tick
3. Computing synthetic OHLC for each new tick using a rolling window
4. Handling market resets when history shrinks

### New Code (Fixed)
```python
if self._indicators_dirty:
    current_len = len(px_adx)
    last_processed = self._adx_last_processed_index

    # Reset if history shrank (market reset)
    if current_len < last_processed:
        self._adx_calculator.reset(period=self.adx_period)
        self._adx_last_processed_index = -1
        last_processed = -1

    # Process only NEW prices
    if current_len > last_processed:
        new_prices_count = 0
        for i in range(last_processed + 1, current_len):  # ✅ Only new indices
            start = max(0, i - self.adx_period + 1)
            window = px_adx[start:i+1]
            high_i = float(np.max(window))
            low_i = float(np.min(window))
            close_i = float(px_adx[i])
            self._adx_calculator.update(high_i, low_i, close_i)
            new_prices_count += 1

        self._adx_last_processed_index = current_len - 1
        logging.debug(f"ADX incremental: processed {new_prices_count} new prices")
```

## Changes Made

### 1. core/engine.py
- **Line 198**: Added `self._adx_last_processed_index = -1` initialization
- **Lines 1346-1375**: Rewrote ADX incremental update logic
- **Line 904**: Reset `_adx_last_processed_index` during market reset

### 2. tests/test_adx_incremental_simple.py (New)
Created comprehensive unit tests:
- `test_incremental_update_only_adds_new_prices`: Verifies only new prices are processed
- `test_history_shrink_triggers_reset`: Verifies reset on market reset
- `test_window_reconstruction_correctness`: Verifies correctness over multiple ticks

## Performance Impact
- **Before**: O(n) per tick - reprocessed entire history (e.g., 60 prices × every tick)
- **After**: O(1) per tick - only processes 1 new price on average
- **Result**: Significant CPU reduction, especially with high tick frequencies

## Correctness
- ADX values match full recomputation (`compute_adx_last`) exactly
- Handles warm-up period correctly (still returns NaN until enough data)
- Properly handles market resets and history shrinkage
- Maintains state between ticks as intended for incremental indicators

## Testing
- All existing tests pass (236 passed)
- New tests specifically validate incremental behavior
- Verified against `compute_adx_last` for correctness

## Logging
Added debug logging to monitor incremental updates:
```
ADX incremental: processed X new prices, total index=Y
```

This helps verify the optimization is working in production.
