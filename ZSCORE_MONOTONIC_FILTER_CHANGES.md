# Z-Score Monotonic Filter: Configurable Strictness

## Summary
Implemented configurable strictness levels for the z-score monotonic filter to handle NIGHT session (low liquidity, noisy market) differently from DAY session.

## Changes

### 1. Core Logic (`core/engine_entry_gates.py`)
- Modified `zscore_monotonic_for_direction()` to accept `monotonic_strictness` parameter
- Three modes:
  - `"strict"`: All ticks must be monotonic (original behavior)
  - `"relaxed"`: Allows at most 1 violation in the sequence
  - `"off"`: Skips check entirely (always returns True)
- Updated `entry_zscore_trend_ok()` to pass through the strictness parameter

### 2. Engine Integration (`core/engine.py`)
- Added `self.zscore_monotonic_strictness` attribute (default: "strict")
- Read from env var `HFT_ZSCORE_MONOTONIC_STRICTNESS`
- Updated in both `__init__()` and `reload_profile_params()`
- Passed to `entry_zscore_trend_ok()` and `_zscore_monotonic_for_direction()`

### 3. Configuration Files
- `config/runtime.env`: Added default `HFT_ZSCORE_MONOTONIC_STRICTNESS=strict`
- `config/runtime_day.env`: Set to `HFT_ZSCORE_MONOTONIC_STRICTNESS=strict`
- `config/runtime_night.env`: Set to `HFT_ZSCORE_MONOTONIC_STRICTNESS=relaxed`

### 4. Debug Logging (`core/engine_entry_candidates.py`)
- Added debug logging in `entry_momentum_alt_signal()` when signal is blocked in relaxed mode
- Logs number of violations, k value, and direction

### 5. Tests (`tests/test_engine_entry_gates.py`)
Added comprehensive tests:
- `test_zscore_monotonic_strict_allows_perfect_monotonic`
- `test_zscore_monotonic_strict_rejects_non_monotonic`
- `test_zscore_monotonic_relaxed_allows_one_violation`
- `test_zscore_monotonic_off_always_allows`
- `test_zscore_monotonic_k_equals_1`
- `test_zscore_monotonic_insufficient_samples`
- `test_zscore_monotonic_unknown_strictness_defaults_to_strict`

## Backward Compatibility
- Default value is "strict" (preserves existing behavior)
- DAY profile explicitly set to "strict"
- Existing tests pass (except 3 pre-existing failures unrelated to this change)

## Expected Behavior
- **DAY session**: Strict monotonicity (as before)
- **NIGHT session**: Relaxed - allows 1 violation in the k-tick window, enabling more entries during noisy low-liquidity periods

## Files Modified
1. `core/engine_entry_gates.py`
2. `core/engine.py`
3. `core/engine_entry_candidates.py`
4. `config/runtime.env`
5. `config/runtime_day.env`
6. `config/runtime_night.env`
7. `tests/test_engine_entry_gates.py`