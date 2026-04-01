# Live trading

## Enabling

- **`LIVE_MODE=1`** in the environment.
- Valid Polygon / Polymarket credentials (see root `README.md`).

Without explicit live mode, the bot runs in simulation. There are no separate `--prod` / `--test` flags — only **`LIVE_MODE`** and loading `.env` / `config/runtime.env`.

## Live configuration (summary)

Create **`.env`** at the project root (do not commit; it is `.gitignore`d):

```env
PRIVATE_KEY=0x...
POLY_FUNDER_ADDRESS=0x...
POLY_SIGNATURE_TYPE=2

LIVE_MODE=1
HFT_DEPOSIT_USD=<balance>
LIVE_ACCOUNT_BALANCE=<same USDC balance>
HFT_DEFAULT_TRADE_USD=<trade size>
LIVE_ORDER_SIZE=<order size>
LIVE_MAX_SESSION_LOSS=-<session loss cap>
```

Recommendation: avoid manually setting Polymarket API keys if the bot derives them from the wallet key — see `py_clob_client` / `create_or_derive_api_creds` in the codebase.

## Order lifecycle (essentials)

- **BUY**: limit at best ask, status polling, reprice up to `LIVE_ORDER_MAX_REPRICE`, then emergency handling.
- After BUY — on-chain CTF balance checks with backoff (`LIVE_BALANCE_CONFIRM_DELAYS_SEC`, etc.).
- **SELL**: GTC at bid with `LIVE_SELL_GTC_OFFSET_FROM_BID`; on staleness — FAK / emergency.
- **Heartbeat** to CLOB at interval ≤ 15 s (platform requirement).

## Risk layers

1. **LiveRiskManager** — stop on session realized PnL (`LIVE_MAX_SESSION_LOSS`).
2. **RiskEngine** — drawdown from peak (`MAX_DRAWDOWN_PCT`), loss cooldown, notional cap (`MAX_POSITION_PCT`).
3. **Regime filter** — rolling win rate of recent trades (`HFT_BAD_REGIME_WINRATE`, `HFT_REGIME_COOLDOWN_SEC`).
4. **Cooldown after failed live BUY** — `LIVE_SKIP_COOLDOWN_SEC` (exceptions for stale/emergency — see env).

## First-live checklist

- Key and funder on Polygon (chain id 137); `POLY_SIGNATURE_TYPE` matches wallet type.
- `HFT_DEPOSIT_USD` = `LIVE_ACCOUNT_BALANCE` = actual balance.
- Loss and position limits aligned with deposit.
- At least one simulation run and review of `reports/trade_journal.csv`.

## Simulation vs live

| Aspect | Simulation | Live |
|--------|------------|------|
| Orders | Log only | Real CLOB limits |
| PnL | `log_trade` / sim | `live_open` / `live_close` after confirmation |
| Balance | N/A | CTF check after BUY |
| Shutdown | Report only | Emergency exit if a position is open |

Full env reference: `config/runtime.env` and engine code comments.
