# Architecture

## Purpose

Trading bot for short Bitcoin Up/Down binary markets on [Polymarket](https://polymarket.com): **latency arbitrage** between CEX spot prices and the Polymarket oracle / CLOB.

## High-level diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         bot.py / main loop                          │
│  FastExchangeProvider (CB + Binance)  │  PolyOrderBook (CLOB + WS)  │
│  MarketSelector (Gamma slots)         │                              │
│              └────────► FastPriceAggregator ◄──────────────────────┘
│                              │
│                              ▼
│                       StrategyHub
│              PhaseRouterStrategy / LatencyArbitrageStrategy
│                              │
│                              ▼
│                         HFTEngine
│                              │
│              PnLTracker ◄────┴────► LiveExecutionEngine (LIVE_MODE=1)
└─────────────────────────────────────────────────────────────────────┘
```

## Module map

| Module | Role |
|--------|------|
| `bot.py`, `bot_main_loop.py` | Entry point, asyncio loop, wiring |
| `core/engine.py` | Signals, entry/exit, trailing TP/SL |
| `core/executor.py` | PnL accounting, regime filter, sim vs live |
| `core/live_engine.py` | Real CLOB orders, reprice, emergency exit |
| `core/risk_engine.py` | Drawdown, size limits, loss cooldown |
| `core/selector.py` | Active 5-minute BTC Up/Down via Gamma API |
| `core/strategy_hub.py` | Strategy registration, tick routing |
| `core/strategies/` | `PhaseRouterStrategy`, `LatencyArbitrageStrategy` |
| `data/providers.py` | Coinbase / Binance price WebSockets |
| `data/poly_clob.py` | Polymarket RTDS (oracle) + HTTP CLOB |
| `data/aggregator.py` | CEX fusion, z-score, trend, speed |
| `ml/model.py` | Optional async LSTM predictor |
| `utils/stats.py`, `utils/trade_journal.py` | Reports and `reports/trade_journal.csv` |

## Data flow

```
Coinbase WS ──┐
              ├──► FastPriceAggregator ──► edge = fast_price − poly_oracle
Binance WS ───┘         │
Polymarket RTDS WS ──────┘
```

- **Coinbase** — mid anchor; **Binance** — leading signal (`USE_SMART_FAST` may blend).
- **PolyRTDS** — Polymarket oracle, typically lags CEX by hundreds of ms to seconds.
- **Edge** — primary quantity for UP/DOWN decisions.
- **Skew gate** — `skew_ms` from `FastPriceAggregator.feed_timing` (Coinbase vs Poly receive-time delta). Entries require `HFT_ENTRY_MIN_SKEW_MS ≤ skew_ms ≤ HFT_ENTRY_MAX_SKEW_MS` when the gate is on (`HFT_ENTRY_MAX_SKEW_MS > 0`). Omit `HFT_ENTRY_MIN_SKEW_MS` for no lower bound. `HFTEngine.reload_profile_params` refreshes both bounds after a day/night profile switch.

## Configuration layering

`bot_runtime.load_runtime_env()` merges (weakest → strongest): `config/runtime.env`, `config/runtime_live.env` (`LIVE_*` and related CLOB execution defaults), day/night session profile (`runtime_day.env` / `runtime_night.env`), `config/sim_slippage.env`, then root `.env`. `apply_sim_live_unify()` may fill `LIVE_ORDER_SIZE` / `LIVE_MAX_SPREAD` from `HFT_*` when unset.

Пример полного разбора одного прогона (effective env, `Fast:` pulse, FilterDiag, отчёты): [docs/log_decode_bot_020426_083433.md](log_decode_bot_020426_083433.md).

## Repository history

Extracted from monorepo `prjBJ_arb_polymarket` with `git filter-repo --subdirectory-filter hft_bot`: root is the former `hft_bot/` tree; history is preserved for commits touching that path. Tree mapping: monorepo `bd7ac4f8ee6005bf0d7f54392958cbd5020c5565` (`hft_bot/`) ≈ this repo `3638c01089b6ecdca2377c8647e6079c40e75e4f` (root).
