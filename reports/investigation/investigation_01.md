# Investigation: HFT Bot Low Win Rate & Latency Issues

## Summary
The bot achieves 33.3% win rate with 51 SL exits vs 15 TP exits across 70 journal entries.
Net PnL of session is +$15.35 but journal cumulative PnL is -$46.34 (multi-session drift).
Key problems: stop-loss fires on spread noise, RSI filter kills entries, and blocking HTTP in async loop.

---

## Root Cause Analysis

### CRITICAL: 1. Blocking HTTP in async event loop (`selector.py`)

**File**: `data/selector.py`, `core/selector.py` lines 155–189

`fetch_up_down_token_ids()` and `fetch_up_down_quotes()` are declared `async def` but internally call `requests.get()` (synchronous, blocking). When `SLOT_POLL_SEC=0` (default), the slot check runs **every single tick**, blocking the event loop with an HTTP request (~50-500ms). This delays signal processing, RSI computation, and entry/exit decisions on every iteration while there is no position — effectively stalling the whole engine.

**Fix**: Replace `requests.get()` with `aiohttp` / `httpx` async calls, OR wrap in `asyncio.to_thread()`.

---

### CRITICAL: 2. Stop-loss too tight relative to spread cost

**File**: `core/engine.py` line 56

```python
self.pnl_sl_pct = float(os.getenv("HFT_PNL_SL_PERCENT", "0.020"))  # 2% SL
self.pnl_tp_pct = float(os.getenv("HFT_PNL_TP_PERCENT", "0.10"))   # 10% TP
```

- SL = 2% of position notional (e.g. $2 on $100 position)
- TP = 10% of position notional (e.g. $10 on $100 position)

The UP/DOWN token bid-ask spread is 1-5 cents (1–5% at prices near 0.3–0.8).
Round-trip spread cost ≈ `entry_ask - exit_bid ≈ 0.01–0.03` per share.

At $100 invested, buying at ask=0.39, 1% fee applied → exec at 0.3903.
Selling at bid=0.38 (1 tick down) → exec at 0.3796.
PnL = (0.3796 - 0.3903) * shares ≈ -$2.75 which immediately hits the 2% ($2) stop.

**The stop is inside the noise/spread zone.** Any 1–2 cent adverse tick triggers SL before the trade has a chance to work.

**Fix**:
- Raise `HFT_PNL_SL_PERCENT` to at least `0.04–0.06` (4–6%)
- Lower `HFT_PNL_TP_PERCENT` to `0.05–0.07` (5–7%) so TP/SL ratio is 1.0–1.5x
- Or use fixed USD: `HFT_STOP_LOSS_USD=4.0`, `HFT_TARGET_PROFIT_USD=5.0`

---

### HIGH: 3. RSI slope filter kills almost all entries

**File**: `core/engine.py` lines 162–168, method `entry_rsi_slope_allows()`

```python
self.entry_rsi_slope_filter_enabled = True  # default "1"
self.rsi_up_entry_max = 30.0   # RSI must be < 30 for UP entry
self.rsi_up_slope_min = 0.0    # RSI slope must be rising
self.rsi_down_entry_min = 70.0 # RSI must be > 70 for DOWN entry
self.rsi_down_slope_max = 0.0  # RSI slope must be falling
```

For a UP entry: RSI must be **below 30** (deeply oversold) AND rising.
For a DOWN entry: RSI must be **above 70** (deeply overbought) AND falling.

In a fast-moving 5-minute BTC prediction market, RSI spends very little time at these extremes. This is why trades are rare and the journal shows many missed opportunities.

Looking at the journal: entries DO happen with RSI at extreme values (0.45, 13.9, etc.) because the bypass via `rsi_agg_bypass` fires for aggressive edge. But normal entries (non-aggressive edge) are blocked.

**Fix**:
- Change `HFT_RSI_UP_ENTRY_MAX=50.0` and `HFT_RSI_DOWN_ENTRY_MIN=50.0` 
  (standard overbought/oversold bands, not extreme)
- OR disable the slope filter: `HFT_ENTRY_RSI_SLOPE_FILTER_ENABLED=0`
  and use the band-based RSI gate with `HFT_RSI_ENTRY_UP_LOW=20`, `HFT_RSI_ENTRY_UP_HIGH=80`

---

### HIGH: 4. Z-score monotonicity filter too strict

**File**: `core/engine.py` lines 472–484, method `entry_zscore_trend_ok()`

```python
self.entry_zscore_strict_ticks = 5  # default
```

Requires **5 consecutive strictly monotone** z-score ticks in the trade direction.
BTC price has noise; strict monotonicity across 5 ticks is very rare. This is another filter that suppresses valid entries.

**Fix**: Lower to 2–3 ticks: `HFT_ENTRY_ZSCORE_STRICT_TICKS=2`
Or disable: `HFT_ENTRY_ZSCORE_TREND_ENABLED=0`

---

### HIGH: 5. Speed acceleration filter over-constrains entries

**File**: `core/engine.py` lines 458–470, method `entry_speed_acceleration_ok()`

```python
self.entry_accel_min = 0.10  # must accelerate by at least 0.10
```

Combined with z-score monotonicity, both must be true simultaneously. Two independent momentum confirmations needed at once — significantly rare.

**Fix**: Disable one: `HFT_ENTRY_ACCEL_ENABLED=0`, rely on z-score OR edge threshold alone.

---

### MEDIUM: 6. Slot check on every tick with no minimum interval

**File**: `bot.py` line 183

```python
if SLOT_POLL_SEC <= 0.0 or (now - last_slot_check_time) >= SLOT_POLL_SEC:
```

When `SLOT_POLL_SEC=0` (default), the slot ID is fetched on **every single iteration**. A 5-minute slot changes once every 300 seconds. Checking it every millisecond wastes resources. Combined with issue #1 (blocking HTTP), this is compounded.

**Fix**: Add a minimum poll interval even when `SLOT_POLL_SEC=0`, e.g. check at most once per second:
```python
MIN_SLOT_POLL = 1.0  # seconds
if (now - last_slot_check_time) >= max(SLOT_POLL_SEC if SLOT_POLL_SEC > 0 else MIN_SLOT_POLL, MIN_SLOT_POLL):
```

---

### MEDIUM: 7. `entry_down_bid`/`entry_down_ask` not passed to close event journal

**File**: `core/engine.py` lines 1048–1054

The `entry_context` stores `entry_down_bid` and `entry_down_ask` but the close event result dict does NOT include these keys. Journal rows have empty `entry_down_bid`/`entry_down_ask` columns for all DOWN trades.

**Fix**: Add these two keys to the result dict in the CLOSE event:
```python
"entry_down_bid": self.entry_context.get("entry_down_bid"),
"entry_down_ask": self.entry_context.get("entry_down_ask"),
```

---

### MEDIUM: 8. TP hold timer blocks profitable exits

**File**: `core/engine.py` line 57

```python
self.pnl_tp_min_hold_sec = 6.0  # default, also combined with min_hold_sec=2.0
```

`_pnl_tp_hold_allows()` uses `max(min_hold_sec, pnl_tp_min_hold_sec) = max(2.0, 6.0) = 6 seconds`.
Many profitable trades (duration 2–5s) cannot exit via TP because the 6-second hold requirement isn't met. This forces them to eventually hit SL or timeout.

**Fix**: Lower `HFT_PNL_TP_MIN_HOLD_SEC` to `2.0` (match `min_hold_sec`) or eliminate it: `HFT_PNL_TP_MIN_HOLD_SEC=0`.

---

### LOW: 9. Exit reason priority logic gap

**File**: `core/engine.py` lines 998–1007

`should_close` fires when any of: `reaction_confirmed OR protective_stop OR timeout_no_reaction OR pnl_tp OR pnl_sl`. The reason assignment starts with `"REACTION_TP"` (covers `reaction_confirmed`) but uses `elif` for others. If only `pnl_tp` and `pnl_sl` are both True simultaneously, the code assigns `PNL_TP` correctly (it's checked before `PNL_SL`). No actual bug here, but the `reaction_confirmed` path (checking BTC oracle move) never appears in the journal — likely because `poly_mid` often comes back as `btc_oracle` (BTC price ~83000) and `entry_poly_mid` stores this correctly, but 0.3% BTC moves (≈$249) don't happen in 2–13 second holds. These thresholds (`poly_take_profit_move=0.003`, `poly_stop_move=0.0025`) are dead code in the current timeframe.

---

## Affected Components

| File | Lines | Issue |
|------|-------|-------|
| `core/selector.py` | 154–189 | Blocking `requests.get()` in `async def` |
| `core/engine.py` | 56 | `pnl_sl_pct=0.020` too tight |
| `core/engine.py` | 162–168 | RSI slope filter too restrictive |
| `core/engine.py` | 173–174 | Z-score monotonicity 5-tick requirement |
| `core/engine.py` | 57 | TP hold min 6s blocks fast wins |
| `core/engine.py` | 1048–1054 | Missing `entry_down_bid/ask` in close event |
| `bot.py` | 183 | Slot check every tick, no minimum interval |

---

## Proposed Solution (Priority Order)

### Fix 1 — Async HTTP in selector (latency, correctness)
Wrap all `requests.get()` calls in `asyncio.to_thread()` inside `selector.py` async functions.

### Fix 2 — Stop-loss parameter adjustment (win rate)
- `HFT_PNL_SL_PERCENT=0.05` (5%)
- `HFT_PNL_TP_PERCENT=0.07` (7%)  
- `HFT_PNL_TP_MIN_HOLD_SEC=2.0` (match min hold)

### Fix 3 — Relax RSI entry filter (trade frequency)
- `HFT_RSI_UP_ENTRY_MAX=50.0`
- `HFT_RSI_DOWN_ENTRY_MIN=50.0`
- `HFT_ENTRY_RSI_SLOPE_FILTER_ENABLED=0` (disable slope requirement, use band-based)

### Fix 4 — Relax z-score and acceleration filters (trade frequency)
- `HFT_ENTRY_ZSCORE_STRICT_TICKS=2`
- `HFT_ENTRY_ACCEL_ENABLED=0`

### Fix 5 — Slot poll minimum interval (CPU / latency)
Add `MIN_SLOT_POLL_SEC=1.0` so slot check doesn't run every tick.

### Fix 6 — Journal completeness (data quality)
Add `entry_down_bid`/`entry_down_ask` to the CLOSE event result dict.

---

## Expected Impact After Fixes

| Metric | Before | Expected After |
|--------|--------|----------------|
| Win rate | 33% | 45–55% |
| Trades/session | 15 | 30–60 |
| Avg SL exit % | 73% | 40–50% |
| Loop latency (no position) | +50–500ms per tick (HTTP) | ~0ms |
| Entry frequency | very rare (RSI<30 required) | moderate |
