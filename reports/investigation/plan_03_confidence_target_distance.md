# Plan 03: Confidence-Driven Strategy by Distance to Target

## Goal
Build a new decision layer where entry/exit logic depends on:
- external movement (`fast_price`, CEX-based);
- internal movement (`poly_mid`, orderbook microstructure);
- position reaction (`uPnL`, entry context);
- distance to target price (turning point confidence).

The key rule:
- closer to target -> higher confidence requirement and stricter entry;
- farther from target -> lower confidence requirement and easier entry.

This shifts control from fixed time-based holding to state-based risk management.

## Problem Statement
Current behavior still relies on several time gates (`min_hold`, `timeout`, cooldown patterns).
This can:
- keep positions too long in weak states;
- miss strong directional continuation when state remains favorable;
- ignore the important fact that risk profile changes with distance to target.

## Concept
Introduce a normalized distance factor to target and convert it into a confidence regime.

Definitions:
- `target_price`: current turning-point anchor (already represented by PolyRTDS/target logic).
- `distance_abs = abs(fast_price - target_price)`.
- `distance_norm = clamp(distance_abs / distance_ref, 0, 1)`.
  - `distance_ref` is adaptive (volatility-scaled, not constant).

Confidence interpretation:
- `distance_norm -> 0` (near target): high reversal probability uncertainty, strict entry filters.
- `distance_norm -> 1` (far from target): directional continuation potential, relaxed entry filters.

## New Module
Create module: `hft_bot/core/confidence.py`.

Main class:
- `ConfidenceModel`.

Main methods:
- `compute_distance_norm(fast_price, target_price, vol_ref) -> float`.
- `entry_strictness(distance_norm) -> float`.
- `min_entry_edge(distance_norm, base_buy_edge) -> float`.
- `min_signal_quality(distance_norm) -> float`.
- `exit_pressure(distance_norm, ext_int_divergence, pnl_state) -> float`.
- `should_allow_entry(side, context) -> tuple[bool, str]`.
- `should_force_exit(side, context) -> tuple[bool, str]`.

Inputs in `context`:
- external: edge, speed, acceleration, cex imbalance;
- internal: spread, imbalance, book move, liquidity;
- coupling: external/internal divergence;
- risk: unrealized pnl, drawdown from peak, trailing state;
- target: target price, distance norm, target drift.

## Parameters (ENV)
Add these parameters:

Core:
- `HFT_CONFIDENCE_ENABLED=1`
- `HFT_CONF_DISTANCE_REF_MODE=vol` (`vol` or `fixed`)
- `HFT_CONF_DISTANCE_REF_FIXED=20.0`
- `HFT_CONF_DISTANCE_REF_VOL_MULT=2.0`

Entry strictness by distance:
- `HFT_CONF_NEAR_TARGET_EDGE_MULT=1.6`
- `HFT_CONF_MID_TARGET_EDGE_MULT=1.2`
- `HFT_CONF_FAR_TARGET_EDGE_MULT=0.9`
- `HFT_CONF_NEAR_TARGET_SPREAD_MAX=0.010`
- `HFT_CONF_FAR_TARGET_SPREAD_MAX=0.020`
- `HFT_CONF_NEAR_TARGET_BOOK_CONFIRM_TICKS=3`
- `HFT_CONF_FAR_TARGET_BOOK_CONFIRM_TICKS=1`

Exit pressure:
- `HFT_CONF_NEAR_TARGET_EXIT_TIGHTEN=1`
- `HFT_CONF_NEAR_TARGET_TRAIL_PB=0.05`
- `HFT_CONF_FAR_TARGET_TRAIL_PB=0.10`
- `HFT_CONF_DIVERGENCE_EXIT_THRESHOLD=0.65`

Safety:
- `HFT_CONF_MIN_OBS_QUALITY=0.45`
- `HFT_CONF_FAILSAFE_TIME_SEC=0` (0 means disabled, pure state-mode).

## Integration Points in Engine
Integrate in `hft_bot/core/engine.py`:

1. At tick update:
- compute `distance_norm`;
- compute `confidence_state` and cache for current tick.

2. Before signal acceptance (`BUY_UP`/`BUY_DOWN`):
- replace fixed gates with confidence-scaled gates:
  - dynamic edge threshold;
  - dynamic spread threshold;
  - dynamic confirmation ticks;
  - dynamic reentry cooldown (already partially adaptive).

3. During open position management:
- prioritize state exits over time exits:
  - divergence between external and internal move;
  - confidence decay while in position;
  - target-proximity tightening for trailing and protective logic.

4. Keep hard safeties:
- stale feed emergency exit;
- upper-bound 0.99 close;
- catastrophic loss stop.

## Decision Logic (High Level)
Entry score:
- `entry_score = w1*edge_quality + w2*book_quality + w3*trend_quality + w4*target_distance_quality`.
- accept only when `entry_score >= min_signal_quality(distance_norm)`.

Exit score:
- `exit_score = a1*divergence + a2*confidence_decay + a3*adverse_book_pullback + a4*risk_pressure`.
- exit when `exit_score` exceeds threshold for current distance regime.

Near target behavior:
- stricter entry;
- tighter trailing;
- faster de-risk on divergence.

Far from target behavior:
- easier entry;
- allow trend continuation;
- wider trailing.

## Rollout Phases
Phase 1 (Shadow mode):
- compute confidence metrics and log decisions;
- do not alter trading actions.

Phase 2 (Soft enable):
- confidence gates affect only new entries;
- exits remain current logic + logging.

Phase 3 (Full state mode):
- confidence controls both entries and exits;
- time exits reduced to fail-safe only.

Phase 4 (Parameter tuning):
- optimize thresholds by market regime and slot period.

## Logging and Observability
Add structured logs:
- `conf.distance_norm`
- `conf.entry_strictness`
- `conf.entry_score`
- `conf.exit_score`
- `conf.regime = near|mid|far`
- reason tags for blocked entries and state exits.

Required reason tags:
- `CONF_BLOCK_NEAR_TARGET_LOW_QUALITY`
- `CONF_BLOCK_SPREAD`
- `CONF_EXIT_DIVERGENCE`
- `CONF_EXIT_CONFIDENCE_DECAY`
- `CONF_EXIT_NEAR_TARGET_TIGHT_TRAIL`

## Validation Metrics
Primary:
- profit factor;
- max drawdown;
- median adverse excursion per trade.

Secondary:
- win rate near target;
- missed move ratio (blocked but later profitable);
- average hold quality (state score over hold lifecycle).

Acceptance criteria:
- lower drawdown without reducing net pnl quality;
- fewer low-quality entries near target;
- better capture of strong far-target trends.

## Risks
- Overfitting thresholds to one market regime.
- Under-trading if near-target strictness is too high.
- Increased complexity and parameter interactions.

Mitigations:
- staged rollout;
- shadow logs first;
- bounded parameter ranges and defaults.

## Implementation Checklist
- [ ] Add `confidence.py` module with pure calculations.
- [ ] Add env parsing in engine with sane defaults.
- [ ] Add per-tick confidence context assembly.
- [ ] Wire confidence into entry gating.
- [ ] Wire confidence into exit pressure logic.
- [ ] Add reason-tag logs and debug snapshot fields.
- [ ] Add tests for near/mid/far regimes.
- [ ] Run shadow-mode replay on recent logs.
- [ ] Enable soft mode on paper run.
- [ ] Enable full mode after metric acceptance.

## Notes
This plan intentionally moves strategy control from fixed time constraints to market-state constraints, while preserving hard safety exits.
