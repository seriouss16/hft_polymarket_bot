# HFT Bot — Live Trading Documentation

> **Simulation mode** (`LIVE_MODE=0`) runs by default. No real orders are placed until `LIVE_MODE=1` is set and valid credentials are provided.

---

## Table of Contents

1. [Overview](#overview)
2. [Architecture](#architecture)
3. [Data Pipeline](#data-pipeline)
4. [Signal Generation](#signal-generation)
5. [Strategies](#strategies)
6. [Entry Logic and Filters](#entry-logic-and-filters)
7. [Exit Logic](#exit-logic)
8. [Order Lifecycle (Live Mode)](#order-lifecycle-live-mode)
9. [Risk Controls](#risk-controls)
10. [Configuration Reference](#configuration-reference)
11. [Running the Bot](#running-the-bot)
12. [Switching to Live Mode](#switching-to-live-mode)
13. [Log Interpretation](#log-interpretation)
14. [Tests](#tests)

---

## Overview

The bot trades 5-minute Bitcoin Up/Down binary prediction markets on [Polymarket](https://polymarket.com).
The core idea is **latency arbitrage**: Polymarket's oracle price (PolyRTDS) lags the CEX spot price (Coinbase / Binance) by hundreds of milliseconds. When the gap between the CEX price and the oracle exceeds a configurable threshold, the bot enters a directional position on the Polymarket CLOB and exits when the gap closes or a trend reversal is detected.

### Key numbers (simulation baseline)

| Metric | Value |
|---|---|
| Deposit | $100 |
| Trade size | $10 per position |
| Target win rate | ≥ 60 % |
| Max session loss (live) | configured via `LIVE_MAX_SESSION_LOSS` |
| Max drawdown | configured via `MAX_DRAWDOWN_PCT` |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                            bot.py  (main loop)                      │
│                                                                     │
│  ┌──────────────┐   ┌──────────────┐   ┌───────────────────────┐   │
│  │ FastExchange │   │  PolyOrderBook│   │   MarketSelector      │   │
│  │  Provider    │   │  (CLOB + WS) │   │   (slot discovery)    │   │
│  │ CB + Binance │   │              │   │                       │   │
│  └──────┬───────┘   └──────┬───────┘   └───────────┬───────────┘   │
│         │                  │                        │               │
│         ▼                  ▼                        │               │
│  ┌──────────────────────────────────┐              │               │
│  │       FastPriceAggregator        │◄─────────────┘               │
│  │  (CB anchor, BNC lead, smart     │                               │
│  │   blending, zscore, trend)       │                               │
│  └──────────────┬───────────────────┘                               │
│                 │                                                   │
│                 ▼                                                   │
│  ┌──────────────────────────────────┐                               │
│  │          StrategyHub             │                               │
│  │  ┌─────────────────────────────┐ │                               │
│  │  │  PhaseRouterStrategy        │ │  ◄── HFT_ACTIVE_STRATEGY      │
│  │  │  ┌──────────┐ ┌──────────┐  │ │                               │
│  │  │  │ latency  │ │soft_flow │  │ │  auto-selected per market     │
│  │  │  │ profile  │ │ profile  │  │ │  phase (speed/edge/age)       │
│  │  │  └──────────┘ └──────────┘  │ │                               │
│  │  └─────────────────────────────┘ │                               │
│  │  ┌─────────────────────────────┐ │                               │
│  │  │  LatencyArbitrageStrategy   │ │  ◄── also registered          │
│  │  └─────────────────────────────┘ │                               │
│  └──────────────┬───────────────────┘                               │
│                 │  decision (OPEN / CLOSE / None)                   │
│                 ▼                                                   │
│  ┌──────────────────────────────────┐                               │
│  │           HFTEngine              │                               │
│  │  (signal eval, entry/exit logic, │                               │
│  │   trailing TP/SL, regime filter) │                               │
│  └────────┬─────────────────────────┘                               │
│           │                                                         │
│    ┌──────┴──────────┐                                              │
│    │                 │                                              │
│    ▼                 ▼                                              │
│  PnLTracker    LiveExecutionEngine  ◄── LIVE_MODE=1 only            │
│  (live P&L,    (order lifecycle,                                    │
│  drawdown,      reprice, emergency                                  │
│  regime)        exit, fill tracking)                                │
└─────────────────────────────────────────────────────────────────────┘
```

### Module map

| Module | Role |
|---|---|
| `bot.py` | Entry point; asyncio event loop, wires all components |
| `core/engine.py` | Signal evaluation, entry/exit conditions, trailing TP/SL |
| `core/executor.py` | Live/sim P&L tracker, regime filter, inventory |
| `core/live_engine.py` | Live CLOB execution, order tracking, reprice, emergency exit, balance verification |
| `core/risk_engine.py` | Drawdown guard, position sizing cap, loss cooldown |
| `core/selector.py` | Discovers active 5-min BTC Up/Down slots from Gamma API |
| `core/strategy_hub.py` | Registers strategies, routes tick data, A/B windows |
| `core/strategies/phase_router_strategy.py` | Selects `latency` or `soft_flow` profile per tick |
| `core/strategies/latency_strategy.py` | Pure latency-arb signal generator |
| `core/market_phase.py` | Classifies market phase (calm / volatile / directional) |
| `data/providers.py` | Coinbase + Binance WebSocket price feeds |
| `data/poly_clob.py` | Polymarket RTDS WebSocket (oracle price) + CLOB HTTP pulls |
| `data/aggregator.py` | Fuses CEX feeds; computes z-score, trend, speed |
| `ml/indicators.py` | RSI computation, dynamic bands |
| `ml/model.py` | Optional async LSTM price predictor (Keras / ONNX) |
| `utils/stats.py` | Session and final performance reports |
| `utils/trade_journal.py` | Appends closed trades to `reports/trade_journal.csv` |

---

## Data Pipeline

```
Coinbase WS ──┐
              ├──► FastPriceAggregator
Binance WS ───┘        │
                        │  fast_price  (CEX anchor)
                        │  zscore      (vs rolling window)
                        │  trend       (UP / DOWN / FLAT)
                        │  speed       (pts/s)
                        │
Polymarket RTDS WS ────►│  poly_btc    (oracle price)
                        │
                        │  edge = fast_price - poly_btc
                        ▼
                   HFTEngine.process_tick()
```

- **Coinbase** is the primary anchor price (best bid/ask mid).
- **Binance** provides a leading signal; it may be blended when `USE_SMART_FAST=1`.
- **PolyRTDS** is the Polymarket oracle; it typically lags CEX by 100–1 500 ms.
- **Edge** (`fast_price − poly_btc`) is the core signal. Positive edge → UP opportunity; negative → DOWN.
- The aggregator also computes a **z-score** of the edge over a rolling window and a **trend** (direction + speed + age in seconds).

---

## Signal Generation

### Edge threshold check

```
edge > HFT_BUY_EDGE (4.0 pts)  →  BUY_UP  candidate
edge < -HFT_BUY_EDGE           →  BUY_DOWN candidate
|edge| < HFT_NOISE_EDGE (0.8)  →  noise, skip
```

### Aggressive entry

When `|edge| >= HFT_AGGRESSIVE_EDGE_MULT × HFT_BUY_EDGE` (≥ 12 pts by default), the engine attempts entry even if some secondary filters would normally block it, as long as the spread gate passes.

---

## Strategies

### PhaseRouterStrategy (default: `HFT_ACTIVE_STRATEGY=phase_router`)

Selects one of two sub-profiles per tick based on real-time market conditions:

```
Market conditions
      │
      ├── trend=FLAT                        → latency profile
      ├── |speed| > HFT_PHASE_SOFT_MAX_ABS_SPEED (55)  → latency profile
      ├── |edge| > HFT_PHASE_SOFT_MAX_ABS_EDGE (18)    → latency profile
      ├── stale > HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS   → latency profile
      └── otherwise                         → soft_flow profile
```

**latency profile** — fast, reactive, uses tighter filters, tolerates higher speed/edge.

**soft_flow profile** — slower, more selective. Requires:
- Directional trend (UP or DOWN) held for ≥ `HFT_PHASE_SOFT_MIN_TREND_AGE_SEC` (1.8 s)
- `|edge| ≥ HFT_SOFT_BUY_EDGE` (10 pts)
- Feed staleness ≤ `HFT_SOFT_ENTRY_MAX_LATENCY_MS` (750 ms)

### LatencyArbitrageStrategy

Wraps the same `HFTEngine` as paper mode. Live and paper both use `HFT_ACTIVE_STRATEGY` (e.g. `phase_router` or `latency_arbitrage`) — there is no separate live-only strategy switch.

---

## Entry Logic and Filters

Entry is evaluated on every tick in `HFTEngine.process_tick()`. All conditions below must pass:

### 1. Spread gate (`spread_gate`)

Blocks entry when market quality is poor:

| Check | Parameter |
|---|---|
| Feed staleness (max age of CB/Poly/BN data) | `HFT_ENTRY_MAX_LATENCY_MS` = 1 350 ms |
| Cross-feed skew (CB recv vs Poly recv) | `HFT_ENTRY_MAX_SKEW_MS` = 0 (disabled) |
| CLOB spread > max | `HFT_MAX_ENTRY_SPREAD` = 0.05 |
| Book liquidity spread | `HFT_ENTRY_LIQUIDITY_MAX_SPREAD` = 0.08 |
| CEX speed z-score monotonicity | `HFT_ENTRY_ZSCORE_TREND_ENABLED` |
| Edge-jump spoofing guard | `HFT_ENTRY_MAX_EDGE_JUMP_PTS` = 14 pts |
| Trend age after flip | `HFT_TREND_FLIP_MIN_AGE_SEC` |

### 2. RSI filter

- Entry blocked if RSI is outside `[HFT_RSI_ENTRY_UP_LOW, HFT_RSI_ENTRY_UP_HIGH]` for UP positions.
- `HFT_RSI_ALLOW_BYPASS_AGGRESSIVE_EDGE=1` lets aggressive signals skip the RSI check.

### 3. CEX order book imbalance

When `HFT_ENTRY_CEX_IMBALANCE_ENABLED=1`:
- BUY_UP requires bid/ask imbalance ≥ `HFT_CEX_IMBALANCE_UP_MIN` (0.70)
- BUY_DOWN requires imbalance ≤ `HFT_CEX_IMBALANCE_DOWN_MAX` (0.30)

### 4. Ask price caps

- UP position: ask must be in `[HFT_ENTRY_MIN_ASK_UP, HFT_ENTRY_MAX_ASK_UP]` (0.08 – 0.97)
- DOWN position: ask must be in `[HFT_ENTRY_MIN_ASK_DOWN, HFT_ENTRY_MAX_ASK_DOWN]` (0.08 – 0.97)

### 5. Regime filter

Tracks the last `HFT_RECENT_TRADES_FOR_REGIME` (8) closed trades. If win rate falls below `HFT_BAD_REGIME_WINRATE` (0.35), entry is blocked for `HFT_REGIME_COOLDOWN_SEC` (60 s). The `soft_flow` profile can bypass this (`HFT_REGIME_BYPASS_SOFT_FLOW=1`).

### 6. Timing guards

- No entry in first `HFT_NO_ENTRY_FIRST_SEC` (e.g. 5 s) or last `HFT_NO_ENTRY_LAST_SEC` of a slot. If unset, the last window defaults to **78 s (1.3 min)** before slot end — unstable near resolution; at startup, starting inside this window means no entries until the next slot boundary.
- Cooldown between entries: `HFT_COOLDOWN_SEC` (0.5 s).
- Re-entry after close: `HFT_POST_CLOSE_REENTRY_COOLDOWN_SEC` (2 s).

---

## Exit Logic

Exits are evaluated on every tick while a position is open. First matching condition wins:

### Priority order

```
1. SLOT_EXPIRY_99C       — market settled at 99¢ (position side won)
2. REACTION_TP           — poly book moved > HFT_POLY_TP_MOVE from entry
3. REACTION_STOP         — poly book moved > HFT_POLY_SL_MOVE against position
4. TRAILING_TP           — profit fell pullback_pct from peak (after activate_usd)
5. TRAILING_SL           — trailing SL floor hit (ratcheted up from breakeven)
6. RSI_RANGE_EXIT        — RSI exited band with profit > min profit threshold
7. RSI_EXTREME_EXIT      — RSI hit extreme zone (>90 or <10)
8. TREND_FLIP_EXIT       — trend reversed against position
9. REACTION_TIMEOUT      — position held > HFT_REACTION_TIMEOUT_SEC (10 s) with no TP
10. PNL_TP / PNL_SL      — absolute PnL thresholds
```

### Trailing TP/SL detail

```
Position open
      │
      ▼
unrealized >= HFT_TRAILING_TP_ACTIVATE_USD (0.02$)
      │  YES
      ▼
_peak_unrealized tracks new high each tick
      │
      ├── peak drops by pullback_pct (35%) AND >= min_pullback_usd (0.015$)
      │      → TRAILING_TP exit
      │
      └── peak >= HFT_TRAILING_SL_BREAKEVEN_AT_USD (0.04$)
               │
               ▼
         _trailing_sl_floor set to breakeven (entry price)
         then ratchets up by HFT_TRAILING_SL_STEP_USD (0.03$)
         locking HFT_TRAILING_SL_STEP_LOCK_PCT (50%) of gain
               │
               └── unrealized < _trailing_sl_floor
                      → TRAILING_SL exit
```

---

## Order Lifecycle (Live Mode)

In live mode, simulation is fully suppressed. `PnLTracker` records only confirmed CLOB fills via `live_open()` / `live_close()`. The `LiveExecutionEngine` manages all order state.

### BUY flow (`execute()`)

```
execute(signal, token_id, budget_usd)
        │
        ├── ask price check: same as paper — best_ask < HFT_MAX_ENTRY_ASK and HFT_ENTRY_MIN/MAX_ASK_* for UP vs DOWN
        ├── spread check: best_ask - best_bid ≤ max_spread
        ├── signal check: must be BUY_UP or BUY_DOWN
        ├── budget check: budget_usd / best_ask ≥ POLY_CLOB_MIN_SHARES
        │
        ▼
_place_limit_raw(BUY, price=best_ask - 0.002, shares)
        │
        ├── immediate_fill=True (status=matched in post_order response)
        │       └── skip poll, go straight to balance verification
        │
        └── immediate_fill=False
                └── await _poll_order(tracked)
        │
        ▼
  Order CANCELLED / FAILED?
        │  YES → skip, no balance check needed
        │
        ▼
  filled > 0 (FILLED or PARTIAL)?
        │  NO  → skip
        │
        ▼
  Wait for on-chain CTF balance to settle
  (retry loop: 0.3 → 0.5 → 0.8 → 1.0 → 1.5 s, max ~4 s)
        │
        ├── balance > 0 confirmed
        │       ├── balance ≥ POLY_CLOB_MIN_SHARES
        │       │       └── 🟢 return (actual_balance, avg_price)
        │       └── balance < POLY_CLOB_MIN_SHARES (protocol fee ate too many shares)
        │               └── 🔴 FAK-sell residual → skip (no open position)
        │
        └── balance still 0 after all retries (API lag)
                └── trust CLOB-reported fill, proceed with filled shares
```

**Note on protocol fee:** Polymarket deducts a small fee in CTF shares at fill time.
The actual spendable balance is always slightly below the CLOB-reported `filled_size`.
The balance verification loop accounts for the settlement delay (observed up to ~600 ms for immediate fills).

### SELL flow (`close_position()`)

```
close_position(token_id, size)
        │
        ▼
  Query on-chain CTF balance
        ├── balance > 0 and balance < size → correct size down (fee adjustment)
        ├── balance = 0 (API lag)          → keep original size, proceed
        └── balance ≥ size                 → use original size
        │
        ├── size < POLY_CLOB_MIN_SHARES (5)
        │       └── 🔴 FAK market SELL (no minimum size restriction)
        │
        └── size ≥ POLY_CLOB_MIN_SHARES
                └── GTC limit at ``best_bid + LIVE_SELL_GTC_OFFSET_FROM_BID`` (default **−0.002**, i.e. slightly below top bid for a marketable sell)
                        │
                        ├── placement OK → _poll_order(SELL)
                        │       └── partial remainder < min → FAK for remainder
                        │
                        └── placement FAILED → FAK fallback → emergency_exit
```

### `_poll_order` loop

```
poll every LIVE_ORDER_FILL_POLL_SEC (0.4 s)
        │
        ├── status = matched / filled   → FILLED, done
        │
        ├── status = partially_matched  → update filled_size, reset stale timer
        │
        └── STALE (age > LIVE_ORDER_STALE_SEC = 3.0 s)
                │
                ├── BUY + filled < POLY_CLOB_MIN_SHARES
                │       └── cancel BUY + FAK-sell partial residual → report as skip
                │
                ├── SELL + remaining < POLY_CLOB_MIN_SHARES
                │       └── FAK-sell remainder directly
                │
                ├── reprice_count < LIVE_ORDER_MAX_REPRICE (2)
                │       └── cancel + new order at current best price
                │           (preserves accumulated filled_size)
                │
                └── reprice exhausted → _emergency_exit_order()
```

### Emergency exit triggers

| Trigger | Action |
|---|---|
| Order stale after max reprice | `_emergency_exit_order()`: FAK for SELL, aggressive limit for BUY |
| Engine CLOSE decision (live) | `close_position()` → tracked SELL order |
| Shutdown with open position | `emergency_exit()` → cancel all pending + aggressive SELL |
| Crash / exception in main loop | Same via `finally` block |
| All SELL attempts fail | Force-clear `PnLTracker` state to prevent phantom position loop |

### Allowances

At startup `ensure_allowances()` sets the USDC (COLLATERAL) spending approval.
After every confirmed BUY fill `ensure_conditional_allowance(token_id)` sets the CTF token
(CONDITIONAL) approval for that specific token so the subsequent SELL is accepted by the CLOB.

### Heartbeat

A background task calls `client.post_heartbeat()` every 5 s to keep the CLOB session alive
and prevent automatic order cancellation.

### Order status states

| State | Meaning |
|---|---|
| `PENDING` | Order placed, awaiting fill |
| `FILLED` | Fully filled — done |
| `PARTIAL` | Partially filled — continues polling |
| `STALE` | Exceeded `LIVE_ORDER_STALE_SEC` without fill |
| `CANCELLED` | Cancelled (by reprice or emergency); no balance check needed |
| `FAILED` | Placement or reprice failed |

---

## Risk Controls

Three independent layers:

### 1. LiveRiskManager (session realized PnL cap)

```python
if live_pnl <= LIVE_MAX_SESSION_LOSS:  # limit is negative, e.g. -50 USD
    block all new entries
```

When the limit is reached, the main loop exits with shutdown reason `session_loss_limit`; the `finally` block runs `show_final_report` (same as normal shutdown).

### 2. RiskEngine (session drawdown)

```python
if drawdown_from_peak > MAX_DRAWDOWN_PCT:
    block all entries
if loss trade closed:
    cooldown LOSS_COOLDOWN_SEC
max_notional = equity × MAX_POSITION_PCT
```

### 3. Regime filter (rolling win rate)

```python
if recent_winrate < HFT_BAD_REGIME_WINRATE (0.35):
    block entries for HFT_REGIME_COOLDOWN_SEC (60 s)
```

### 4. Live skip cooldown

After a failed live BUY (order not placed or rejected), entries are blocked for
`HFT_LIVE_SKIP_COOLDOWN_SEC` (30 s) to prevent retry spam.

---

## Configuration Reference

### Core sizing

| Key | Default | Description |
|---|---|---|
| `HFT_DEPOSIT_USD` | 100 | Starting equity (must match `LIVE_ACCOUNT_BALANCE` in live mode) |
| `HFT_DEFAULT_TRADE_USD` | 10 | Base notional per trade |
| `HFT_TRADE_PCT_OF_DEPOSIT` | 0 | If > 0: trade size scales with current balance |
| `HFT_MAX_POSITION_USD` | 100 | Max open notional |

### Signal thresholds

| Key | Default | Description |
|---|---|---|
| `HFT_BUY_EDGE` | 4.0 | Min edge (pts) to open a position |
| `HFT_NOISE_EDGE` | 0.8 | Edge below this is ignored |
| `HFT_AGGRESSIVE_EDGE_MULT` | 3.0 | Multiplier for aggressive entry bypass |

### Trailing TP/SL

| Key | Default | Description |
|---|---|---|
| `HFT_TRAILING_TP_ENABLED` | 1 | Enable trailing take-profit |
| `HFT_TRAILING_TP_ACTIVATE_USD` | 0.02 | Minimum profit before trail activates |
| `HFT_TRAILING_TP_PULLBACK_PCT` | 0.35 | Exit when profit drops this fraction from peak |
| `HFT_TRAILING_TP_MIN_PULLBACK_USD` | 0.015 | Minimum pullback in USD to trigger exit |
| `HFT_TRAILING_SL_ENABLED` | 1 | Enable trailing stop-loss |
| `HFT_TRAILING_SL_BREAKEVEN_AT_USD` | 0.04 | Move SL to breakeven when profit ≥ this |
| `HFT_TRAILING_SL_STEP_USD` | 0.03 | SL ratchets up by this step |
| `HFT_TRAILING_SL_STEP_LOCK_PCT` | 0.50 | Fraction of gain locked per step |

### Hold and exits

| Key | Default | Description |
|---|---|---|
| `HFT_MIN_HOLD_SEC` | 3.0 | Minimum position hold time |
| `HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC` | 2.5 | Min hold before trend-flip exit fires |
| `HFT_OPPOSITE_TREND_EXIT_MIN_ABS_EDGE` | 3.0 | Minimum opposing edge to trigger flip exit |
| `HFT_POST_CLOSE_REENTRY_COOLDOWN_SEC` | 2 | Cooldown after close before re-entry |
| `HFT_REACTION_TIMEOUT_SEC` | 10.0 | Force-close if no TP after N seconds |

### Phase router

| Key | Default | Description |
|---|---|---|
| `HFT_ENABLE_PHASE_ROUTING` | 1 | Enable PhaseRouterStrategy |
| `HFT_PHASE_FORCE_PROFILE` | _(empty)_ | Force `soft_flow` or `latency`; empty = auto |
| `HFT_PHASE_SOFT_MIN_TREND_AGE_SEC` | 1.8 | Trend must be this old for soft_flow entry |
| `HFT_PHASE_SOFT_MAX_ABS_SPEED` | 55.0 | Speed above this → switch to latency profile |
| `HFT_PHASE_SOFT_MAX_ABS_EDGE` | 18.0 | Edge above this → switch to latency profile |
| `HFT_SOFT_BUY_EDGE` | 10.0 | Min edge for soft_flow entries |

### Live execution and order lifecycle

| Key | Default | Description |
|---|---|---|
| `LIVE_ORDER_FILL_POLL_SEC` | 0.4 | Seconds between fill status polls |
| `LIVE_ORDER_STALE_SEC` | 3.0 | Seconds before unfilled order is considered stale |
| `LIVE_ORDER_MAX_REPRICE` | 2 | Max reprice attempts before emergency exit |
| `LIVE_ORDER_SIZE` | 10 | Fallback order size in USD (used when budget not set) |
| `LIVE_MAX_SPREAD` | 0.05 | Max CLOB spread to allow execution |
| `HFT_MAX_ENTRY_ASK` | 0.99 | Global ceiling: best_ask must be **below** this (same as paper `_entry_ask_allows_open`) |
| `HFT_ENTRY_MIN_ASK_UP` / `HFT_ENTRY_MAX_ASK_UP` | _(per runtime)_ | Per-outcome band for UP token (same gates as paper `HFTEngine`) |
| `HFT_ENTRY_MIN_ASK_DOWN` / `HFT_ENTRY_MAX_ASK_DOWN` | _(per runtime)_ | Per-outcome band for DOWN token |
| `POLY_CLOB_MIN_SHARES` | 5 | Polymarket minimum GTC order size in shares |
| `POLY_SIGNATURE_TYPE` | 2 | Wallet signature type (2 = EOA with proxy) |
| `HFT_LIVE_SKIP_COOLDOWN_SEC` | 30 | Cooldown after a failed live BUY |

**Latency tuning (optional):** Lower `LIVE_ORDER_FILL_POLL_SEC` for faster fill detection (more API load). When `HFT_SLOT_POLL_SEC=0`, slot/market checks are still capped by `HFT_MIN_SLOT_POLL_SEC` (default 1 s) to avoid hammering Gamma—reduce for quicker slot-boundary reaction. `LIVE_BALANCE_CONFIRM_DELAYS_SEC` overrides the default backoff after a CLOB-reported BUY when confirming on-chain balance (comma-separated seconds; shorter = faster but higher false-abort risk if RPC lags). `LIVE_HEARTBEAT_INTERVAL_SEC` defaults to 5 s; keep ≤15 s (Polymarket requirement).

| Key | Default | Description |
|---|---|---|
| `HFT_MIN_SLOT_POLL_SEC` | 1.0 | Min seconds between slot/market resolution checks when `HFT_SLOT_POLL_SEC=0` |
| `LIVE_BALANCE_CONFIRM_DELAYS_SEC` | `0,0.15,0.35,0.6,1,1.5` | On-chain balance retry delays after BUY (comma-separated) |
| `LIVE_HEARTBEAT_INTERVAL_SEC` | 5.0 | CLOB heartbeat interval (must stay ≤15) |
| `LIVE_BUY_PRICE_OFFSET` | 0 | Added to best ask for BUY limit (0 = at ask; positive crosses spread) |
| `LIVE_SELL_GTC_OFFSET_FROM_BID` | −0.002 | GTC SELL limit = `best_bid + this` (negative = below bid, more marketable) |
| `LIVE_FAK_SELL_WORST_BID_MULT` | 0.995 | FAK SELL worst acceptable price ≈ `best_bid × mult` (was 0.90; higher = less haircut) |

### Risk limits

| Key | Default | Description |
|---|---|---|
| `LIVE_MAX_SESSION_LOSS` | -1.0 | Stop trading when session realized PnL ≤ this (negative USD; set ~50% of deposit) |
| `LIVE_ACCOUNT_BALANCE` | _(required)_ | Actual Polymarket USDC balance; must match `HFT_DEPOSIT_USD` |
| `MAX_DRAWDOWN_PCT` | 0.15 | Stop trading when session drawdown > 15% |
| `MAX_POSITION_PCT` | 0.10 | Max notional as fraction of equity |
| `LOSS_COOLDOWN_SEC` | 30 | Cooldown after a losing trade |

### Regime filter

| Key | Default | Description |
|---|---|---|
| `HFT_REGIME_FILTER_ENABLED` | 1 | Enable rolling win-rate regime block |
| `HFT_RECENT_TRADES_FOR_REGIME` | 8 | Number of recent trades to evaluate |
| `HFT_GOOD_REGIME_WINRATE` | 0.42 | Min WR to exit bad regime |
| `HFT_BAD_REGIME_WINRATE` | 0.35 | WR below this triggers regime block |
| `HFT_REGIME_COOLDOWN_SEC` | 60 | Block duration after bad regime detected |
| `HFT_REGIME_BYPASS_SOFT_FLOW` | 1 | Allow soft_flow entries during bad regime |

---

## Running the Bot

### Prerequisites

```bash
uv sync
```

Required packages: `websockets`, `requests`, `numpy`, `uvloop`, `py_clob_client`.

### Simulation mode (safe default)

```bash
cd hft_bot
uv run bot.py
```

Logs appear in `reports/logs/bot_DDMMYY_HHMMSS.log` (last 20 kept).
Trade records append to `reports/trade_journal.csv`.

### Force a single strategy profile

```bash
HFT_PHASE_FORCE_PROFILE=latency uv run bot.py
HFT_PHASE_FORCE_PROFILE=soft_flow uv run bot.py
```

---

## Switching to Live Mode

### 1. Credentials

Create `hft_bot/.env` (never commit — it is `.gitignore`d):

```env
PRIVATE_KEY=0x<your_polygon_private_key>
POLY_FUNDER_ADDRESS=0x<your_funder_wallet>
POLY_SIGNATURE_TYPE=2

LIVE_MODE=1
HFT_DEPOSIT_USD=<your_balance>
LIVE_ACCOUNT_BALANCE=<your_balance>
HFT_DEFAULT_TRADE_USD=<trade_size>
LIVE_ORDER_SIZE=<trade_size>
HFT_MAX_POSITION_USD=<max_position>
LIVE_MAX_SESSION_LOSS=-<max_loss>
```

`.env` overrides `config/runtime.env` — any key set in `.env` takes precedence.

> **Do not** set `POLIMARKET_API_KEY/SECRET/PASSPHRASE` manually. The bot always
> derives credentials from `PRIVATE_KEY` via `create_or_derive_api_creds()` to avoid
> stale-key errors after Polymarket key rotations.

### 2. Checklist before first live run

- [ ] `PRIVATE_KEY` and `POLY_FUNDER_ADDRESS` set and confirmed on Polygon mainnet (chain_id=137)
- [ ] `POLY_SIGNATURE_TYPE=2` matches wallet type
- [ ] `HFT_DEPOSIT_USD` equals `LIVE_ACCOUNT_BALANCE` equals your actual Polymarket USDC balance
- [ ] `LIVE_MAX_SESSION_LOSS` set to an amount you can afford to lose in a session (e.g. 50% of deposit). Legacy alias: `LIVE_MAX_DAILY_LOSS`.
- [ ] `LIVE_ORDER_SIZE` and `HFT_DEFAULT_TRADE_USD` set consistently
- [ ] `HFT_MAX_POSITION_USD` ≥ `LIVE_ORDER_SIZE` (otherwise bot will never trade)
- [ ] `py_clob_client` installed: included in `uv sync`
- [ ] Run at least one simulation session and review `reports/trade_journal.csv`

### 3. Live vs simulation differences

| Aspect | Simulation | Live |
|---|---|---|
| `strategy_hub.process_tick` inputs | Same feeds, book, `meta_enabled`, `seconds_to_expiry`, anchor | **Same** — live skip cooldown does **not** disable `meta_enabled` (only suppresses CLOB placement in code) |
| Order placement | Log only (`[SIM LIMIT]`) | Real GTC limit on Polymarket CLOB |
| Fill tracking | Not tracked | Polled every `LIVE_ORDER_FILL_POLL_SEC` |
| PnL recording | `log_trade()` on every tick | `live_open()` / `live_close()` only after CLOB confirmation |
| Balance verification | N/A | `fetch_conditional_balance()` with retry after each BUY fill |
| CONDITIONAL allowance | N/A | `ensure_conditional_allowance(token_id)` after each BUY fill |
| Position close | Simulated P&L | Real SELL order via `close_position()` |
| Shutdown | Report only | Emergency exit if open position |
| Crash | Report only | Emergency exit then report |
| Phantom position | N/A | Force-cleared if all SELL attempts fail |

---

## Log Interpretation

### Main pulse line (every ~0.25 s)

```
Fast: 66960.69 (CB 66960.69 BNC 66993.33/66993.34 smart=False) |
PolyRTDS: 66948.09 | Diff: +12.60 | Z: +0.01 |
Trend: UP s=+0.00 d=12.60 a=0.7s |
Book: UP b/a 0.500/0.510 | RSI: 74.7 [15-85] Δ=+0.00 |
Imb: 0.12 | uPnL: +0.00$ | Stale: 917ms skew: +830 (cb 87 poly 917 bn 25) |
DD: 0.00% | Gate: ON | Forecast: 66960.69
```

| Field | Meaning |
|---|---|
| `Fast` | CEX anchor price (Coinbase mid, or blended) |
| `CB / BNC` | Raw Coinbase and Binance best bid/ask |
| `PolyRTDS` | Polymarket oracle price |
| `Diff` | `Fast − PolyRTDS` = edge signal |
| `Z` | Z-score of edge |
| `Trend` | Direction, speed (pts/s), depth (max edge in trend), age |
| `Book` | Polymarket CLOB direction, best bid/ask |
| `RSI` | Current RSI and dynamic band `[lower, upper]` |
| `Δ` | RSI slope |
| `Imb` | Order book imbalance (bid vs ask volume) |
| `uPnL` | Unrealized P&L of open position |
| `Stale` | Max data age in ms; `cb/poly/bn` breakdown |
| `skew` | Cross-feed timestamp skew (CB recv − Poly recv) |
| `DD` | Current session drawdown % |
| `Gate` | `ON` = entry permitted; `OFF` = risk block active |

### Live order events

```
🟢 [LIVE] BUY placed: BUY_DOWN 19.04 sh @ 0.2080 (3.96 USD) token=... id=... immediate=True
🟢 [LIVE] On-chain balance confirmed: 18.9900 sh (attempt 2, delay 0.5s) token=...
⚠️ [LIVE] BUY adjusted for protocol fee: reported=19.0400 actual=18.9900 (fee=0.0500 sh) token=...
🟢 [LIVE] BUY confirmed: 18.9900 shares @ 0.2080 token=...
🔴 [LIVE] SELL placed: 18.9900 @ 0.2180 id=... immediate=False token=...
🔴 [LIVE] SELL confirmed: filled=18.9900 / 18.9900 @ 0.2180 token=...
⚠️ [LIVE] SELL size corrected: 6.0600 → 5.9837 (on-chain balance after fee) token=...
🔴 [LIVE] FAK SELL done: 5.9837 @ 0.4500 token=...
🚨 EMERGENCY CLOSE: SELL 6.06 @ 0.7350 token=...
🛑 Emergency close FAILED token=... — manual intervention required.
🛑 [LIVE] SELL failed entirely — force-clearing PnL state. Manual check required.
```

### Simulation events

```
🟢 [SIM BUY_UP latency] book=0.5100 exec=0.5105 | 10.00$ → 19.59 sh
🔴 [SIM SELL latency]   book=0.5000 exec=0.4995 | sold 19.59 sh | PnL -0.22$ | WR 71.4%
```

### Exit and regime diagnostics

```
📌 Exit reason=TRAILING_TP hold=4.2s pnl=+0.18 peak_pnl=+0.21
⚠️  BAD REGIME detected: WR=30.0% avgPnL=-0.15$ -> cooldown 60s
```

### Phase router diagnostics

```
Phase diag: selected=soft_flow soft_eligible=True logic_ok=True |
trend=UP speed=0.00 edge=12.60 age=2.1s stale=210ms | blockers=-
```

`blockers` lists why a profile was not selected (e.g. `volatile_speed`, `stale_above_soft_max`, `flat_trend`).

### Filter diagnostics (every 20 s)

```
FilterDiag stats: ticks=35886 entry_checks=7750 entry_no_signal=4787
entry_block_regime=0 entry_block_spread_gate=5900 entry_block_slot=2101
entry_block_rsi=3 entry_block_book=4 entry_open_ok=4
exit_reason_flip=4 exit_reason_tp=0 exit_reason_rsi=0
```

A high `entry_block_spread_gate` ratio means the market is too stale or choppy.
A high `entry_block_regime` means the win rate has been below threshold recently.

---

## Tests

Tests live in `hft_bot/tests/` and cover all critical live-mode logic.

### Run

```bash
cd hft_bot
uv run pytest tests/ -q
```

### Coverage

```bash
uv run pytest tests/ --cov=core --cov-report=term-missing
```

### Test files

| File | What is tested |
|---|---|
| `tests/test_executor.py` | `PnLTracker`: SIM BUY/SELL accounting, live mode suppression, `live_open()`, `live_close()`, `rollback_last_open()` |
| `tests/test_executor_helpers.py` | `_up_outcome_quotes_ok`, `mark_price_for_side`, `mark_bid_for_side`, `get_unrealized_pnl`, `is_good_regime` |
| `tests/test_live_engine.py` | `TrackedOrder` properties, `LiveExecutionEngine` test-mode, `_poll_order` (full/partial/sub-min/reprice), `close_position` routing, `execute()` skip and success paths |
