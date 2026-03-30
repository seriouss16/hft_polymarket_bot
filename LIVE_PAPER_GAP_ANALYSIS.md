# HFT Bot: Paper vs Live Performance Gap Analysis

**Problem**: Bot shows excellent results in paper trading but fails in live mode.

**Root Cause**: Multiple systemic mismatches between simulated and real execution environments.

---

## Code verification (audit vs current tree)

This section records what was re-checked against the repository. **Not all gaps are “bugs”**: paper mode intentionally uses idealized fills; live mode uses the CLOB. Closing the *performance* gap requires either richer paper simulation (slippage/fees) or accepting live-only PnL.

| # | Topic | Status | Verified in code / config |
|---|--------|--------|---------------------------|
| 1 | Entry slippage vs paper `up_ask` | **Still applies** | `HFTEngine.process_tick` → `execute("BUY_UP", up_ask, …)` → `PnLTracker.log_trade` at book price. Live: `LiveExecutionEngine.execute` places limit at `best_ask + LIVE_BUY_PRICE_OFFSET`; `_poll_order` reprices with `+0.001`. |
| 2 | FAK SELL worst price vs bid | **Configurable** | `LIVE_FAK_SELL_WORST_BID_MULT` in `config/runtime.env` (default **0.90**). Lower = more aggressive crossing; raise toward **1.0** for a less punitive floor (still marketable). Implemented in `_place_fak_sell`. |
| 3 | Min shares / skip cooldown | **Partially mitigated** | `execute` clamps toward `POLY_CLOB_MIN_SHARES` and `_affordable_buy_shares`; reprice can still shrink size. Cooldown in `bot.py` when live BUY skipped. |
| 4 | Allowance before SELL | **Earlier doc was wrong** | `close_position` calls `ensure_conditional_allowance` before **each** GTC attempt and FAK retries; startup `ensure_allowances` for USDC. **Remaining risk**: ledger lag / pre-SELL balance 0, not missing allowance call. |
| 5 | Runtime parity | **Improved** | Shared keys live in `config/runtime.env` (merged by `bot.py` and on import by `core/live_engine.py` via `utils.env_merge`). `LIVE_ORDER_SIZE`, `LIVE_*` live settings should be set there explicitly. |
| 6 | Stale CLOB book | **Still applies** | Depends on `CLOB_BOOK_PULL_SEC`, failures, and backoff. No automatic “skip if book age > N s” unless added. |
| 7 | Live suppressed `log_trade(BUY)` metadata | **Fixed** | Live used to return only `{suppressed, side}` — **no** balance gates vs paper and **no** `book_px`/`exec_px`/`amount_usd`. Now `PnLTracker.log_trade` applies the **same** balance checks as SIM and returns **paper-equivalent** pricing fields (no balance mutation until `live_open()`). |

**Conclusion**: The structural paper/live execution gap **remains** for items 1 and 6 until you add simulation (e.g. paper slippage model) or operational guards (e.g. max book age). Items 2–5 are **better documented** and **partly addressed** in code/config. Item 7 removes a real skew in OPEN **metadata** between paper and live.

**Wall-clock**: The main loop still **`await`s** `live_exec.execute()` after OPEN, so fewer `process_tick` calls per second than paper while an order is in flight; only a non-blocking order pipeline would match paper’s tick rate (larger refactor).

---

## Critical Issues

### 1. Entry Price Slippage (CRITICAL)

**Paper Mode**:
- Uses `up_ask` from orderbook as fill price
- Assumes immediate, zero-slippage execution

**Live Mode**:
- Places limit order at `best_ask + LIVE_BUY_PRICE_OFFSET` (default 0.0 = at ask)
- BUT: Order may fill at worse price after re-pricing
- Re-price logic moves order by `+0.001` each attempt to chase the market
- **Result**: Live entry price can be 1-3 ticks worse than paper assumption

**Impact**: Every live entry is immediately underwater relative to paper baseline.

**Evidence**: `HFTEngine.process_tick` / `execute` uses `up_ask` for simulation; `LiveExecutionEngine.execute` places at `best_ask + LIVE_BUY_PRICE_OFFSET`; `_poll_order` reprices BUYs at `best_ask + 0.001` (see `LiveExecutionEngine._poll_order`).

---

### 2. Exit Price Degradation (CRITICAL)

**Paper Mode**:
- Exits at `down_bid` (for DOWN) or `up_bid` (for UP) from orderbook
- Assumes perfect limit order fill

**Live Mode**:
- SELL GTC limit placed at `best_bid + 0.002` (see `LiveExecutionEngine.close_position`).
- FAK fallback uses `worst_price = best_bid * LIVE_FAK_SELL_WORST_BID_MULT` (default **0.90** in `runtime.env`; clamped to 1.0). Tune **up** toward 1.0 to reduce exit haircut vs bid (still a market-style cross).
- **Result**: Exit can be worse than paper; magnitude depends on GTC vs FAK path and `LIVE_FAK_SELL_WORST_BID_MULT`.

**Impact**: Winners become losers or get reduced profits; losers get stopped out at much worse levels when FAK path triggers.

---

### 3. Minimum Share Constraint Cascade (HIGH)

**Problem**: Polymarket CLOB minimum order size is `POLY_CLOB_MIN_SHARES` (default 5).

**Live Mode Flow**:
1. Bot calculates desired shares from `LIVE_ORDER_SIZE` (e.g., $4 at 0.60 price = 6.67 shares)
2. After re-price or balance adjustment, shares may drop below 5
3. Order is rejected with "insufficient balance" even though USD value is sufficient
4. Bot enters `_live_skip_until` cooldown and stops trading

**Code Evidence**:
- [`core/live_engine.py:1698-1707`](hft_bot/core/live_engine.py:1698): checks `shares < poly_min_shares`
- [`core/live_engine.py:1002-1008`](hft_bot/core/live_engine.py:1002): re-price can reduce size below minimum
- [`bot.py:1084-1110`](hft_bot/bot.py:1084): skip cooldown triggered when budget too low

**Impact**: Live trading becomes intermittent; positions that should open get skipped; account can enter death spiral where balance drops, order size shrinks, gets rejected, balance drops further.

---

### 4. Balance/Allowance Synchronization Lag (HIGH)

**Polymarket Protocol Quirks**:
- USDC collateral allowance must be refreshed before each BUY
- Conditional token allowance must be refreshed before each SELL
- On-chain balance updates can lag CLOB fill reports by several seconds
- Protocol deducts fees in shares, so on-chain balance < CLOB fill size

**Live Mode Vulnerabilities** (updated):
- **Allowance**: `ensure_conditional_allowance(token_id)` is **called before each SELL placement attempt** in `close_position` (GTC loop + FAK retries), not only at BUY entry. Startup `ensure_allowances()` refreshes USDC collateral.
- **Ledger lag**: pre-SELL balance can still read `0` briefly; `_await_sellable_balance` and retry sleeps mitigate but do not eliminate races.
- **Bot**: deferred engine sync uses `apply_live_entry_after_fill()` after a confirmed fill; on failed BUY, `rollback_live_open_signal()` clears pending state (verify live path in `bot.py` main loop).

**Impact**: Residual failures are mostly **timing/API mismatch**, not “forgot to refresh allowance” in the current code.

---

### 5. Paper/Live Configuration Mismatch (MEDIUM)

**Observation**: `config/runtime.env` and `config/runtime_night.env` contain parameters that behave differently in live mode:

- `HFT_DEPOSIT_USD=4` (paper starting balance)
- Set `LIVE_ORDER_SIZE`, `HFT_MAX_POSITION_USD`, `LIVE_ACCOUNT_BALANCE`, and `LIVE_MAX_SESSION_LOSS` / `LIVE_MAX_DAILY_LOSS` (legacy) **explicitly** in `config/runtime.env` or layered `.env` so paper and live agree.
- `HFT_MAX_POSITION_USD=4` caps exposure

**Risk**: Paper simulation runs with $4 deposit and $4 trades, but live account may have different actual balance. The bot caps orders to `min(LIVE_ORDER_SIZE, HFT_MAX_POSITION_USD, current_balance)`, which can shrink orders below minimum share count.

---

### 6. Orderbook Data Staleness (MEDIUM)

**Live Mode**:
- CLOB orderbook pulled via HTTP every `CLOB_BOOK_PULL_SEC` (default from env, likely 0.5-1s)
- Rate limiting can cause failures, bot uses fallback but may trade on stale data
- [`bot.py:686-690`](hft_bot/bot.py:686): failures logged at most every 90s, but data may be 30+ seconds old

**Paper Mode**:
- Uses same aggregator but no HTTP calls; data is always fresh from WebSocket

**Impact**: Live entries based on stale quotes; price moved away from signal; immediate loss.

---

### 7. Entry Gate Inconsistency (MEDIUM)

**Paper Mode**:
- `HFTEngine._entry_outcome_price_allows()` checks per-outcome ask caps
- Uses `HFT_ENTRY_MIN_ASK_UP/DOWN` and `HFT_ENTRY_MAX_ASK_UP/DOWN`

**Live Mode**:
- `LiveExecutionEngine.execute()` calls `_paper_aligned_buy_price_allows()` which applies SAME gates
- BUT also checks `HFT_MAX_ENTRY_ASK` global cap
- [`core/live_engine.py:1663-1671`](hft_bot/core/live_engine.py:1663): two-layer gate can reject trades paper would take

**Configuration**:
- `runtime.env` line 220: `HFT_MAX_ENTRY_ASK=0.99` (very permissive)
- `runtime_night.env` lines 19-22: per-outcome caps set to 0.05-0.97

**Issue**: Night profile sets `HFT_ENTRY_MAX_ASK_UP=0.97` and `HFT_ENTRY_MAX_ASK_DOWN=0.97`. For DOWN token with typical price 0.30-0.40, this is fine. But if UP token is at 0.60, DOWN token is at 0.40, the gate checks `down_ask` against 0.97 which is always true. So this gate is likely not the blocker.

---

### 8. Latency and Feed Timing Disconnect (LOW)

**Paper Mode**:
- All timestamps from single asyncio loop clock
- No network latency

**Live Mode**:
- `aggregator.feed_timing()` computes staleness using local receive times
- `latency_ms` passed to engine can trigger entry blocks
- [`core/engine.py:1084-1088`](hft_bot/core/engine.py:1084): `entry_latency_allows_entry()` blocks if `latency_ms > entry_max_latency_ms`
- Night profile: `HFT_ENTRY_MAX_LATENCY_MS=2800` (2.8s) — very permissive
- Day profile: `HFT_ENTRY_MAX_LATENCY_MS=1350` (1.35s)

**Observation**: This gate is likely not the primary issue given the high thresholds.

---

## Execution Flow Comparison

### Paper Entry (Simulated)
```
1. Engine decides BUY_UP
2. pnl.log_trade("BUY", up_ask, amount_usd)
   - Deducts balance immediately
   - Records inventory at up_ask
   - No fill confirmation needed
3. Entry_time set immediately
4. Later: exit at down_bid (or up_bid) from orderbook
```

### Live Entry (Real)
```
1. Engine decides BUY_UP
2. `bot.py` `live_exec.execute(BUY_UP, token_up_id, budget_usd)` (line numbers drift — search `live_exec.execute`)
3. LiveExecutionEngine:
   a. Fetch best_bid/ask via HTTP (network round-trip)
   b. Check spread, ask cap, min shares
   c. Place GTC limit order at best_ask + offset
   d. `_poll_order`:
      - Polls every `LIVE_ORDER_FILL_POLL_SEC` (see `runtime.env`, often **0.4**)
      - If stale after `LIVE_ORDER_STALE_SEC` (often **3**), reprice up to `LIVE_ORDER_MAX_REPRICE` times
      - Each reprice: cancel old order, wait for fill race, place new order at best_ask + 0.001
   e. If filled (immediate or after polling), return (shares, avg_price)
4. `pnl.live_open(...)` then `hft_eng.apply_live_entry_after_fill(...)` — syncs engine entry_time, mids, `entry_exec_px` / book snapshot
```

**Key Difference**: Live entry can take 0.1-10+ seconds to fill, during which:
- Market may move against the position
- Engine may generate opposite exit signal (but position not yet recorded)
- Balance is locked but not yet reflected in PnL tracker

---

## Configuration Red Flags

From [`config/runtime.env`](hft_bot/config/runtime.env):

```bash
HFT_DEPOSIT_USD=4              # Very small account; any fee or rounding error matters
HFT_DEFAULT_TRADE_USD=4        # 100% of deposit per trade
HFT_MAX_POSITION_USD=4         # No position sizing flexibility
HFT_TRADE_PCT_OF_DEPOSIT=0     # No profit scaling to recover from losses
```

From [`config/runtime_night.env`](hft_bot/config/runtime_night.env):

```bash
HFT_BUY_EDGE=3.0               # Lower threshold than day (4.0) — more entries
HFT_ENTRY_MAX_LATENCY_MS=2800  # 2.8s staleness allowed — very high for HFT
HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS=2000  # 2s for soft_flow
```

**Live-specific config**: keep `LIVE_ORDER_SIZE`, `LIVE_ACCOUNT_BALANCE`, `LIVE_MAX_SESSION_LOSS` (or legacy `LIVE_MAX_DAILY_LOSS`), and all `LIVE_*` execution keys in `config/runtime.env` (or overrides in `.env`). Defaults are **not** hardcoded in Python — missing keys raise at startup (see `utils/env_config.py`).

---

## Failure Scenarios

### Scenario 1: Order Rejection Due to Minimum Shares
```
Account balance: $4.00
Price: 0.75
Desired shares: 4 / 0.75 = 5.33
After rounding down to 2 decimals: 5.33 shares (OK)
After reprice to 0.751: 4 / 0.751 = 5.32 shares (OK)
After balance drop to $3.50: 3.50 / 0.751 = 4.66 shares → REJECTED (< 5)
Bot enters skip cooldown, stops trading
```

### Scenario 2: SELL Fails Due to Ledger Lag (allowance usually OK)
```
BUY fills: 6 shares of UP token
Conditional allowance refreshed before SELL in close_position — but on-chain balance API still 0
SELL order placed: rejected or retries
Poll loop / _await_sellable_balance, eventually FAK
FAK sells at bid * LIVE_FAK_SELL_WORST_BID_MULT (default 0.90) → can be much worse than paper exit at bid
```

### Scenario 3: Partial Fill with Sub-Minimum Remainder
```
BUY order: 6 shares @ 0.60
First fill: 4 shares (partial)
Remaining: 2 shares < POLY_CLOB_MIN_SHARES (5)
_poll_order detects stale partial fill < min
Cancels BUY and FAK-SELLS the 4 shares at market
Result: Position never opens, but 4 shares sold at unfavorable price → loss
```

### Scenario 4: Stale Orderbook Causes Bad Entry
```
CLOB book pull fails due to rate limit (last successful 15s ago)
Bot uses stale down_ask = 0.35
Actual market: down_ask = 0.42
Engine sees edge = fast_price - poly_mid = 5.0 points → BUY_DOWN signal
Live order placed @ 0.35 + offset = 0.351
Order sits unfilled while market is at 0.42
After reprice attempts, order cancelled or filled at 0.42
Entry price 20% worse than expected → instant loss
```

---

## Recommended Fixes (Priority Order)

### 1. Align Entry/Exit Price Assumptions (URGENT)
- **Problem**: Paper uses ideal orderbook prices; live uses actual fill prices with slippage
- **Fix**: In live mode, pass actual fill prices back to engine for PnL calculation and trailing stops
- **Code**: [`core/engine.py:564-600`](hft_bot/core/engine.py:564) `apply_live_entry_after_fill()` already does this for entry; ensure exit also uses real fill price
- **Verify**: Check that `execute()` returns actual avg_price and that `live_close()` uses it

### 2. Fix Minimum Share Rounding (URGENT)
- **Problem**: Shares rounded down to 2 decimals can fall below minimum after balance adjustment
- **Fix**: In `LiveExecutionEngine.execute()`, after computing `shares`, ensure `shares >= poly_min` BEFORE affordability check, or use `ceil()` instead of `floor()` when near threshold
- **Code**: [`core/live_engine.py:1710`](hft_bot/core/live_engine.py:1710) `shares = float(int(shares * 100) / 100)` should be `max(poly_min, ceil(shares * 100) / 100)`

### 3. Add Pre-Entry Balance Buffer (HIGH)
- **Problem**: Balance can drop between order placement and fill due to fees or other trades
- **Fix**: In `_affordable_buy_shares()`, apply safety margin earlier; in `execute()`, re-check balance after reprice before placing new order
- **Config**: Add `LIVE_BUY_BALANCE_BUFFER_PCT=0.02` to reserve 2% USDC for fees/slippage

### 4. Ensure Allowance Refresh Before Every SELL (HIGH)
- **Problem**: Conditional allowance may expire or be insufficient
- **Fix**: In `close_position()`, call `ensure_conditional_allowance()` immediately before placing SELL, not just after BUY
- **Code**: [`core/live_engine.py:1539`](hft_bot/core/live_engine.py:1539) already does this in retry loop, but also needed in initial attempt

### 5. Add Chain Balance Confirmation Before SELL (HIGH)
- **Problem**: Ledger lag causes SELL to fail with 0 balance
- **Fix**: Increase `LIVE_CLOSE_WAIT_PENDING_SEC` to 10s; ensure `_await_sellable_balance()` is called before every SELL
- **Code**: [`core/live_engine.py:1468-1507`](hft_bot/core/live_engine.py:1468) already has logic, but may need longer delays

### 6. Synchronize Paper/Live Configuration (MEDIUM)
- **Problem**: Paper uses `HFT_DEFAULT_TRADE_USD` but live uses `LIVE_ORDER_SIZE` which may differ
- **Fix**: Set `LIVE_ORDER_SIZE` explicitly in runtime.env to match paper trade size
- **Or**: Make live mode inherit from `HFT_DEFAULT_TRADE_USD` if `LIVE_ORDER_SIZE` unset

### 7. Add Live-Specific Spread Gate Adjustment (MEDIUM)
- **Problem**: Live orderbook spread may be wider than paper's ideal mid
- **Fix**: Increase `HFT_MAX_ENTRY_SPREAD` for live mode only (e.g., 0.10 instead of 0.03)
- **Or**: Use `entry_liquidity_max_spread` from engine which already has separate profile values

### 8. Add Fill Price Feedback to Engine (MEDIUM)
- **Problem**: Engine's trailing TP/SL uses orderbook mid, not actual fill price
- **Fix**: When `apply_live_entry_after_fill()` is called, also update engine's `entry_poly_mid` and `entry_outcome_mid` based on fill price impact
- **Note**: Already done in [`core/engine.py:564-600`](hft_bot/core/engine.py:564), but verify exit also uses real prices

### 9. Add Orderbook Freshness Check (LOW)
- **Problem**: Stale CLOB data from rate-limited HTTP pulls
- **Fix**: In `bot.py` before calling `live_exec.execute()`, check if orderbook data timestamp is recent (< 2s). If stale, skip entry and log warning.
- **Code**: Add `book_age = time.time() - last_book_pull_time` check

### 10. Add Live-Only Diagnostics (LOW)
- **Problem**: Hard to debug live issues without detailed logs
- **Fix**: Enable `HFT_DEBUG_LOG_ENABLED=1` and set `DEBUG_LOG_PATH` to capture all tick data
- **Add**: Log every order placement, fill, reprice, cancellation with timestamps

---

## Immediate Action Items

1. **Check current live configuration**:
   ```bash
   cat hft_bot/.env | grep -E "LIVE_|HFT_MAX_POSITION|HFT_DEPOSIT|POLY_CLOB_MIN"
   ```

2. **Set explicit LIVE_ORDER_SIZE** matching paper trade size:
   ```bash
   LIVE_ORDER_SIZE=4.00  # or whatever matches HFT_DEFAULT_TRADE_USD
   ```

3. **Increase LIVE_MAX_SPREAD** to allow wider orderbook spreads:
   ```bash
   LIVE_MAX_SPREAD=0.10
   ```

4. **Verify account balance** is sufficient for minimum orders:
   ```bash
   # If balance is $20, can trade 5 shares @ 0.60 = $3
   # If balance is $4, 6 shares @ 0.67 = $4.02 → may be rejected
   ```

5. **Monitor logs for specific failure patterns**:
   - "Skip %s: budget %.2f USD → %.2f shares < CLOB minimum" → minimum share issue
   - "SELL GTC placement failed" → allowance/balance problem
   - "BUY stale with partial fill" → partial fill cascade
   - "CLOB book pull failed" → data staleness

6. **Temporarily disable aggressive re-pricing** to see if fills improve:
   ```bash
   LIVE_ORDER_MAX_REPRICE=0  # Don't chase; fail fast
   LIVE_MAX_BUY_REPRICE_SLIPPAGE=0.005  # Max 0.5% slippage
   ```

7. **Add safety buffer** to avoid order size dropping below minimum:
   ```bash
   LIVE_BUY_COLLATERAL_SAFETY=0.95  # Only use 95% of balance
   ```

---

## Testing Recommendations

1. **Dry-run with real orderbook but simulated fills**:
   - Set `LIVE_MODE=0` but use live CLOB data feed
   - Compare paper PnL vs what live PnL would be with actual fill prices

2. **Add fill price logging**:
   ```python
   # In bot.py after live_exec.execute()
   logging.info("LIVE FILL: side=%s shares=%.4f px=%.4f notional=%.4f",
                signal, filled_sh, filled_px, filled_sh * filled_px)
   ```

3. **Track slippage metrics**:
   - Entry: `(filled_px - up_ask) / up_ask`
   - Exit: `(filled_px - exit_book_px) / exit_book_px`

4. **Run with single trade size** to isolate issues:
   ```bash
   HFT_DEFAULT_TRADE_USD=2.00
   LIVE_ORDER_SIZE=2.00
   HFT_MAX_POSITION_USD=2.00
   ```

---

## Conclusion

The paper-to-live gap is **not** due to strategy logic but to **execution infrastructure**:

1. **Slippage** on both entry and exit is severely underestimated
2. **Minimum share constraints** cause order rejections and trading halts
3. **Balance/allowance lag** causes SELL failures and forced emergency exits
4. **Configuration mismatch** between paper parameters and live account size

**Most likely root cause**: Combination of #1 and #2. Live entries fill at worse prices than paper assumes, immediately putting positions in loss. Then trailing stops trigger prematurely or PnL-based exits fire at wrong levels. Meanwhile, shrinking balance causes orders to be rejected, creating a death spiral.

**Priority fix**: Align paper PnL calculation to use realistic fill prices (including slippage and fees) before attempting live trading. The current paper simulation is overly optimistic and does not reflect CLOB realities.

**After this audit**: (1) FAK worst bid multiplier is **configurable** (`LIVE_FAK_SELL_WORST_BID_MULT`). (2) Section 4 in the original text was **incorrect** about allowance — the code refreshes before SELL. (3) **Paper vs live price path** (#1, #6) still differs by design until you add a paper slippage / execution model or stricter live guards.
