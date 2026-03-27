"""Shared pytest fixtures and configuration for hft_bot tests."""

import os
import sys

import pytest

# Allow imports from hft_bot root without package installation.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Minimal env so modules that read os.getenv() at import time get safe defaults.
_ENV_DEFAULTS = {
    "HFT_DEPOSIT_USD": "100.0",
    "HFT_DEFAULT_TRADE_USD": "10.0",
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
    "LIVE_ORDER_SIZE": "10.0",
    "LIVE_MAX_SPREAD": "0.10",
}


@pytest.fixture(autouse=True)
def set_env(monkeypatch):
    """Inject safe defaults for all env variables used by the modules."""
    for key, val in _ENV_DEFAULTS.items():
        monkeypatch.setenv(key, val)
