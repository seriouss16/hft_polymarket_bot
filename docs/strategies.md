# Strategies

## Idea

**Latency arbitrage** between a fast CEX spot signal (Coinbase / Binance) and the lagging Polymarket oracle. Positive **edge** (`fast_price − poly_btc`) suggests an UP opportunity, negative suggests DOWN (subject to filters and thresholds).

## Active strategy (env)

`HFT_ACTIVE_STRATEGY` — e.g. `phase_router` or `latency_arbitrage`. The phase router picks a sub-profile from live conditions (trend, speed, edge magnitude, feed staleness).

## PhaseRouterStrategy (typical default)

Two profiles:

- **latency** — fast, reactive; used under strong move, large |edge|, stale feed, etc.
- **soft_flow** — more selective: needs sustained trend, minimum soft edge, staleness cap.

Switching rules use variables like `HFT_PHASE_SOFT_*`, `HFT_SOFT_BUY_EDGE` (see `config/runtime.env`).

## LatencyArbitrageStrategy

Direct latency-arb logic through shared `HFTEngine`; live and paper use the same strategy switch — there is no separate live-only profile.

## Supporting layers

- **RSI** — dynamic bands, block entries at extremes (with bypass for aggressive edge when enabled).
- **Z-score of edge** — window filter; **trend** (direction, speed, age) for soft_flow and exits.
- **LSTM** (optional) — background predictor in `ml/model.py` when enabled and a model is loaded.

## Entry thresholds (concept)

- Minimum |edge| for a candidate: `HFT_BUY_EDGE`; noise below `HFT_NOISE_EDGE` is ignored.
- Aggressive entry when `|edge|` ≥ `HFT_AGGRESSIVE_EDGE_MULT * HFT_BUY_EDGE` (see `HFTEngine`).

## Exits

Priority: book reaction TP/SL, trailing TP/SL, RSI, trend flip, reaction timeout, PnL limits — order and parameters in `core/engine.py` and `HFT_*` env vars.
