# Paper vs Live — audit follow-up (V2)

Supplements `LIVE_PAPER_GAP_ANALYSIS.md` after targeted parity fixes.

## Applied (code + `config/runtime.env`)

| Issue | Action |
|--------|--------|
| GTC SELL limit above bid | **Fixed in code:** `close_position()` now uses `best_bid + LIVE_SELL_GTC_OFFSET_FROM_BID` (default **−0.002**). Previous `best_bid + 0.002` could rest above the top bid and fill worse than paper’s bid-based exit assumption. |
| FAK SELL too aggressive | **`LIVE_FAK_SELL_WORST_BID_MULT=0.995`** in `runtime.env` (replaces 0.90). Still configurable. |
| BUY crossing spread | **`LIVE_BUY_PRICE_OFFSET=0`** — limit at ask, not ask+0.002. |
| Min 5 shares vs small budget | **Example sizing:** `HFT_DEPOSIT_USD=20`, `LIVE_ORDER_SIZE=2`, `HFT_MAX_POSITION_USD=2`, `LIVE_ACCOUNT_BALANCE=20`, `HFT_DEFAULT_TRADE_USD=2` — adjust to your real account; keep `notional/ask ≥ POLY_CLOB_MIN_SHARES`. |
| BUY reprice chasing | **`LIVE_ORDER_MAX_REPRICE=0`** — fail fast without `+0.001` chase (stricter vs paper; tune upward if fills are rare). |

## Still different by design (not “bugs”)

- **Unrealized PnL** uses mark from the book; **live** exits can differ after fees/slippage — conservative modeling would require bid/ask marks per side (future work).
- **Fees:** paper uses `HFT_SIM_FEE_RATE` in `log_trade`; live uses CLOB/chain outcomes — align by calibrating sim fee or accepting residual drift.
- **Balance / ledger lag** — mitigated by existing waits; cannot be zero in distributed systems.

## Verification

After changing `runtime.env`, confirm live account USDC matches `LIVE_ACCOUNT_BALANCE` / `HFT_DEPOSIT_USD` expectations before `LIVE_MODE=1`.
