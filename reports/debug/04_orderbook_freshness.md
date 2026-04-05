# 📊 HFT Performance Review: Section 4 - Orderbook & Data Freshness

## Executive Summary

Completed deep audit of orderbook processing, data freshness controls, timestamp handling, and out-of-order event protection. The system has **good foundational architecture** but **critical latency risks** and **inconsistencies** that could cause stale data usage or unnecessary action blocking.

---

## 🔍 Critical Findings

### 1. **Out-of-Order Event Protection Missing for Market Data**

**Location:** [`ClobMarketBookCache`](data/clob_market_ws.py:74) vs [`ClobUserOrderCache`](data/clob_user_ws.py:850)

**Issue:** Only user-channel WebSocket has sequence gap detection. Market-channel orderbook updates have **NO out-of-order protection**. A delayed book event could overwrite fresh data with stale prices.

**Impact:** 100-5000ms of trading on stale data

**Fix:** Add sequence tracking to `ClobMarketBookCache` mirroring `ClobUserOrderCache._check_sequence_gap()`

---

### 2. **Timestamp Chaos - Multiple Time Sources**

**Locations:** 
- [`data/aggregator.py:133`](data/aggregator.py:133) - `asyncio.get_running_loop().time()`
- [`data/poly_clob.py:143`](data/poly_clob.py:143) - `asyncio.get_running_loop().time()`
- [`data/clob_market_ws.py:375`](data/clob_market_ws.py:375) - `time.time()`
- [`bot_main_loop.py:836`](bot_main_loop.py:836) - `asyncio.get_event_loop().time()`

**Issue:** Mixing wall-clock (`time.time()`) and monotonic clocks creates inconsistent staleness measurements. NTP jumps can cause false stale/fresh states.

**Impact:** 0-1000ms measurement errors; latency gates become unreliable

**Fix:** Standardize on `asyncio.get_running_loop().time()` for all internal timestamps

---

### 3. **Blocking Lock in Async Critical Path**

**Location:** [`ClobMarketBookCache.is_fresh()`](data/clob_market_ws.py:303) uses `threading.RLock()`

**Issue:** Freshness checks in `LiveExecutionEngine.execute()` acquire blocking lock. WS handler holds lock during snapshot generation (0.1-0.5ms). Causes main loop jitter.

**Impact:** 0.5-2ms per tick under contention

**Fix:** Use `asyncio.Lock()` or lock-free snapshot with atomic copy

---

### 4. **TOCTOU Race in `sync_poly_book_from_cache()`**

**Location:** [`data/clob_market_ws.py:682-683`](data/clob_market_ws.py:682)

```python
ob_up = cache.snapshot(token_up_id, depth)  # Acquires lock
if ob_up is None or not cache.is_fresh(token_up_id):  # Acquires lock AGAIN
```

**Issue:** Two separate lock acquisitions; snapshot could become stale between check and use.

**Impact:** 0-25ms race window; could apply stale snapshot

**Fix:** Combine into atomic `get_fresh_snapshot()` method

---

### 5. **No Timestamp Validation on Incoming Events**

**Location:** [`data/clob_market_ws.py:494-505`](data/clob_market_ws.py:494)

**Issue:** Events accepted without validating timestamp bounds. Malformed packets could corrupt freshness state.

**Impact:** Variable; could cause indefinite stale or false fresh state

**Fix:** Reject events with timestamps >±5s from current time

---

### 6. **Inconsistent Staleness Thresholds**

**Location:** [`ClobMarketBookCache`](data/clob_market_ws.py:84-86)

**Issue:** Three thresholds (`_max_stale_sec`, `_stale_warn_sec`, `_stale_skip_sec`) but only first is used. Main loop has separate `_live_max_book_age_sec` check not aligned with reconnection grace period.

**Impact:** 0-50ms missed opportunities during reconnection

**Fix:** Consolidate thresholds; use `cache.is_fresh()` consistently

---

### 7. **Missing Monotonic Ordering for `poly_book.book["ts"]`**

**Location:** [`data/poly_clob.py:143`](data/poly_clob.py:143)

**Issue:** Poly RTDS timestamp not validated for monotonicity. Out-of-order message could make feed appear fresh (negative age clamped to 0).

**Impact:** 1000-5000ms of false freshness

**Fix:** Track last timestamp per symbol; reject older updates

---

### 8. **Latency Measurement Gaps**

**Location:** [`bot_main_loop.py:995-1044`](bot_main_loop.py:995)

**Issue:** Latency measured only when multiple conditions hold; no rolling buffer for P99; gaps in data.

**Impact:** Poor diagnostics; cannot detect gradual degradation

**Fix:** Measure every tick; maintain rolling statistics

---

## 📋 Step-by-Step Improvement Plan

### **Priority 1 (Critical): Out-of-Order Protection for Market Data**

**Target:** [`data/clob_market_ws.py`](data/clob_market_ws.py)

**Changes:**
- Add `_last_sequence: dict[str, int]` and `_sequence_gaps: int`
- Implement `_check_sequence(asset_id, seq)` mirroring `clob_user_ws.py:850`
- Call in `_apply_book()`, `_apply_price_change()`, `_apply_best_bid_ask()`
- Drop events with `seq <= last` and log warning

**Estimated Gain:** Prevents 100-5000ms stale trading

---

### **Priority 2 (High): Unify Timestamps to Monotonic Clock**

**Targets:** 
- [`data/clob_market_ws.py:375`](data/clob_market_ws.py:375)
- [`data/clob_user_ws.py:434,470`](data/clob_user_ws.py:434) (latency calc only)
- [`core/engine.py:666`](core/engine.py:666) and all `time.time()` calls in async paths

**Changes:**
- Replace `time.time()` with `asyncio.get_running_loop().time()` for all age/freshness calculations
- Update `TrackedOrder.placed_at` to use monotonic (already set via `time.time()` in many places)
- Add utility: `def now_mono() -> float: return asyncio.get_running_loop().time()`
- Update `TrackedOrder.age_sec` to use monotonic

**Estimated Gain:** Eliminates 0-1000ms measurement errors

---

### **Priority 3 (High): Fix TOCTOU in `sync_poly_book_from_cache()`**

**Target:** [`data/clob_market_ws.py:667-699`](data/clob_market_ws.py:667)

**Changes:**
- Add `ClobMarketBookCache.get_fresh_snapshot_atomic(token_id, depth) -> tuple[dict|None, bool]`
- Acquire lock once, check freshness, build snapshot while holding lock, return both
- Refactor `sync_poly_book_from_cache()` to use atomic method

**Estimated Gain:** Eliminates race condition (0-25ms error window)

---

### **Priority 4 (Medium): Consistent Staleness Thresholds**

**Target:** [`data/clob_market_ws.py`](data/clob_market_ws.py) + [`bot_main_loop.py:1019`](bot_main_loop.py:1019)

**Changes:**
- Remove unused `_stale_warn_sec`/`_stale_skip_sec` or add warning logs
- Replace `_live_max_book_age_sec` check with `cache.is_fresh()` directly
- Ensure reconnection 2x multiplier applies consistently

**Estimated Gain:** 0-50ms fewer skips during reconnection

---

### **Priority 5 (Medium): Timestamp Sanity Validation**

**Target:** [`data/clob_market_ws.py:494-505`](data/clob_market_ws.py:494)

**Changes:**
- In `_handle_message_dict()`, extract `msg_ts = msg.get("timestamp")`
- If present and numeric, validate: `abs(msg_ts - now_mono()) < 5.0` (seconds)
- Reject with warning if outside bounds
- For `book` events, also check `msg_ts >= self._last_book_ts.get(asset_id, 0)`

**Estimated Gain:** Prevents corruption from bad packets

---

### **Priority 6 (Low): Continuous Latency Metrics**

**Target:** [`bot_main_loop.py`](bot_main_loop.py)

**Changes:**
- Add `latency_buffer: deque[float] = deque(maxlen=1000)`
- Append `latency_ms` every tick (already computed at line 1043)
- Compute P50/P95/P99 every stats interval; log/export
- Alert on sustained high latency

**Estimated Gain:** Better observability
