# Simulation (paper) mode

## Default behavior

While **`LIVE_MODE=0`** (or unset), the bot **does not** place real CLOB orders: entries and exits are logged as simulation (`[SIM …]`), PnL is tracked in memory like paper trading.

## Run

```bash
uv sync --all-groups
uv run python bot.py
# or
uv run hft-bot
```

Mode is controlled by **`LIVE_MODE`** only: `0` or unset → simulation (see `bot_main_loop.main()`). There are no `--prod` / `--test` CLI flags on the entrypoint.

## Logs and journal

- Logs: `reports/logs/bot_DDMMYY_HHMMSS.log` (rotation configured in the logger).
- Closed trades: appended to `reports/trade_journal.csv`.

## Tests

```bash
uv run pytest tests/
```

Critical paths: `tests/test_executor.py`, `tests/test_live_engine.py`, and others.

## Parity with live

Strategies and `StrategyHub.process_tick` receive the same feeds and metadata as in live; the difference is no real CLOB placement and fill accounting only in simulation.
