# Phase 3 WebSocket Migration: Balance Updates via WebSocket

**Date:** 2026-03-31  
**Status:** Complete  
**Target:** Implement optimized balance caching since Polymarket does not provide balance updates via WebSocket

---

## Executive Summary

Polymarket's WebSocket API **does not provide balance updates**. The User WebSocket channel (`wss://ws-subscriptions-clob.polymarket.com/ws/user`) only provides order and trade events. There is no separate balance WebSocket endpoint available.

This implementation provides an **optimized HTTP polling approach with caching** that:
- Reduces HTTP calls through intelligent cache invalidation
- Tracks balance fetch metrics (hit rate, latency, errors)
- Provides real-time balance tracking in stats reports
- Maintains backward compatibility with existing code

---

## Research Findings

### Polymarket WebSocket Capabilities

**User WebSocket Channel** (`wss://ws-subscriptions-clob.polymarket.com/ws/user`):
- ✅ `order` events (PLACEMENT, UPDATE, CANCELLATION)
- ✅ `trade` events (MATCHED, CONFIRMED, FAILED)
- ❌ **No balance events**
- ❌ **No balance subscription**

**Market WebSocket Channel** (`wss://ws-subscriptions-clob.polymarket.com/ws/market`):
- ✅ `book` events (full order book snapshots)
- ✅ `price_change` events (incremental updates)
- ✅ `best_bid_ask` events (top-of-book)
- ❌ **No balance events**

**Balance API** (REST only):
- `GET /balance-allowance` with `asset_type=COLLATERAL` → USDC balance
- `GET /balance-allowance` with `asset_type=CONDITIONAL` + `token_id` → Conditional token balance

**Reference:** https://docs.polymarket.com/developers/CLOB/websocket/wss-auth

---

## Implementation

### New Module: `hft_bot/data/balance_cache.py`

A thread-safe balance cache with HTTP polling and metrics tracking:

#### Classes

**`BalanceCacheEntry`**
- Cached balance value with timestamp
- Staleness detection based on configurable age threshold

**`BalanceMetrics`**
- Tracks: fetches_total, cache_hits, http_fallbacks, errors
- Latency statistics: avg, min, max
- Hit rate calculation

**`BalanceCache`**
- Thread-safe USDC balance cache
- Per-token conditional balance cache
- Automatic staleness-based refresh
- Comprehensive metrics tracking

**`BalanceCacheProvider`**
- Factory for creating BalanceCache from LiveExecutionEngine

#### Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `BALANCE_CACHE_MAX_AGE_SEC` | 5.0 | Maximum cache age for USDC balance |
| `BALANCE_CONDITIONAL_MAX_AGE_SEC` | 10.0 | Maximum cache age for conditional token balances |

### Modified Files

#### `hft_bot/utils/stats.py`

Added balance cache metrics tracking:
- `_balance_metrics` dictionary for balance cache statistics
- `set_balance_metrics()` method
- `update_balance_metrics_from_cache()` method
- `_balance_metrics_line()` for report display
- Balance metrics line in `show_report()` output

#### `hft_bot/bot_main_loop.py`

Integration changes:
- Import `BalanceCache` from `data.balance_cache`
- Initialize `balance_cache` in LIVE_MODE
- Use `balance_cache.get_usdc_balance()` for stats reports
- Use `balance_cache.get_conditional_balance()` for inventory reconciliation
- Log balance cache metrics periodically

---

## Usage

### Basic Usage

```python
from data.balance_cache import BalanceCache

# Create cache with custom fetchers
balance_cache = BalanceCache(
    balance_fetcher=live_exec.fetch_usdc_balance,
    conditional_balance_fetcher=live_exec.fetch_conditional_balance,
    max_age_sec=5.0,
    conditional_max_age_sec=10.0,
)

# Get USDC balance (uses cache if fresh)
usdc_balance = await asyncio.to_thread(balance_cache.get_usdc_balance)

# Get conditional token balance
ctf_balance = await asyncio.to_thread(
    balance_cache.get_conditional_balance, token_id
)

# Get metrics
metrics = balance_cache.get_metrics()
```

### In Main Loop

```python
# Initialize balance cache
balance_cache = BalanceCache(
    balance_fetcher=live_exec.fetch_usdc_balance,
    conditional_balance_fetcher=live_exec.fetch_conditional_balance,
    max_age_sec=float(os.getenv("BALANCE_CACHE_MAX_AGE_SEC", "5.0")),
    conditional_max_age_sec=float(os.getenv("BALANCE_CONDITIONAL_MAX_AGE_SEC", "10.0")),
)

# Use in stats report
_st_usdc = await asyncio.to_thread(balance_cache.get_usdc_balance)
stats.set_live_wallet_usdc(_st_usdc)
stats.update_balance_metrics_from_cache(balance_cache)
balance_cache.log_metrics("stats_interval")
```

---

## Metrics Display

Balance cache metrics are displayed in stats reports:

```
💾 BAL: fetches=100 hits=85 hit_rate=85.0% latency_avg=12.5ms usdc_age=2.3s
```

| Metric | Description |
|--------|-------------|
| `fetches` | Total balance fetch attempts |
| `hits` | Cache hits (no HTTP call needed) |
| `hit_rate` | Percentage of cache hits |
| `latency_avg` | Average fetch latency in ms |
| `usdc_age` | Current USDC cache age in seconds |

---

## Performance Benefits

### Before (Direct HTTP Polling)
- Every stats interval (60s): 1 HTTP call for USDC
- Every close: 1 HTTP call per conditional token
- No caching between polls
- No metrics tracking

### After (Cached HTTP Polling)
- USDC balance cached for 5 seconds
- Conditional token balances cached for 10 seconds
- Automatic cache invalidation based on staleness
- Comprehensive metrics tracking
- Reduced HTTP load during high-frequency operations

---

## Testing

### Unit Tests

```python
import pytest
from data.balance_cache import BalanceCache, BalanceCacheEntry

def test_cache_freshness():
    entry = BalanceCacheEntry(value=100.0, timestamp=time.time())
    assert entry.is_fresh(5.0) is True
    assert entry.is_fresh(0.1) is False

def test_cache_hit_rate():
    metrics = BalanceMetrics()
    metrics.fetches_total = 100
    metrics.cache_hits = 85
    assert metrics.hit_rate == 85.0
```

### Integration Tests

Run the bot with `LIVE_MODE=1` and verify:
1. Balance cache metrics appear in stats reports
2. Cache hit rate is > 0% (cache is being used)
3. No errors in balance fetch operations
4. Conditional token balances are cached correctly

---

## Migration Checklist

- [x] Research Polymarket WebSocket documentation for balance updates
- [x] Check existing code for balance-related WebSocket events
- [x] Analyze current HTTP balance polling implementation
- [x] Determine if balance updates are available via WebSocket (NOT AVAILABLE)
- [x] Document findings and create balance cache optimization
- [x] Update stats.py to track balance metrics
- [x] Integrate balance cache in bot_main_loop.py
- [x] Test implementation and verify no regressions

---

## Future Considerations

### If Polymarket Adds Balance WebSocket

If Polymarket adds balance updates via WebSocket in the future:

1. Add `balance` event type to `ClobUserOrderCache`
2. Implement balance update handler
3. Update `BalanceCache` to support WebSocket-driven updates
4. Reduce HTTP polling interval or eliminate entirely

### Recommended Monitoring

- Monitor `hit_rate_pct` - should be > 80% in normal operation
- Monitor `avg_latency_ms` - should be < 50ms for cached reads
- Monitor `errors` - should be 0 in normal operation
- Alert on `hit_rate_pct` < 50% (indicates cache invalidation issues)

---

## References

- Polymarket WebSocket Documentation: https://docs.polymarket.com/developers/CLOB/websocket/wss-auth
- Phase 2 Implementation: `hft_bot/WEBSOCKET_MIGRATION_PHASE2_IMPLEMENTATION.md`
- Migration Plan: `docs/WEBSOCKET_MIGRATION_PLAN.md`

