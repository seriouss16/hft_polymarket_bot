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
| Max daily loss (live) | $15 |
| Max drawdown | 15 % |

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
│  (sim P&L,     (order lifecycle,                                    │
│  drawdown,      reprice, emergency                                  │
│  regime)        exit, fill tracking)                                │
└─────────────────────────────────────────────────────────────────────┘
```

### Module map

| Module | Role |
|---|---|
| `bot.py` | Entry point; asyncio event loop, wires all components |
| `core/engine.py` | Signal evaluation, entry/exit conditions, trailing TP/SL |
| `core/executor.py` | Simulated P&L tracker, regime filter, inventory |
| `core/live_engine.py` | Live CLOB execution, order tracking, reprice, emergency exit |
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

Generates live entry signals directly from the edge gap. Used as the live signal source (`HFT_LIVE_SIGNAL_STRATEGY=latency_arbitrage`) even when PhaseRouter drives the simulation engine.

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

- No entry in first `HFT_NO_ENTRY_FIRST_SEC` (5 s) or last `HFT_NO_ENTRY_LAST_SEC` (45 s) of a slot.
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

When `LIVE_MODE=1`, every simulated entry/exit decision also places a real GTC limit order on the Polymarket CLOB. The `LiveExecutionEngine` manages the full lifecycle:

```
execute(signal, token_id)          close_position(token_id, size)
        │                                    │
        ▼                                    ▼
_place_limit_raw()              _place_limit_raw(SELL)
        │                                    │
        ▼                                    ▼
TrackedOrder(PENDING)           TrackedOrder(PENDING)
        │                                    │
        └──────────────┬─────────────────────┘
                       │
                       ▼
              _poll_order() [background task]
                       │
               poll every 0.4 s
                       │
              ┌────────┼──────────────┐
              │        │              │
           FILLED   PARTIAL        STALE (> 3.0 s)
              │        │              │
             done   continue    reprice ≤ 2 times
                    polling          │
                               cancel + new order
                               at current best price
                                     │
                              reprice exhausted?
                                     │  YES
                                     ▼
                           _emergency_exit_order()
                           cross the spread (+0.005)
```

### Emergency exit triggers

| Trigger | Action |
|---|---|
| Order stale after max reprice | Cancel + aggressive limit crossing the book |
| Engine CLOSE decision (LIVE_MODE) | `close_position()` → tracked SELL order |
| Shutdown with open position | `emergency_exit()` → cancel all pending + aggressive SELL |
| Crash / exception in main loop | Same via `finally` block |

### Order status states

| State | Meaning |
|---|---|
| `PENDING` | Order placed, awaiting fill |
| `FILLED` | Fully filled — done |
| `PARTIAL` | Partially filled — continues polling |
| `STALE` | Exceeded `LIVE_ORDER_STALE_SEC` without fill |
| `CANCELLED` | Manually cancelled (by reprice or emergency) |
| `FAILED` | Placement or reprice failed — needs attention |

---

## Risk Controls

Three independent layers:

### 1. LiveRiskManager (daily loss)

```python
if live_pnl < LIVE_MAX_DAILY_LOSS:   # -$15
    block all new entries
```

### 2. RiskEngine (session drawdown)

```python
if drawdown_from_peak > MAX_DRAWDOWN_PCT:   # 15%
    block all entries
if loss trade closed:
    cooldown LOSS_COOLDOWN_SEC             # 30 s
max_notional = equity × MAX_POSITION_PCT  # 10%
```

### 3. Regime filter (rolling win rate)

```python
if recent_winrate < HFT_BAD_REGIME_WINRATE (0.35):
    block entries for HFT_REGIME_COOLDOWN_SEC (60 s)
```

---

## Configuration Reference

### Core sizing

| Key | Default | Description |
|---|---|---|
| `HFT_DEPOSIT_USD` | 100 | Starting equity |
| `HFT_DEFAULT_TRADE_USD` | 10 | Notional per trade |
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

### Live order lifecycle

| Key | Default | Description |
|---|---|---|
| `LIVE_ORDER_FILL_POLL_SEC` | 0.4 | Seconds between fill status polls |
| `LIVE_ORDER_STALE_SEC` | 3.0 | Seconds before unfilled order is considered stale |
| `LIVE_ORDER_MAX_REPRICE` | 2 | Max reprice attempts before emergency exit |
| `LIVE_ORDER_SIZE` | 10 | Shares per live order |
| `LIVE_MAX_SPREAD` | 0.05 | Max CLOB spread to allow execution |

### Risk limits

| Key | Default | Description |
|---|---|---|
| `LIVE_MAX_DAILY_LOSS` | -15.0 | Stop trading when daily PnL < this |
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
# Install dependencies
uv sync
# or
pip install -r requirements.txt
```

Required packages: `websockets`, `requests`, `numpy`, `uvloop`, `py_clob_client` (for live mode).

### Simulation mode (safe default)

```bash
cd hft_bot
uv run bot.py
```

Logs appear in `reports/logs/bot_DDMMYY_HHMMSS.log` (last 20 kept).
Trade records append to `reports/trade_journal.csv`.

### Force a single strategy profile

```bash
# Latency only
HFT_PHASE_FORCE_PROFILE=latency uv run bot.py

# Soft-flow only
HFT_PHASE_FORCE_PROFILE=soft_flow uv run bot.py
```

### Enable LSTM price predictor

```bash
HFT_ENABLE_LSTM=1 uv run bot.py
```

Requires `tensorflow` or `onnxruntime` with a trained model file.

---

## Switching to Live Mode

### 1. Credentials

Create `hft_bot/.env` (never commit this file):

```env
PRIVATE_KEY=0x<your_polygon_private_key>
POLY_FUNDER_ADDRESS=0x<your_funder_wallet>
POLIMARKET_API_KEY=<api_key>
POLIMARKET_API_SECRET=<api_secret>
POLIMARKET_API_PASSPHRASE=<passphrase>
POLY_SIGNATURE_TYPE=2
```

`.env` overrides `runtime.env` — any key set in `.env` takes precedence.

### 2. Enable live mode

```bash
# Either in .env:
LIVE_MODE=1

# Or as environment variable:
LIVE_MODE=1 uv run bot.py
```

### 3. Checklist before first live run

- [ ] `PRIVATE_KEY` and `POLY_FUNDER_ADDRESS` set and confirmed correct network (Polygon mainnet, chain_id=137)
- [ ] `POLY_SIGNATURE_TYPE=2` matches wallet type
- [ ] `LIVE_MAX_DAILY_LOSS` set to an amount you can afford to lose in a session
- [ ] `MAX_DRAWDOWN_PCT` ≤ 0.20 (20%)
- [ ] `LIVE_ORDER_SIZE` matches available USDC balance on Polymarket
- [ ] Run at least one simulation session and review `reports/trade_journal.csv`
- [ ] `py_clob_client` installed: `pip install py_clob_client`

### 4. Live vs simulation differences

| Aspect | Simulation | Live |
|---|---|---|
| Order placement | Log only (`[SIM LIMIT]`) | Real GTC limit on Polymarket CLOB |
| Fill tracking | Not tracked | Polled every `LIVE_ORDER_FILL_POLL_SEC` |
| Position close | Simulated P&L | Real SELL order via `close_position()` |
| Shutdown | Report only | Emergency exit if open position |
| Crash | Report only | Emergency exit then report |

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

### Entry and exit events

```
🟢 [SIM BUY_UP latency] book=0.5100 exec=0.5105 | 10.00$ → 19.59 sh
🔴 [SIM SELL latency]   book=0.5000 exec=0.4995 | sold 19.59 sh | PnL -0.22$ | WR 71.4%
📌 Exit reason=TRAILING_TP hold=4.2s pnl=+0.18 peak_pnl=+0.21
⚠️  BAD REGIME detected: WR=30.0% avgPnL=-0.15$ -> cooldown 60s
🚨 EMERGENCY CLOSE: SELL 19.59 @ 0.4990 token=...
```

### Phase router diagnostics (every 45 s)

```
Phase diag: selected=soft_flow soft_eligible=True logic_ok=True |
trend=UP speed=0.00 edge=12.60 age=2.1s stale=210ms | blockers=-
```

`blockers` lists why latency or soft_flow was not selected (e.g. `volatile_speed`, `stale_above_soft_max`, `flat_trend`).

### Filter diagnostics (every 20 s)

```
FilterDiag stats: ticks=35886 entry_checks=7750 entry_no_signal=4787
entry_block_regime=0 entry_block_spread_gate=5900 entry_block_slot=2101
entry_block_rsi=3 entry_block_book=4 entry_open_ok=4
exit_reason_flip=4 exit_reason_tp=0 exit_reason_rsi=0
```

A high `entry_block_spread_gate` ratio means the market is too stale or choppy. A high `entry_block_regime` means the win rate has been below threshold recently.
