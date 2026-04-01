# Blocking Call Elimination Plan

**Goal:** Eliminate all blocking calls that hurt latency or create risk in the HFT bot.
**Current State:** 38 `asyncio.to_thread()` calls, 2 `requests` blocking calls.
**Target:** Zero blocking calls in the async event loop.

---

## Phase 1: Critical Blocking Calls (Do Now)

### 1.1 Order Placement — ✅ COMPLETED

**File:** [`hft_bot/core/live_engine.py:_place_limit_raw()`](hft_bot/core/live_engine.py:1110)

**Before:**
```python
signed = self.client.create_order(order)  # BLOCKING: EIP-712 signing
resp = self.client.post_order(signed, OrderType.GTC)  # BLOCKING: HTTP POST
```

**After:**
```python
loop = asyncio.get_running_loop()
signed = await loop.run_in_executor(None, self.client.create_order, order)
resp = await loop.run_in_executor(None, self.client.post_order, signed, OrderType.GTC)
```

**Impact:** 30-50ms event loop unblocking per order
**Status:** ✅ Done, all 53 tests pass

---

### 1.2 Heartbeat — HIGH PRIORITY

**File:** [`hft_bot/bot_main_loop.py:199`](hft_bot/bot_main_loop.py:199)

**Current:**
```python
resp = await asyncio.to_thread(live_exec.client.post_heartbeat, _hb_id)
```

**Problem:**
- Heartbeat runs every 5 seconds in a dedicated task
- `asyncio.to_thread()` is correct here — it doesn't block the event loop
- **No change needed** — this is already non-blocking

**Status:** ✅ Already correct

---

### 1.3 Allowance Refresh — HIGH PRIORITY

**File:** [`hft_bot/bot_main_loop.py:145`](hft_bot/bot_main_loop.py:145)

**Current:**
```python
await asyncio.to_thread(live_exec.ensure_allowances)
```

**Problem:**
- Runs once at startup — doesn't affect trading latency
- `asyncio.to_thread()` is correct here
- **No change needed** — this is already non-blocking

**Status:** ✅ Already correct

---

## Phase 2: High-Impact Blocking Calls (Next Sprint)

### 2.1 Order Book Snapshot — MEDIUM PRIORITY

**Files:** Multiple locations in [`live_engine.py`](hft_bot/core/live_engine.py)

**Locations:**
- Line 587-591: `get_orderbook_snapshot` (parallel gather)
- Line 595-596: `get_orderbook_snapshot` (single)
- Line 2241: `get_orderbook_snapshot` (depth=1)

**Current:**
```python
ob_up, ob_down = await asyncio.gather(
    asyncio.to_thread(live_exec.get_orderbook_snapshot, token_up_id, 5),
    asyncio.to_thread(live_exec.get_orderbook_snapshot, token_down_id, 5),
)
```

**Analysis:**
- These are already wrapped in `asyncio.to_thread()` — **non-blocking**
- The underlying HTTP call takes ~620ms from Portugal, ~20ms from Ireland
- **No code change needed** — migration to Ireland will reduce latency by 600ms

**Status:** ✅ Already non-blocking, latency will improve with migration

---

### 2.2 Balance Fetch — MEDIUM PRIORITY

**Files:** Multiple locations in [`live_engine.py`](hft_bot/core/live_engine.py)

**Locations:**
- Line 322: `fetch_usdc_balance` (USDC debit verify)
- Line 1040: `fetch_usdc_balance` (budget check)
- Line 1341: `fetch_usdc_balance` (final stats)
- Line 2255: `fetch_usdc_balance` (pre-order snapshot)

**Current:**
```python
after = await asyncio.to_thread(self.fetch_usdc_balance)
```

**Analysis:**
- All wrapped in `asyncio.to_thread()` — **non-blocking**
- Each call takes ~620ms from Portugal
- Balance cache (`BalanceCache`) already implements caching with configurable staleness
- **No code change needed** — migration to Ireland will reduce latency by 600ms

**Status:** ✅ Already non-blocking, latency will improve with migration

---

### 2.3 Conditional Balance Fetch — MEDIUM PRIORITY

**Files:** Multiple locations in [`live_engine.py`](hft_bot/core/live_engine.py)

**Locations:**
- Line 269: `balance_cache.get_conditional_balance`
- Line 1799: `fetch_conditional_balance` (exit readiness)
- Line 1833: `fetch_conditional_balance` (allowance + balance)
- Line 1890: `fetch_conditional_balance` (sell chain sync)
- Line 1975: `fetch_conditional_balance` (parallel with get_best_prices)
- Line 2298: `fetch_conditional_balance` (rescue balance)
- Line 2344: `fetch_conditional_balance` (post-buy balance confirm)
- Line 2379: `fetch_conditional_balance` (strict mode extra polls)
- Line 2449: `fetch_conditional_balance` (post-FAK balance)

**Current:**
```python
_b = await asyncio.to_thread(self.fetch_conditional_balance, token_id)
```

**Analysis:**
- All wrapped in `asyncio.to_thread()` — **non-blocking**
- Each call takes ~620ms from Portugal
- **No code change needed** — migration to Ireland will reduce latency by 600ms

**Status:** ✅ Already non-blocking, latency will improve with migration

---

### 2.4 Get Best Prices — MEDIUM PRIORITY

**Files:** Multiple locations in [`live_engine.py`](hft_bot/core/live_engine.py)

**Locations:**
- Line 1335: `get_best_prices` (reprice)
- Line 1498: `get_best_prices` (emergency exit)
- Line 1590: `get_best_prices` (emergency cross)
- Line 1976: `get_best_prices` (parallel with balance fetch)
- Line 2036: `get_best_prices` (SELL placement)
- Line 2169: `get_best_prices` (BUY placement)

**Current:**
```python
best_bid, best_ask = await asyncio.to_thread(self.get_best_prices, tracked.token_id)
```

**Analysis:**
- All wrapped in `asyncio.to_thread()` — **non-blocking**
- Each call takes ~620ms from Portugal
- **No code change needed** — migration to Ireland will reduce latency by 600ms

**Status:** ✅ Already non-blocking, latency will improve with migration

---

### 2.5 Get Open Orders — LOW PRIORITY

**File:** [`hft_bot/core/live_engine.py:1768`](hft_bot/core/live_engine.py:1768)

**Current:**
```python
open_list = await asyncio.to_thread(self.get_open_orders, token_id)
```

**Analysis:**
- Wrapped in `asyncio.to_thread()` — **non-blocking**
- Used for order reconciliation, not on critical path
- **No change needed**

**Status:** ✅ Already non-blocking

---

### 2.6 FAK Sell — LOW PRIORITY

**Files:** Multiple locations in [`live_engine.py`](hft_bot/core/live_engine.py)

**Locations:**
- Line 1170: `_place_fak_sell`
- Line 1899: `_place_fak_sell` (residual exit)
- Line 2023: `_place_fak_sell` (SELL fallback)
- Line 2071: `_place_fak_sell` (SELL retry)

**Current:**
```python
filled, price = await asyncio.to_thread(self._place_fak_sell, token_id, size)
```

**Analysis:**
- All wrapped in `asyncio.to_thread()` — **non-blocking**
- **No change needed**

**Status:** ✅ Already non-blocking

---

### 2.7 Ensure Conditional Allowance — LOW PRIORITY

**Files:** Multiple locations in [`live_engine.py`](hft_bot/core/live_engine.py)

**Locations:**
- Line 363: `ensure_conditional_allowance`
- Line 1204: `ensure_conditional_allowance` (post-buy)
- Line 1832: `ensure_conditional_allowance` (sell prep)
- Line 2003: `ensure_conditional_allowance` (sell prep)
- Line 2045: `ensure_conditional_allowance` (sell loop)
- Line 2065: `ensure_conditional_allowance` (FAK retry)

**Current:**
```python
await asyncio.to_thread(self.ensure_conditional_allowance, token_id)
```

**Analysis:**
- All wrapped in `asyncio.to_thread()` — **non-blocking**
- **No change needed**

**Status:** ✅ Already non-blocking

---

## Phase 3: External Blocking Calls

### 3.1 Market Selector HTTP Request

**File:** [`hft_bot/core/selector.py:159`](hft_bot/core/selector.py:159)

**Current:**
```python
def _do_request():
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()

return await asyncio.to_thread(_do_request)
```

**Analysis:**
- Wrapped in `asyncio.to_thread()` — **non-blocking**
- Used for market selection, not on critical trading path
- **No change needed**

**Status:** ✅ Already non-blocking

---

### 3.2 Unused HTTP Session

**File:** [`hft_bot/core/live_engine.py:127`](hft_bot/core/live_engine.py:127)

**Current:**
```python
self._http = requests.Session()
```

**Analysis:**
- This session is created but **never used** for any HTTP calls
- All HTTP calls go through `py_clob_client`
- **Recommendation:** Remove this unused session to reduce memory footprint

**Status:** ⚠️ Unused code — safe to remove

---

## Phase 4: Latency Reduction via Migration

### 4.1 Current Latency Breakdown

| Operation | Current (Portugal) | Target (Ireland) | Improvement |
|-----------|-------------------|------------------|-------------|
| HTTPS request to CLOB | ~620ms | ~20ms | **600ms** |
| Order placement (sign + HTTP) | ~670ms | ~70ms | **600ms** |
| Balance fetch | ~620ms | ~20ms | **600ms** |
| Get best prices | ~620ms | ~20ms | **600ms** |
| Conditional balance fetch | ~620ms | ~20ms | **600ms** |

### 4.2 Migration Impact on Trading Loop

**Current Trading Loop Latency (Portugal):**
```
1. Get best prices:        620ms
2. Place order (sign+HTTP): 670ms
3. Wait for fill (WS):     0-30000ms (event-driven)
4. Balance confirm:        620ms × 3 retries = 1860ms
───────────────────────────────────────────────────
Total (best case):         ~3150ms
Total (with retries):      ~5000ms+
```

**After Migration (Ireland):**
```
1. Get best prices:        20ms
2. Place order (sign+HTTP): 70ms
3. Wait for fill (WS):     0-30000ms (event-driven)
4. Balance confirm:        20ms × 3 retries = 60ms
───────────────────────────────────────────────────
Total (best case):         ~150ms
Total (with retries):      ~200ms
```

**Improvement: 20x faster** (from 3150ms to 150ms)

---

## Phase 5: Configuration Optimizations

### 5.1 Reduce WS Timeout

**File:** `hft_bot/config/runtime.env`

**Current:**
```env
LIVE_ORDER_WS_TIMEOUT_SEC=30
```

**Recommended:**
```env
LIVE_ORDER_WS_TIMEOUT_SEC=10
```

**Impact:** 20s faster failure detection when WS events don't arrive

---

### 5.2 Balance Cache Tuning

**File:** `hft_bot/config/runtime.env`

**Current:**
```env
BALANCE_CACHE_MAX_AGE_SEC=5.0
BALANCE_CONDITIONAL_MAX_AGE_SEC=10.0
```

**Recommended (after migration to Ireland):**
```env
BALANCE_CACHE_MAX_AGE_SEC=2.0
BALANCE_CONDITIONAL_MAX_AGE_SEC=5.0
```

**Impact:** Fresher balance data with lower latency from Ireland

---

## Summary: What Needs Code Changes

| Item | Status | Action Required |
|------|--------|-----------------|
| Order placement (`_place_limit_raw`) | ✅ **DONE** | Already async |
| Heartbeat | ✅ Already non-blocking | No change |
| Allowance refresh | ✅ Already non-blocking | No change |
| Order book snapshot | ✅ Already non-blocking | No change |
| Balance fetch | ✅ Already non-blocking | No change |
| Conditional balance fetch | ✅ Already non-blocking | No change |
| Get best prices | ✅ Already non-blocking | No change |
| Get open orders | ✅ Already non-blocking | No change |
| FAK sell | ✅ Already non-blocking | No change |
| Ensure conditional allowance | ✅ Already non-blocking | No change |
| Market selector HTTP | ✅ Already non-blocking | No change |
| Unused HTTP session | ⚠️ Unused | Remove `self._http = requests.Session()` |

## Summary: What Needs Migration

| Item | Current | Target | Improvement |
|------|---------|--------|-------------|
| HTTPS latency | 620ms | 20ms | **600ms** |
| Order cycle time | 3150ms | 150ms | **20x faster** |

## Action Items

1. **Deploy to Ireland (AWS eu-west-1)** — **HIGHEST PRIORITY**
   - Expected improvement: 600ms per HTTP call
   - Total trading loop improvement: 20x faster

2. **Remove unused HTTP session** — Low effort
   - Remove `self._http = requests.Session()` from `live_engine.py:127`

3. **Reduce `LIVE_ORDER_WS_TIMEOUT_SEC` to 10** — Config change only
   - Faster failure detection

4. **Tune balance cache after migration** — Config change only
   - Reduce `BALANCE_CACHE_MAX_AGE_SEC` to 2.0
   - Reduce `BALANCE_CONDITIONAL_MAX_AGE_SEC` to 5.0

---

*Document generated: 2026-04-01*
*Author: Architect Mode Analysis*
