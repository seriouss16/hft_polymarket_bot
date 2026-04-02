"""Shared pytest fixtures and configuration for hft_bot tests."""

import os
import sys
from pathlib import Path

import pytest

# Enable asyncio mode for all async tests in this suite.
pytest_plugins = ("pytest_asyncio",)

# Allow imports from hft_bot root without package installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.env_merge import merge_env_file
from utils.env_unify import apply_sim_live_unify

# Minimal env so modules that read os.getenv() at import time get safe defaults.
_ENV_DEFAULTS = {
    "HFT_DEPOSIT_USD": "100.0",
    "HFT_DEFAULT_TRADE_USD": "10.0",
    "HFT_MAX_POSITION_USD": "100",
    "LIVE_ACCOUNT_BALANCE": "100",
    "HFT_SIM_FEE_RATE": "0.001",
    "HFT_RECENT_TRADES_FOR_REGIME": "12",
    "HFT_GOOD_REGIME_WINRATE": "0.49",
    "HFT_BAD_REGIME_WINRATE": "0.48",
    "HFT_REGIME_COOLDOWN_SEC": "150",
    "POLY_CLOB_MIN_SHARES": "5",
    "LIVE_ORDER_FILL_POLL_SEC": "0.01",
    "LIVE_ORDER_STALE_SEC": "0.05",
    "LIVE_ORDER_MAX_REPRICE": "2",
    "HFT_MAX_ENTRY_ASK": "0.99",
    "HFT_MIN_ENTRY_ASK": "0.08",
    "HFT_MAX_ENTRY_SPREAD": "0.10",
    # LIVE_ORDER_SIZE / LIVE_MAX_SPREAD: omitted on purpose — filled from HFT_* by
    # bot_config_log._unify_sim_live_trading_params() when tests call validate_required_config.
    "LIVE_INVENTORY_DUST_SHARES": "0.05",
    "LIVE_SELL_GTC_OFFSET_FROM_BID": "-0.002",
    "LIVE_SELL_PLACE_ATTEMPTS": "1",
    "LIVE_SELL_FAK_ATTEMPTS": "1",
    "LIVE_SELL_PLACE_RETRY_SLEEP_SEC": "0.01",
    "LIVE_SELL_FAK_RETRY_SLEEP_SEC": "0.01",
    "LIVE_SELL_BALANCE_WAIT_DELAYS_SEC": "0",
    "LIVE_CLOSE_CHAIN_PROBE_DELAYS_SEC": "0",
    "LIVE_CLOSE_WAIT_PENDING_SEC": "0.01",
    "LIVE_CHAIN_EXIT_DUST_SHARES": "0.05",
    "LIVE_SELL_CHAIN_DUST_SHARES": "0.05",
    "LIVE_POST_SELL_CHAIN_DELAY_SEC": "0",
    "LIVE_BUY_PRICE_OFFSET": "0.001",
    "LIVE_BUY_REPRICE_TICK": "0.001",
    "LIVE_SELL_REPRICE_TICK": "0.001",
    "LIVE_FAK_SELL_WORST_BID_MULT": "0.99",
    "LIVE_EMERGENCY_BUY_BUMP": "0.002",
    "LIVE_EMERGENCY_CROSS_BUMP": "0.002",
    "LIVE_REPRICE_POST_CANCEL_FILL_POLLS": "1",
    "LIVE_REPRICE_POST_CANCEL_POLL_SEC": "0.01",
    "LIVE_REPRICE_POST_CANCEL_SLEEP_SEC": "0.01",
    "LIVE_BUY_COLLATERAL_SAFETY": "0.99",
    "LIVE_USDC_DEBIT_VERIFY_ABS_USD": "0.12",
    "LIVE_USDC_DEBIT_VERIFY_REL": "0.025",
    "LIVE_BALANCE_MIN_FRAC": "0.1",
    "LIVE_BALANCE_CONFIRM_DELAYS_SEC": "0",
    "LIVE_TRUST_CLOB_WITHOUT_CHAIN_BALANCE": "1",
    "LIVE_ORDER_WS_TIMEOUT_SEC": "0.1",
    # Tests: no live CLOB market WebSocket (avoid network).
    "CLOB_MARKET_WS_ENABLED": "0",
    "CLOB_USER_WS_ENABLED": "0",
    # CLOB HTTP timeout for live_engine module import
    "LIVE_CLOB_BOOK_HTTP_TIMEOUT": "1.5",
    # Live engine settings
    "HFT_LIVE_SKIP_STATS_LOG_SEC": "60",
    # Deterministic SIM entry price in executor tests (shell or runtime may set analyzer suggestion).
    "HFT_SIM_SLIPPAGE_EXTRA_FRACTION": "0",
}


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    """Load ``config/runtime.env`` then apply minimal test overrides."""
    root = Path(__file__).resolve().parent.parent
    merge_env_file(root / "config" / "sim_slippage.env", overwrite=False)
    merge_env_file(root / "config" / "runtime.env", overwrite=True)
    for key, val in _ENV_DEFAULTS.items():
        monkeypatch.setenv(key, val)
    apply_sim_live_unify()
