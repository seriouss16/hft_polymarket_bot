# Median Average Calculation Implementation

## Summary
Added "median average" (медианная средняя) calculation to the HFT bot reports as requested.

## What Was Implemented

### 1. Core Calculation Function
- Added `_median_avg(values: List[float]) -> float` in `hft_bot/utils/stats.py`
- For **odd** count: takes 3 central values and averages them
- For **even** count: takes 4 central values and averages them
- For small datasets (n<3 odd or n<4 even): returns arithmetic mean
- Example: `[1,2,3,4,5,6,7]` → `(3+4+5)/3 = 4.0`

### 2. Journal Stats Properties
Added to `_JournalStats` class:
- `median_avg_pnl`: median average of all trade PnL values
- `median_avg_win`: median average of winning trades
- `median_avg_loss`: median average of losing trades

### 3. Report Integration
**Compact Report** (`show_report`):
- Reads journal path from `TRADE_JOURNAL_PATH` env var (default: `reports/trade_journal.csv`)
- Displays median metrics section if journal exists and has data:
  ```
  📊 Медианные показатели (журнал):
     Медианная средняя (все):      X.XXXX USD
     Медианная средняя (профит):   X.XXXX USD
     Медианная средняя (убыток):   X.XXXX USD
  ```

**Final Report** (`show_final_report`):
- Uses existing journal aggregates
- Adds three new rows in the journal stats table:
  - "Медианная средняя (все)"
  - "Медианная средняя (профит)"
  - "Медианная средняя (убыток)"

## Testing
Created `hft_bot/test_median_calc.py` with comprehensive test cases:
- Odd counts: n=7, n=3
- Even counts: n=4, n=6, n=8
- Edge cases: empty list, single element, small datasets
- Symmetric distributions
- Duplicate values

All tests pass ✓

## Files Modified
1. `hft_bot/utils/stats.py` - Added median calculation and integration
2. `hft_bot/test_median_calc.py` - Test script (new file)

## Usage
The median metrics automatically appear in reports when:
1. Trade journal exists and contains data
2. Journal has `pnl` column with numeric values

No configuration changes needed - works out of the box.
