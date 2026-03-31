# Phase 2 WebSocket Migration - Implementation Plan

## Overview

This document provides a detailed implementation plan for completing Phase 2 of the WebSocket migration: **Fully Event-Driven Order Tracking**.

## Current State Analysis

### What's Already Implemented (Good Foundation)

1. **ClobUserOrderCache** (`hft_bot/data/clob_user_ws.py`):
   - ✅ Complete order state machine with `OrderState` enum (PENDING, PARTIAL, FILLED, CANCELLED, FAILED, STALE)
   - ✅ Event types and order event dataclasses
   - ✅ Callback mechanism (`set_order_callback`)
   - ✅ Event waiting (`wait_for_order_update` with asyncio.Event)
   - ✅ Comprehensive metrics (latency, event counts, state transitions)
   - ✅ Detailed logging for all events

2. **LiveExecutionEngine** (`hft_bot/core/live_engine.py`):
   - ✅ `_on_user_order_event()` callback handler
   - ✅ `_wait_for_order_fill()` with WS event wait + HTTP fallback
   - ✅ `_track_ws_latency()`, `_get_ws_metrics()`, `_log_ws_metrics()`
   - ✅ Metrics dicts: `_ws_metrics`, `_http_metrics`
   - ⚠️ `_poll_order()` still exists and is called from multiple places
   - ⚠️ `_wait_for_order_fill()` currently polls cache every 100ms instead of truly event-driven wait

3. **StatsCollector** (`hft_bot/utils/stats.py`):
   - ✅ WS metrics placeholders in `__init__`
   - ✅ `_ws_metrics_line()` method for display
   - ✅ `set_ws_metrics()` and `set_http_metrics()` methods
   - ⚠️ Not integrated into `show_report()` - needs to pull from live_engine

### What Needs Completion

1. **Make `_wait_for_order_fill()` truly event-driven** - use `wait_for_order_update()` instead of polling
2. **Remove `_poll_order()` completely** - replace all calls with event-driven approach
3. **Integrate metrics into stats** - pull from live_engine in show_report
4. **Add config var** `LIVE_ORDER_WS_TIMEOUT_SEC` to runtime.env
5. **Update bot_main_loop** to periodically pull and log WS metrics
6. **Test and validate** WS-only operation

---

## Implementation Steps

### Step 1: Add Configuration Variable

**File**: `hft_bot/config/runtime.env`

Add the following line after `LIVE_ORDER_MAX_REPRICE=1`:

```
# Phase 2 WebSocket Migration: Timeout for order event wait before HTTP fallback
LIVE_ORDER_WS_TIMEOUT_SEC=30
```

**Rationale**: This controls how long to wait for WS events before falling back to HTTP polling. 30 seconds is a reasonable balance between waiting for events and not blocking too long.

---

### Step 2: Modify `_wait_for_order_fill()` to be Truly Event-Driven

**File**: `hft_bot/core/live_engine.py`

**Current Implementation** (lines 633-696):
```python
async def _wait_for_order_fill(
    self,
    order_id: str,
    timeout: float | None = None,
) -> tuple[str, float]:
    """Wait for order event via WS with HTTP fallback."""
    if timeout is None:
        timeout = float(os.getenv("LIVE_ORDER_WS_TIMEOUT_SEC", "30"))
    
    start_time = time.time()
    ws_wait_start = start_time
    
    # Check cache first (may have recent data from WS)
    if self._user_order_cache is not None:
        cached = self._user_order_cache.get_order_fill(order_id)
        if cached is not None:
            return cached
    
    # Wait for WS event with timeout
    # Since we don't have asyncio.Event per order, we poll the cache
    # with a reasonable interval until timeout
    poll_interval = 0.1  # 100ms polling for event-driven fallback
    ws_timeout = float(os.getenv("LIVE_ORDER_WS_TIMEOUT_SEC", "30"))
    
    while time.time() - ws_wait_start < ws_timeout:
        cached = self._user_order_cache.get_order_fill(order_id) if self._user_order_cache else None
        if cached is not None:
            ws_latency = (time.time() - ws_wait_start) * 1000
            self._track_ws_latency(ws_latency)
            return cached
        await asyncio.sleep(poll_interval)
    
    # Timeout — fall back to HTTP polling
    http_wait_time = time.time() - ws_wait_start
    self._http_metrics["http_fallbacks_total"] += 1
    logging.warning(
        "[WS] Order event timeout for %s after %.1fs, falling back to HTTP "
        "(WS latency=%.2fms, HTTP fallback count=%d)",
        order_id[:20], http_wait_time,
        (time.time() - ws_wait_start) * 1000,
        self._http_metrics["http_fallbacks_total"],
    )
    return await asyncio.to_thread(self._get_order_fill, order_id)
```

**New Implementation** (truly event-driven):
```python
async def _wait_for_order_fill(
    self,
    order_id: str,
    timeout: float | None = None,
) -> tuple[str, float]:
    """Wait for order event via WS with HTTP fallback.
    
    Phase 2 WebSocket Migration: Truly event-driven using asyncio.Event.
    Uses ClobUserOrderCache.wait_for_order_update() for event-driven waiting.
    Falls back to HTTP polling only if WS events timeout.
    """
    if timeout is None:
        timeout = float(os.getenv("LIVE_ORDER_WS_TIMEOUT_SEC", "30"))
    
    start_time = time.time()
    
    # Check cache first (may have recent data from WS)
    if self._user_order_cache is not None:
        cached = self._user_order_cache.get_order_fill(order_id)
        if cached is not None:
            ws_latency = (time.time() - start_time) * 1000
            self._track_ws_latency(ws_latency)
            return cached
    
    # Use event-driven wait via ClobUserOrderCache
    if self._user_order_cache is not None:
        event_received = self._user_order_cache.wait_for_order_update(
            order_id, timeout=timeout
        )
        if event_received:
            # Event received - check cache for updated data
            cached = self._user_order_cache.get_order_fill(order_id)
            if cached is not None:
                ws_latency = (time.time() - start_time) * 1000
                self._track_ws_latency(ws_latency)
                return cached
    
    # Timeout — fall back to HTTP polling
    http_wait_time = time.time() - start_time
    self._http_metrics["http_fallbacks_total"] += 1
    logging.warning(
        "[WS] Order event timeout for %s after %.1fs, falling back to HTTP "
        "(WS latency=%.2fms, HTTP fallback count=%d)",
        order_id[:20], http_wait_time,
        (time.time() - start_time) * 1000,
        self._http_metrics["http_fallbacks_total"],
    )
    return await asyncio.to_thread(self._get_order_fill, order_id)
```

**Key Changes**:
- Removed polling loop (`while time.time() - ws_wait_start < ws_timeout`)
- Added `await self._user_order_cache.wait_for_order_update(order_id, timeout=timeout)`
- This is truly event-driven - no polling, just waiting for the event to be set

---

### Step 3: Remove `_poll_order()` Method

**File**: `hft_bot/core/live_engine.py`

**Current State**: `_poll_order()` exists at lines 1174-1448 and is called from:
- `execute()` (line ~1100)
- `close_position()` (line ~1200)
- `_emergency_exit_order()` (line ~1545)

**Action**: Replace all calls to `_poll_order()` with direct event-driven approach.

**Option A: Keep `_poll_order()` but make it event-driven**
- Modify `_poll_order()` to use `_wait_for_order_fill()` internally
- This is less invasive and maintains backward compatibility

**Option B: Remove `_poll_order()` completely**
- Inline the logic from `_poll_order()` into callers
- Use `_wait_for_order_fill()` directly
- More invasive but cleaner architecture

**Recommended: Option A** (less risk, easier to test)

**Modified `_poll_order()`**:
```python
async def _poll_order(self, tracked: TrackedOrder) -> None:
    """Monitor fill status; reprice stale orders; handle partial fills correctly.
    
    Phase 2 WebSocket Migration: Fully event-driven order tracking.
    Uses WebSocket events for immediate fill detection with HTTP fallback.
    """
    poly_min = req_float("POLY_CLOB_MIN_SHARES")
    ws_enabled = os.getenv("CLOB_USER_WS_ENABLED", "1").strip().lower() in ("1", "true", "yes")
    
    logging.debug(
        "[WS] Starting event-driven order tracking: id=%s %s %.2f @ %.4f",
        tracked.order_id[:20], tracked.side, tracked.size, tracked.price,
    )
    
    # Use event-driven model with WS timeout fallback
    while tracked.status in (OrderStatus.PENDING, OrderStatus.PARTIAL):
        # Wait for WS event with timeout (default 30s)
        status_str, clob_filled = await self._wait_for_order_fill(
            tracked.order_id,
            timeout=float(os.getenv("LIVE_ORDER_WS_TIMEOUT_SEC", "30")),
        )
        
        # Process status (same logic as before)
        if status_str in ("matched", "filled"):
            tracked.status = OrderStatus.FILLED
            if clob_filled > 0:
                tracked.filled_size = min(tracked.size, clob_filled)
            else:
                tracked.filled_size = tracked.size
            logging.info(
                "✅ Order filled: id=%s %s %.2f @ %.4f "
                "(WS events=%d HTTP fallbacks=%d)",
                tracked.order_id[:20], tracked.side, tracked.filled_size, tracked.price,
                self._ws_metrics["ws_events_received"],
                self._http_metrics["http_fallbacks_total"],
            )
            break
        
        # ... rest of status processing logic remains the same ...
```

---

### Step 4: Integrate WS Metrics into Stats

**File**: `hft_bot/utils/stats.py`

**Current State**: `show_report()` already has `_ws_metrics_line()` but metrics are not populated.

**Action**: Add method to pull metrics from live_engine and update in `show_report()`.

**Add to StatsCollector class**:
```python
def update_ws_metrics_from_engine(self, live_engine) -> None:
    """Update WS metrics from LiveExecutionEngine."""
    if hasattr(live_engine, '_get_ws_metrics'):
        ws_metrics = live_engine._get_ws_metrics()
        self.set_ws_metrics(ws_metrics)
    if hasattr(live_engine, '_http_metrics'):
        self.set_http_metrics(live_engine._http_metrics)
```

**Update `show_report()`**:
```python
def show_report(self, live_engine=None):
    """Print compact PnL summary to stdout (legacy block format)."""
    # ... existing code ...
    
    # Update WS metrics from engine if provided
    if live_engine is not None:
        self.update_ws_metrics_from_engine(live_engine)
    
    # ... rest of report generation ...
    # Line 264 already includes WS metrics:
    report.append(f"📡 {self._ws_metrics_line()}")
```

---

### Step 5: Update bot_main_loop

**File**: `hft_bot/bot_main_loop.py`

**Action**: Add periodic WS metrics logging.

**Find the stats reporting section** (around line 366-382):
```python
# Periodic stats before any await: slot/orderbook/strategy work must not delay the report.
if STATS_INTERVAL > 0.0 and (now - last_stats_time >= STATS_INTERVAL):
    if LIVE_MODE:
        try:
            _st_usdc = await asyncio.to_thread(live_exec.fetch_usdc_balance)
            stats.set_live_wallet_usdc(_st_usdc)
        except Exception as _st_exc:
            logging.debug("fetch_usdc_balance for stats: %s", _st_exc)
            stats.set_live_wallet_usdc(None)
    else:
        stats.set_live_wallet_usdc(None)
    stats.show_report()
    logging.info(
        "Intermediate stats (STATS_INTERVAL_SEC=%s, loop.now=%.3f).",
        STATS_INTERVAL,
        now,
    )
    last_stats_time = now
```

**Add WS metrics logging after stats.show_report()**:
```python
if STATS_INTERVAL > 0.0 and (now - last_stats_time >= STATS_INTERVAL):
    if LIVE_MODE:
        try:
            _st_usdc = await asyncio.to_thread(live_exec.fetch_usdc_balance)
            stats.set_live_wallet_usdc(_st_usdc)
        except Exception as _st_exc:
            logging.debug("fetch_usdc_balance for stats: %s", _st_exc)
            stats.set_live_wallet_usdc(None)
    else:
        stats.set_live_wallet_usdc(None)
    
    # Update and show WS metrics
    if LIVE_MODE and live_exec is not None:
        stats.update_ws_metrics_from_engine(live_exec)
        live_exec._log_ws_metrics("stats_interval")
    
    stats.show_report()
    logging.info(
        "Intermediate stats (STATS_INTERVAL_SEC=%s, loop.now=%.3f).",
        STATS_INTERVAL,
        now,
    )
    last_stats_time = now
```

---

### Step 6: Testing and Validation

**Test Plan**:

1. **Paper Mode Test** (`LIVE_MODE=0`):
   - Run bot in paper mode
   - Verify no HTTP GET /orders calls in logs during normal operation
   - Verify WS events appear in logs with "[WS] Event received" prefix
   - Check stats report shows WS metrics line

2. **Live Mode Test** (`LIVE_MODE=1`):
   - Run bot with real account (small position)
   - Place test order
   - Verify order fill detected via WS event (<200ms)
   - Check logs: should see "✅ [WS] Order filled via event"
   - Verify HTTP fallback only triggers on timeout

3. **WS Failure Test**:
   - Simulate WS disconnect (stop clob_user_ws task)
   - Verify HTTP fallback triggers after 30s timeout
   - Check logs: should see "[WS] Order event timeout... falling back to HTTP"

4. **Metrics Validation**:
   - Check stats report shows: "WS: events=X fallbacks=Y latency_avg=Zms"
   - Verify ws_events_total increases with each order event
   - Verify http_fallbacks_total only increases on timeout

---

## Expected Results

### Before (Polling-Based):
- HTTP GET /orders called every 0.2s (5 times/sec)
- Order fill detection latency: 1-2s (polling interval)
- API load: ~300 calls/min for order status
- No WS metrics in stats

### After (Event-Driven):
- HTTP GET /orders called only on timeout (every 30s max)
- Order fill detection latency: <200ms (WS event)
- API load: ~2 calls/min for order status (fallback only)
- WS metrics displayed in stats: "WS: events=150 fallbacks=2 latency_avg=45ms"

### Latency Improvement:
- **Book update latency**: 50-80ms (from 500-1000ms polling)
- **Order fill detection**: <200ms (from 1-2s polling)
- **Overall system responsiveness**: 5-10x improvement

---

## Rollback Plan

If issues occur:

1. **Quick Rollback**:
   - Set `CLOB_USER_WS_ENABLED=0` in config
   - Bot will fall back to HTTP polling
   - No code changes needed

2. **Configuration Rollback**:
   - Set `LIVE_ORDER_WS_TIMEOUT_SEC=5` (shorter timeout)
   - Faster fallback to HTTP if WS issues

3. **Code Rollback**:
   - Revert `_wait_for_order_fill()` to polling version
   - Restore `_poll_order()` to original implementation

---

## Success Criteria

- [ ] No periodic HTTP GET /orders during normal WS operation
- [ ] Order fill detection latency <200ms via WS events
- [ ] HTTP fallback only when WS fails or times out (logged clearly)
- [ ] All existing tests pass
- [ ] Stats report clearly shows WS vs HTTP usage
- [ ] No regressions in paper mode trading
- [ ] WS metrics line appears in stats report

---

## Timeline

| Step | Task | Estimated Time |
|------|------|----------------|
| 1 | Add config variable | 5 min |
| 2 | Modify `_wait_for_order_fill()` | 30 min |
| 3 | Update `_poll_order()` | 30 min |
| 4 | Integrate WS metrics into stats | 20 min |
| 5 | Update bot_main_loop | 15 min |
| 6 | Testing and validation | 2-4 hours |
| **Total** | | **~5 hours** |

---

## References

- [Phase 1 Implementation](hft_bot/WEBSOCKET_MIGRATION_PHASE2.md)
- [WebSocket Migration Plan](docs/WEBSOCKET_MIGRATION_PLAN.md)
- [Polymarket CLOB WebSocket Docs](https://docs.polymarket.com/developers/CLOB/websocket/wss-auth)
- [Polymarket CLOB Market WebSocket Docs](https://docs.polymarket.com/developers/CLOB/websocket/wss-market)
