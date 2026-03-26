# HFT Bot Production Setup

This guide describes how to run `hft_bot` in production mode with Polymarket CLOB order management.

## Runtime Mode Branching

- `uv run bot.py --test` starts simulation mode.
- `uv run bot.py --prod` starts production mode.
- If no CLI flag is provided, `LIVE_MODE` from env is used.

## Required Credentials in `hft_bot/.env`

Set one private key and one wallet/funder value.

```env
PRIVATE_KEY=0x...
FUNDER=0x...
```

Alternative names are supported.

- Private key: `PRIVATE_KEY` or `CLOB_PRIVATE_KEY`.
- Wallet/funder: `FUNDER`, `POLY_FUNDER_ADDRESS`, `WALLET`, `WALLET_ADDRESS`, or `CLOB_FUNDER`.
- API key trio (optional, for explicit CLOB auth): `POLIMARKET_API_KEY`, `POLIMARKET_API_SECRET`, `POLIMARKET_API_PASSPHRASE`.
- API key trio aliases from `prjBD_polybot`: `POLY_API_KEY`, `POLY_API_SECRET`, `POLY_API_PASSPHRASE`.
- Signature mode: `HFT_CLOB_SIGNATURE_TYPE` or `POLY_SIGNATURE_TYPE` (`0` for EOA/MetaMask, `1` for Magic/email wallets).

## Position Lifecycle in Production

- New live signal opens a position through CLOB limit order.
- Opposite signal closes current position first, then opens the new side.
- Strategy close event triggers explicit CLOB close order.
- Spread and max entry ask gates are enforced before every entry and close.

## Key Smoke Test

Run a non-trading authentication and deposit/allowance check.

`uv run hft_bot/tools/test_polymarket_keys.py`

The script exits with code `0` on success and `1` on failure.

## Safety Notes

- Keep `LIVE_MODE=0` for dry runs.
- Tune `LIVE_ORDER_SIZE`, `LIVE_MAX_SPREAD`, and `HFT_MAX_ENTRY_ASK`.
- Validate credentials on a small size before running full flow.

