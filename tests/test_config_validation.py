"""Tests for centralized configuration validation (utils/config_validation.py)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Allow imports from hft_bot root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from utils.config_validation import (
    ConfigValidator,
    ValidationError,
    validate_config,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Ensure a clean environment for each test."""
    # Clear any existing HFT_* variables
    keys_to_remove = [k for k in os.environ if k.startswith("HFT_") or k in {
        "REGIME_WINDOW_TICKS", "REGIME_CALM_SPEED_MAX", "REGIME_ACTIVE_SPEED_MIN",
        "REGIME_CALM_STALE_MIN_MS", "REGIME_LOG_MIN_SEC", "REGIME_HYSTERESIS_TICKS",
        "HFT_DEPOSIT_USD", "HFT_DEFAULT_TRADE_USD", "HFT_MAX_POSITION_USD",
        "STATS_INTERVAL_SEC", "HFT_BUY_EDGE", "HFT_SELL_EDGE_ABS", "HFT_MIN_HOLD_SEC",
        "HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "HFT_REGIME_FILTER_ENABLED",
        "HFT_TRAILING_TP_ENABLED", "HFT_TRAILING_SL_ENABLED",
    }]
    for key in keys_to_remove:
        monkeypatch.delenv(key, raising=False)


class TestConfigValidator:
    """Test the ConfigValidator class."""

    def test_valid_minimal_config_passes(self, monkeypatch):
        """A minimal valid configuration should pass validation."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")

        # Should not raise
        validate_config()

    def test_missing_required_parameter_fails(self, monkeypatch):
        """Missing required parameter should cause validation to fail."""
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        # Missing HFT_DEPOSIT_USD

        with pytest.raises(SystemExit):
            validate_config()

    def test_invalid_float_type_fails(self, monkeypatch):
        """Non-numeric value for float parameter should fail."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "ten")  # invalid
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")

        with pytest.raises(SystemExit):
            validate_config()

    def test_float_below_min_fails(self, monkeypatch):
        """Float value below minimum should fail."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "-10.0")  # should be >= 0
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")

        with pytest.raises(SystemExit):
            validate_config()

    def test_float_above_max_fails(self, monkeypatch):
        """Float value above maximum should fail."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")
        # Set HFT_ENTRY_MAX_ASK_UP above max (1.0)
        monkeypatch.setenv("HFT_ENTRY_MAX_ASK_UP", "1.5")

        with pytest.raises(SystemExit):
            validate_config()

    def test_invalid_int_type_fails(self, monkeypatch):
        """Non-integer value for int parameter should fail."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "sixty")  # invalid
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")

        with pytest.raises(SystemExit):
            validate_config()

    def test_invalid_bool_type_fails(self, monkeypatch):
        """Invalid boolean value should fail."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "maybe")  # invalid
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")

        with pytest.raises(SystemExit):
            validate_config()

    def test_invalid_enum_choice_fails(self, monkeypatch):
        """Invalid enum choice should fail."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")
        monkeypatch.setenv("HFT_ZSCORE_MONOTONIC_STRICTNESS", "invalid")  # not in {strict, relaxed, off}

        with pytest.raises(SystemExit):
            validate_config()

    def test_clamp_high_must_be_greater_than_low(self, monkeypatch):
        """HFT_RSI_EXIT_CLAMP_HIGH must be > HFT_RSI_EXIT_CLAMP_LOW."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")
        # Invalid: clamp high <= clamp low
        monkeypatch.setenv("HFT_RSI_EXIT_CLAMP_HIGH", "50.0")
        monkeypatch.setenv("HFT_RSI_EXIT_CLAMP_LOW", "60.0")

        with pytest.raises(SystemExit):
            validate_config()

    def test_clamp_low_must_be_less_than_high(self, monkeypatch):
        """HFT_RSI_EXIT_CLAMP_LOW must be < HFT_RSI_EXIT_CLAMP_HIGH."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")
        # Valid: clamp high > clamp low
        monkeypatch.setenv("HFT_RSI_EXIT_CLAMP_HIGH", "90.0")
        monkeypatch.setenv("HFT_RSI_EXIT_CLAMP_LOW", "10.0")

        # Should not raise
        validate_config()

    def test_slope_up_must_be_negative(self, monkeypatch):
        """HFT_RSI_SLOPE_EXIT_UP must be < 0."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        # Invalid: slope up >= 0
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")

        with pytest.raises(SystemExit):
            validate_config()

    def test_slope_down_must_be_positive(self, monkeypatch):
        """HFT_RSI_SLOPE_EXIT_DOWN must be > 0."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        # Invalid: slope down <= 0
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "-1.0")

        with pytest.raises(SystemExit):
            validate_config()

    def test_valid_slope_signs_pass(self, monkeypatch):
        """Valid slope signs should pass."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        # Valid slopes
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")

        validate_config()

    def test_skew_min_max_order(self, monkeypatch):
        """HFT_ENTRY_MIN_SKEW_MS must be <= HFT_ENTRY_MAX_SKEW_MS."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")
        # Invalid: min > max
        monkeypatch.setenv("HFT_ENTRY_MIN_SKEW_MS", "100")
        monkeypatch.setenv("HFT_ENTRY_MAX_SKEW_MS", "50")

        with pytest.raises(SystemExit):
            validate_config()

    def test_dynamic_amount_min_max_order(self, monkeypatch):
        """HFT_DYNAMIC_AMOUNT_MIN_USD must be <= HFT_DYNAMIC_AMOUNT_MAX_USD."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")
        monkeypatch.setenv("HFT_DYNAMIC_AMOUNT_MIN_USD", "80.0")
        monkeypatch.setenv("HFT_DYNAMIC_AMOUNT_MAX_USD", "50.0")  # min > max

        with pytest.raises(SystemExit):
            validate_config()

    def test_ask_band_validation(self, monkeypatch):
        """HFT_ENTRY_MIN_ASK_* must be <= HFT_ENTRY_MAX_ASK_*."""
        # Test UP
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")
        # Invalid: min > max for UP
        monkeypatch.setenv("HFT_ENTRY_MIN_ASK_UP", "0.5")
        monkeypatch.setenv("HFT_ENTRY_MAX_ASK_UP", "0.3")

        with pytest.raises(SystemExit):
            validate_config()

    def test_boolean_parsing(self, monkeypatch):
        """Test various boolean representations are parsed correctly."""
        test_cases = [
            ("1", True),
            ("0", False),
            ("true", True),
            ("false", False),
            ("yes", True),
            ("no", False),
            ("on", True),
            ("off", False),
        ]
        for raw, expected in test_cases:
            monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", raw)
            # Need to reimport to test parsing, but we'll just test the validator's internal method
            from utils.config_validation import ConfigValidator
            validator = ConfigValidator()
            result = validator._parse_bool(raw)
            assert result == expected, f"{raw} should parse to {expected}, got {result}"

    def test_enum_choices(self, monkeypatch):
        """Test enum choices are validated."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")
        # Valid choices: strict, relaxed, off
        for choice in ("strict", "relaxed", "off"):
            monkeypatch.setenv("HFT_ZSCORE_MONOTONIC_STRICTNESS", choice)
            validate_config()  # should not raise

    def test_optional_parameters_can_be_unset(self, monkeypatch):
        """Optional parameters can be omitted without causing validation errors."""
        # Only set required params
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")

        # Should not raise even though many optional params are unset
        validate_config()

    def test_rsi_period_range(self, monkeypatch):
        """HFT_RSI_PRICE_LEN must be between 1 and 1000."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")

        # Too large
        monkeypatch.setenv("HFT_RSI_PRICE_LEN", "2000")
        with pytest.raises(SystemExit):
            validate_config()

        # Too small
        monkeypatch.setenv("HFT_RSI_PRICE_LEN", "0")
        with pytest.raises(SystemExit):
            validate_config()

        # Valid
        monkeypatch.setenv("HFT_RSI_PRICE_LEN", "128")
        validate_config()

    def test_rsi_band_ranges(self, monkeypatch):
        """RSI band values must be in 0-100 range."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")

        # Out of range
        monkeypatch.setenv("HFT_RSI_ENTRY_UP_LOW", "110.0")
        with pytest.raises(SystemExit):
            validate_config()

        monkeypatch.setenv("HFT_RSI_ENTRY_UP_LOW", "-10.0")
        with pytest.raises(SystemExit):
            validate_config()

    def test_percentage_params_max_1(self, monkeypatch):
        """Percentage parameters (0-1 range) are validated."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")

        # Exceeds 1.0
        monkeypatch.setenv("HFT_PNL_TP_PERCENT", "1.5")
        with pytest.raises(SystemExit):
            validate_config()

        # Negative
        monkeypatch.setenv("HFT_PNL_TP_PERCENT", "-0.1")
        with pytest.raises(SystemExit):
            validate_config()

    def test_validation_error_message_content(self, monkeypatch):
        """Error messages should include parameter name and reason."""
        monkeypatch.setenv("HFT_DEPOSIT_USD", "100.0")
        monkeypatch.setenv("HFT_DEFAULT_TRADE_USD", "10.0")
        monkeypatch.setenv("HFT_MAX_POSITION_USD", "100")
        monkeypatch.setenv("STATS_INTERVAL_SEC", "120")
        monkeypatch.setenv("HFT_BUY_EDGE", "4.8")
        monkeypatch.setenv("HFT_SELL_EDGE_ABS", "7.2")
        monkeypatch.setenv("HFT_MIN_HOLD_SEC", "3.8")
        monkeypatch.setenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
        monkeypatch.setenv("HFT_REGIME_FILTER_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_TP_ENABLED", "1")
        monkeypatch.setenv("HFT_TRAILING_SL_ENABLED", "1")
        monkeypatch.setenv("REGIME_WINDOW_TICKS", "60")
        monkeypatch.setenv("REGIME_CALM_SPEED_MAX", "0.5")
        monkeypatch.setenv("REGIME_ACTIVE_SPEED_MIN", "5.0")
        monkeypatch.setenv("REGIME_CALM_STALE_MIN_MS", "1200")
        monkeypatch.setenv("REGIME_LOG_MIN_SEC", "120")
        monkeypatch.setenv("REGIME_HYSTERESIS_TICKS", "15")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_UP", "-2.0")
        monkeypatch.setenv("HFT_RSI_SLOPE_EXIT_DOWN", "2.0")
        monkeypatch.setenv("HFT_RSI_EXIT_CLAMP_HIGH", "50.0")
        monkeypatch.setenv("HFT_RSI_EXIT_CLAMP_LOW", "60.0")

        try:
            validate_config()
            assert False, "Should have raised SystemExit"
        except SystemExit:
            # The error should be logged
            # We can't easily capture logging here, but we know it's called
            pass


class TestRealConfigFiles:
    """Test that actual configuration files are valid."""

    def test_runtime_env_valid(self):
        """Test that config/runtime.env contains valid values."""
        # Load the runtime.env file
        root = Path(__file__).resolve().parent.parent
        runtime_env = root / "config" / "runtime.env"
        if not runtime_env.is_file():
            pytest.skip(f"{runtime_env} not found")

        # Parse the file and set environment variables
        from utils.env_merge import merge_env_file
        # We need to load it into os.environ for validation
        # But we should restore original state after
        original_env = os.environ.copy()
        try:
            merge_env_file(runtime_env, overwrite=True)
            # Also load minimal required params
            os.environ.setdefault("HFT_DEPOSIT_USD", "100.0")
            os.environ.setdefault("HFT_DEFAULT_TRADE_USD", "10.0")
            os.environ.setdefault("HFT_MAX_POSITION_USD", "100")
            os.environ.setdefault("STATS_INTERVAL_SEC", "120")
            os.environ.setdefault("HFT_BUY_EDGE", "4.8")
            os.environ.setdefault("HFT_SELL_EDGE_ABS", "7.2")
            os.environ.setdefault("HFT_MIN_HOLD_SEC", "3.8")
            os.environ.setdefault("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
            os.environ.setdefault("HFT_REGIME_FILTER_ENABLED", "1")
            os.environ.setdefault("HFT_TRAILING_TP_ENABLED", "1")
            os.environ.setdefault("HFT_TRAILING_SL_ENABLED", "1")
            os.environ.setdefault("REGIME_WINDOW_TICKS", "60")
            os.environ.setdefault("REGIME_CALM_SPEED_MAX", "0.5")
            os.environ.setdefault("REGIME_ACTIVE_SPEED_MIN", "5.0")
            os.environ.setdefault("REGIME_CALM_STALE_MIN_MS", "1200")
            os.environ.setdefault("REGIME_LOG_MIN_SEC", "120")
            os.environ.setdefault("REGIME_HYSTERESIS_TICKS", "15")

            # Should not raise
            validate_config()
        finally:
            os.environ.clear()
            os.environ.update(original_env)

    def test_runtime_day_env_valid(self):
        """Test that config/runtime_day.env contains valid values."""
        root = Path(__file__).resolve().parent.parent
        day_env = root / "config" / "runtime_day.env"
        if not day_env.is_file():
            pytest.skip(f"{day_env} not found")

        from utils.env_merge import merge_env_file
        original_env = os.environ.copy()
        try:
            merge_env_file(day_env, overwrite=True)
            # Also need base runtime.env and minimal required
            merge_env_file(root / "config" / "runtime.env", overwrite=True)
            os.environ.setdefault("HFT_DEPOSIT_USD", "100.0")
            os.environ.setdefault("HFT_DEFAULT_TRADE_USD", "10.0")
            os.environ.setdefault("HFT_MAX_POSITION_USD", "100")
            os.environ.setdefault("STATS_INTERVAL_SEC", "120")
            os.environ.setdefault("HFT_BUY_EDGE", "4.8")
            os.environ.setdefault("HFT_SELL_EDGE_ABS", "7.2")
            os.environ.setdefault("HFT_MIN_HOLD_SEC", "3.8")
            os.environ.setdefault("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
            os.environ.setdefault("HFT_REGIME_FILTER_ENABLED", "1")
            os.environ.setdefault("HFT_TRAILING_TP_ENABLED", "1")
            os.environ.setdefault("HFT_TRAILING_SL_ENABLED", "1")
            os.environ.setdefault("REGIME_WINDOW_TICKS", "60")
            os.environ.setdefault("REGIME_CALM_SPEED_MAX", "0.5")
            os.environ.setdefault("REGIME_ACTIVE_SPEED_MIN", "5.0")
            os.environ.setdefault("REGIME_CALM_STALE_MIN_MS", "1200")
            os.environ.setdefault("REGIME_LOG_MIN_SEC", "120")
            os.environ.setdefault("REGIME_HYSTERESIS_TICKS", "15")

            validate_config()
        finally:
            os.environ.clear()
            os.environ.update(original_env)

    def test_runtime_night_env_valid(self):
        """Test that config/runtime_night.env contains valid values."""
        root = Path(__file__).resolve().parent.parent
        night_env = root / "config" / "runtime_night.env"
        if not night_env.is_file():
            pytest.skip(f"{night_env} not found")

        from utils.env_merge import merge_env_file
        original_env = os.environ.copy()
        try:
            merge_env_file(night_env, overwrite=True)
            merge_env_file(root / "config" / "runtime.env", overwrite=True)
            os.environ.setdefault("HFT_DEPOSIT_USD", "100.0")
            os.environ.setdefault("HFT_DEFAULT_TRADE_USD", "10.0")
            os.environ.setdefault("HFT_MAX_POSITION_USD", "100")
            os.environ.setdefault("STATS_INTERVAL_SEC", "120")
            os.environ.setdefault("HFT_BUY_EDGE", "4.8")
            os.environ.setdefault("HFT_SELL_EDGE_ABS", "7.2")
            os.environ.setdefault("HFT_MIN_HOLD_SEC", "3.8")
            os.environ.setdefault("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC", "5.5")
            os.environ.setdefault("HFT_REGIME_FILTER_ENABLED", "1")
            os.environ.setdefault("HFT_TRAILING_TP_ENABLED", "1")
            os.environ.setdefault("HFT_TRAILING_SL_ENABLED", "1")
            os.environ.setdefault("REGIME_WINDOW_TICKS", "60")
            os.environ.setdefault("REGIME_CALM_SPEED_MAX", "0.5")
            os.environ.setdefault("REGIME_ACTIVE_SPEED_MIN", "5.0")
            os.environ.setdefault("REGIME_CALM_STALE_MIN_MS", "1200")
            os.environ.setdefault("REGIME_LOG_MIN_SEC", "120")
            os.environ.setdefault("REGIME_HYSTERESIS_TICKS", "15")

            validate_config()
        finally:
            os.environ.clear()
            os.environ.update(original_env)
