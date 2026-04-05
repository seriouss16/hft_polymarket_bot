"""Centralized configuration validation for HFT bot environment variables.

This module defines validation schemas for all critical configuration parameters
and provides a validate_config() function that checks the environment at startup.

Design goals:
- Clear error messages indicating which parameter is invalid and why
- Backward compatible: existing valid configs should pass
- Easy to extend for new parameters
- Covers types, ranges, logical dependencies, and enum choices

Note: Most parameters are optional (have defaults in code). This validation
only checks that if a parameter is set, it must be valid. Required parameters
are checked by validate_required_config() in bot_config_log.py.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class ValidationError(ValueError):
    """Raised when a configuration parameter fails validation."""

    def __init__(self, param_name: str, value: Any, reason: str) -> None:
        self.param_name = param_name
        self.value = value
        self.reason = reason
        super().__init__(f"Config validation failed: {param_name}={value!r} - {reason}")


class ZscoreStrictness(Enum):
    """Valid values for HFT_ZSCORE_MONOTONIC_STRICTNESS."""

    STRICT = "strict"
    RELAXED = "relaxed"
    OFF = "off"

    @classmethod
    def valid_values(cls) -> set[str]:
        return {v.value for v in cls}


@dataclass
class ParameterSpec:
    """Specification for validating a single configuration parameter."""

    name: str
    type: type
    required: bool = True
    min: float | int | None = None
    max: float | int | None = None
    choices: set[Any] | None = None
    custom_validator: Callable[[Any], str | None] | None = None
    depends_on: list[str] | None = None  # other params that must be validated first


class ConfigValidator:
    """Validates all configuration parameters against defined schemas."""

    def __init__(self) -> None:
        self._specs: dict[str, ParameterSpec] = {}
        self._define_schemas()

    def _define_schemas(self) -> None:
        """Define validation schemas for all configuration parameters."""
        # ==============================================================================
        # CORE TRADING PARAMETERS (most are required by validate_required_config)
        # ==============================================================================

        # Deposit and sizing
        self._add_float("HFT_DEPOSIT_USD", min=0.0, required=True)
        self._add_float("HFT_DEFAULT_TRADE_USD", min=0.0, required=True)
        self._add_float("HFT_MAX_POSITION_USD", min=0.0, required=True)
        self._add_float("HFT_DYNAMIC_RISK_PER_TICK_USD", min=0.0, required=False)
        self._add_float("HFT_DYNAMIC_AMOUNT_MIN_USD", min=0.0, required=False)
        self._add_float("HFT_DYNAMIC_AMOUNT_MAX_USD", min=0.0, required=False)
        self._add_float("HFT_DYNAMIC_CHEAP_PRICE_BELOW", min=0.0, max=1.0, required=False)
        self._add_float("HFT_DYNAMIC_RICH_PRICE_ABOVE", min=0.0, max=1.0, required=False)
        self._add_float("HFT_DYNAMIC_MIN_EXEC_PRICE", min=0.0, required=False)
        self._add_float("HFT_DYNAMIC_FLOOR_NOTIONAL_USD", min=0.0, required=False)
        self._add_float("HFT_DYNAMIC_AMOUNT_CHEAP_USD", min=0.0, required=False)
        self._add_float("HFT_DYNAMIC_AMOUNT_RICH_USD", min=0.0, required=False)

        # Edge thresholds
        self._add_float("HFT_BUY_EDGE", min=0.0, required=True)
        self._add_float("HFT_NOISE_EDGE", min=0.0, required=False)
        self._add_float("HFT_SELL_EDGE_ABS", min=0.0, required=True)
        self._add_float("HFT_STRONG_EDGE_RSI_MULT", min=1.0, required=False)
        self._add_float("HFT_AGGRESSIVE_EDGE_MULT", min=1.0, required=False)
        self._add_float("HFT_WIDE_SPREAD_MIN_EDGE", min=0.0, required=False)
        self._add_float("HFT_ENTRY_EXTREME_MIN_EDGE", min=0.0, required=False)

        # Timing and cooldowns
        self._add_float("HFT_COOLDOWN_SEC", min=0.0, required=False)
        self._add_float("HFT_MIN_HOLD_SEC", min=0.0, required=True)
        self._add_float("HFT_POST_CLOSE_REENTRY_COOLDOWN_SEC", min=0.0, required=False)
        self._add_float("HFT_REACTION_TIMEOUT_SEC", min=0.0, required=False)
        self._add_float("HFT_ENTRY_CONFIRM_AGE_SEC", min=0.0, required=False)
        self._add_float("HFT_ENTRY_CONFIRM_AGE_STRONG_SEC", min=0.0, required=False)
        self._add_float("HFT_REVERSAL_CONFIRM_AGE_SEC", min=0.0, required=False)
        self._add_float("HFT_TREND_FLIP_MIN_AGE_SEC", min=0.0, required=False)
        self._add_float("HFT_AGGRESSIVE_ENTRY_LOG_MIN_SEC", min=0.0, required=False)
        self._add_float("HFT_ENTRY_CAP_DENY_LOG_SEC", min=0.0, required=False)
        self._add_float("HFT_FEED_GATE_LOG_MIN_SEC", min=0.0, required=False)
        self._add_float("HFT_REGIME_SKIP_LOG_MIN_SEC", min=0.0, required=False)
        self._add_float("HFT_SLOT_EXPIRY_INFO_LOG_MIN_SEC", min=0.0, required=False)
        self._add_float("HFT_FILTER_DIAG_LOG_SEC", min=0.0, required=False)

        # Profit/Loss management
        self._add_float("HFT_TARGET_PROFIT_USD", min=0.0, required=False)
        self._add_float("HFT_STOP_LOSS_USD", min=0.0, required=False)
        self._add_float("HFT_PNL_TP_PERCENT", min=0.0, max=1.0, required=False)
        self._add_float("HFT_PNL_SL_PERCENT", min=0.0, max=1.0, required=False)
        self._add_float("HFT_PNL_TP_MIN_HOLD_SEC", min=0.0, required=False)
        self._add_float("HFT_TRAILING_TP_ACTIVATE_USD", min=0.0, required=False)
        self._add_float("HFT_TRAILING_TP_PULLBACK_PCT", min=0.0, max=1.0, required=False)
        self._add_float("HFT_TRAILING_TP_MIN_PULLBACK_USD", min=0.0, required=False)
        self._add_float("HFT_TRAILING_SL_BREAKEVEN_AT_USD", min=0.0, required=False)
        self._add_float("HFT_TRAILING_SL_STEP_USD", min=0.0, required=False)
        self._add_float("HFT_TRAILING_SL_STEP_LOCK_PCT", min=0.0, max=1.0, required=False)

        # ==============================================================================
        # RSI PARAMETERS
        # ==============================================================================

        self._add_int("HFT_RSI_PRICE_LEN", min=1, max=1000, required=False)
        self._add_int("HFT_ADX_PERIOD", min=1, max=1000, required=False)
        self._add_int("HFT_ADX_TICK_LEN", min=1, max=10000, required=False)

        # RSI entry bands
        self._add_float("HFT_RSI_ENTRY_UP_LOW", min=0.0, max=100.0, required=False)
        self._add_float("HFT_RSI_ENTRY_UP_HIGH", min=0.0, max=100.0, required=False)
        self._add_float("HFT_RSI_ENTRY_DOWN_LOW", min=0.0, max=100.0, required=False)
        self._add_float("HFT_RSI_ENTRY_DOWN_HIGH", min=0.0, max=100.0, required=False)

        # RSI exit bands
        self._add_float("HFT_RSI_EXIT_UPPER_BASE", min=0.0, max=100.0, required=False)
        self._add_float("HFT_RSI_EXIT_LOWER_BASE", min=0.0, max=100.0, required=False)
        self._add_float("HFT_RSI_RANGE_EXIT_BAND_MARGIN", min=0.0, required=False)
        self._add_float("HFT_RSI_RANGE_EXIT_MIN_PROFIT_USD", min=0.0, required=False)
        self._add_float("HFT_RSI_RANGE_EXIT_PROFIT_FRAC", min=0.0, max=1.0, required=False)
        self._add_float("HFT_RSI_RANGE_EXIT_MIN_HOLD_SEC", min=0.0, required=False)
        self._add_float("HFT_RSI_RANGE_EXIT_FADE_BUFFER", min=0.0, required=False)
        self._add_float("HFT_RSI_EXTREME_HIGH", min=0.0, max=100.0, required=False)
        self._add_float("HFT_RSI_EXTREME_LOW", min=0.0, max=100.0, required=False)
        self._add_float("HFT_RSI_BAND_VOL_K", min=0.0, required=False)

        # RSI clamp (critical: must have HIGH > LOW)
        self._add_float(
            "HFT_RSI_EXIT_CLAMP_HIGH",
            min=0.0,
            max=100.0,
            required=False,
            custom_validator=self._validate_clamp_high,
        )
        self._add_float(
            "HFT_RSI_EXIT_CLAMP_LOW",
            min=0.0,
            max=100.0,
            required=False,
            custom_validator=self._validate_clamp_low,
        )

        # RSI slope exits (critical: UP < 0, DOWN > 0)
        self._add_float(
            "HFT_RSI_SLOPE_EXIT_UP",
            required=True,
            custom_validator=self._validate_slope_up,
        )
        self._add_float(
            "HFT_RSI_SLOPE_EXIT_DOWN",
            required=True,
            custom_validator=self._validate_slope_down,
        )

        # RSI hold floors/ceilings
        self._add_float("HFT_RSI_HOLD_UP_FLOOR", min=0.0, max=100.0, required=False)
        self._add_float("HFT_RSI_HOLD_DOWN_CEILING", min=0.0, max=100.0, required=False)
        self._add_float("HFT_INTERNAL_REVERSAL_IMB_RSI_GATE", min=0.0, max=100.0, required=False)

        # RSI slope entry filters
        self._add_float("HFT_RSI_UP_ENTRY_MAX", min=0.0, max=100.0, required=False)
        self._add_float("HFT_RSI_UP_SLOPE_MIN", min=0.0, required=False)  # Should be positive for UP entries
        self._add_float("HFT_RSI_DOWN_ENTRY_MIN", min=0.0, max=100.0, required=False)
        self._add_float("HFT_RSI_DOWN_SLOPE_MAX", max=0.0, required=False)  # Should be negative for DOWN entries

        # ==============================================================================
        # ENTRY GATES AND FILTERS
        # ==============================================================================

        # Price/speed gates
        self._add_float("HFT_ENTRY_MAX_LATENCY_MS", min=0.0, required=False)
        # Signed ms bounds (see data/aggregator feed_timing skew_ms). Not |skew|.
        # HFTEngine applies entry_min_skew_ms <= skew_ms <= entry_max_skew_ms when max > 0.
        # max <= 0 disables the skew gate (legacy); min may be negative (e.g. -3200).
        self._add_float("HFT_ENTRY_MAX_SKEW_MS", required=False)
        self._add_float("HFT_ENTRY_MIN_SKEW_MS", required=False)  # can be -inf
        self._add_float("HFT_LATENCY_HIGH_MS", min=0.0, required=False)
        self._add_float("HFT_LATENCY_HIGH_EDGE_MULT", min=1.0, required=False)
        self._add_float("HFT_ENTRY_MAX_ASK_UP", min=0.0, max=1.0, required=False)
        self._add_float("HFT_ENTRY_MAX_ASK_DOWN", min=0.0, max=1.0, required=False)
        self._add_float("HFT_ENTRY_MIN_ASK_UP", min=0.0, max=1.0, required=False)
        self._add_float("HFT_ENTRY_MIN_ASK_DOWN", min=0.0, max=1.0, required=False)
        self._add_float("HFT_MAX_ENTRY_SPREAD", min=0.0, max=1.0, required=False)
        self._add_float("HFT_ENTRY_LOW_SPEED_ABS", min=0.0, required=False)
        self._add_float("HFT_ENTRY_LOW_SPEED_EDGE_MULT", min=1.0, required=False)
        self._add_float("HFT_ENTRY_UP_SPEED_MIN", required=False)  # can be negative for DOWN
        self._add_float("HFT_ENTRY_DOWN_SPEED_MAX", required=False)  # can be negative
        self._add_float("HFT_SPEED_FLOOR", min=0.0, required=False)
        self._add_float("HFT_ENTRY_MAX_EDGE_JUMP_PTS", min=0.0, required=False)
        self._add_float("HFT_ENTRY_EDGE_JUMP_BYPASS_ABS_SPEED", min=0.0, required=False)
        self._add_float("HFT_ENTRY_ZSCORE_BYPASS_ABS_SPEED", min=0.0, required=False)
        self._add_float("HFT_AGGRESSIVE_MIN_TREND_AGE_SEC", min=0.0, required=False)
        self._add_float("HFT_AGGRESSIVE_EXTREME_ASK_HI", min=0.0, max=1.0, required=False)
        self._add_float("HFT_AGGRESSIVE_EXTREME_ASK_LO", min=0.0, max=1.0, required=False)

        # Z-score parameters
        self._add_int("HFT_ENTRY_ZSCORE_STRICT_TICKS", min=0, required=False)
        self._add_str_enum("HFT_ZSCORE_MONOTONIC_STRICTNESS", {"strict", "relaxed", "off"}, required=False)

        # ==============================================================================
        # BOOK AND LIQUIDITY
        # ==============================================================================

        self._add_float("HFT_BOOK_MOVE_ENTRY_MIN", min=0.0, required=False)
        self._add_float("HFT_BOOK_MOVE_STOP_MAX", min=0.0, required=False)
        self._add_int("HFT_BOOK_STALL_TICKS", min=0, required=False)
        self._add_float("HFT_MIN_IMBALANCE_ENTRY", min=0.0, max=1.0, required=False)
        self._add_int("HFT_BOOK_TOP_N", min=1, max=100, required=False)

        # ==============================================================================
        # SLOT AND TIMING
        # ==============================================================================

        self._add_float("HFT_SLOT_INTERVAL_SEC", min=60.0, max=3600.0, required=False)
        self._add_float("HFT_NO_ENTRY_FIRST_SEC", min=0.0, required=False)
        self._add_float("HFT_NO_ENTRY_LAST_SEC", min=0.0, required=False)
        self._add_float("HFT_SLOT_FORCE_CLOSE_LAST_SEC", min=0.0, required=False)
        self._add_float("HFT_SLOT_99C_MAX_SEC", min=0.0, required=False)
        self._add_float("HFT_SLOT_EXPIRY_INFO_MAX_SEC", min=0.0, required=False)
        self._add_float("HFT_EXPIRY_TIGHT_SEC", min=0.0, required=False)
        self._add_float("HFT_EXPIRY_EDGE_MULT", min=1.0, required=False)

        # ==============================================================================
        # MARKET REGIME
        # ==============================================================================

        self._add_int("REGIME_WINDOW_TICKS", min=10, max=1000, required=True)
        self._add_float("REGIME_CALM_SPEED_MAX", min=0.0, required=True)
        self._add_float("REGIME_ACTIVE_SPEED_MIN", min=0.0, required=True)
        self._add_float("REGIME_CALM_STALE_MIN_MS", min=0.0, required=True)
        self._add_float("REGIME_LOG_MIN_SEC", min=0.0, required=True)
        self._add_int("REGIME_HYSTERESIS_TICKS", min=1, required=True)

        # ==============================================================================
        # REACTION SCORE
        # ==============================================================================

        self._add_int("HFT_REACTION_MA_PERIOD", min=1, max=200, required=False)
        self._add_int("HFT_REACTION_MACD_FAST", min=1, max=200, required=False)
        self._add_int("HFT_REACTION_MACD_SLOW", min=1, max=200, required=False)
        self._add_int("HFT_REACTION_MACD_SIGNAL", min=1, max=200, required=False)
        self._add_float("HFT_REACTION_MA_REL_SCALE", min=0.0, required=False)
        self._add_float("HFT_REACTION_MACD_HIST_SCALE", min=0.0, required=False)
        self._add_float("HFT_REACTION_W_RSI", min=0.0, max=1.0, required=False)
        self._add_float("HFT_REACTION_W_MA", min=0.0, max=1.0, required=False)
        self._add_float("HFT_REACTION_W_MACD", min=0.0, max=1.0, required=False)

        # ==============================================================================
        # SESSION PROFILES (soft_flow)
        # ==============================================================================

        self._add_float("HFT_SOFT_NOISE_EDGE", min=0.0, required=False)
        self._add_float("HFT_SOFT_BUY_EDGE", min=0.0, required=False)
        self._add_float("HFT_SOFT_ENTRY_CONFIRM_AGE_SEC", min=0.0, required=False)
        self._add_float("HFT_SOFT_ENTRY_CONFIRM_AGE_STRONG_SEC", min=0.0, required=False)
        self._add_float("HFT_SOFT_STRONG_EDGE_RSI_MULT", min=1.0, required=False)
        self._add_float("HFT_SOFT_AGGRESSIVE_EDGE_MULT", min=1.0, required=False)
        self._add_float("HFT_SOFT_ENTRY_MAX_EDGE_JUMP_PTS", min=0.0, required=False)
        self._add_float("HFT_SOFT_ENTRY_MAX_LATENCY_MS", min=0.0, required=False)
        self._add_float("HFT_SOFT_ENTRY_LOW_SPEED_ABS", min=0.0, required=False)
        self._add_float("HFT_SOFT_ENTRY_LOW_SPEED_EDGE_MULT", min=1.0, required=False)
        self._add_float("HFT_SOFT_SPEED_FLOOR", min=0.0, required=False)
        self._add_float("HFT_PHASE_SOFT_MIN_ABS_EDGE", min=0.0, required=False)
        self._add_float("HFT_PHASE_SOFT_MAX_ABS_SPEED", min=0.0, required=False)
        self._add_float("HFT_PHASE_SOFT_MAX_ABS_EDGE", min=0.0, required=False)
        self._add_float("HFT_PHASE_VOLATILE_MIN_ABS_SPEED", min=0.0, required=False)
        self._add_float("HFT_PHASE_VOLATILE_MIN_ABS_EDGE", min=0.0, required=False)
        self._add_float("HFT_PHASE_SOFT_MIN_TREND_AGE_SEC", min=0.0, required=False)
        self._add_float("HFT_PHASE_SOFT_MAX_FEED_LATENCY_MS", min=0.0, required=False)

        # ==============================================================================
        # AGGRESSIVE ENTRY PARAMETERS
        # ==============================================================================

        self._add_float("HFT_AGGRESSIVE_ENTRY_RELAX_SPEED", required=False)
        self._add_float("HFT_AGGRESSIVE_ENTRY_RELAX_SPEED_DOWN", required=False)

        # ==============================================================================
        # POLYMARKET / CLOB PARAMETERS
        # ==============================================================================

        self._add_float("POLY_CLOB_MIN_SHARES", min=1.0, required=False)
        self._add_float("CLOB_BOOK_HTTP_TIMEOUT", min=0.1, max=30.0, required=False)
        self._add_float("CLOB_MARKET_WS_OPEN_TIMEOUT_SEC", min=1.0, max=60.0, required=False)
        self._add_float("CLOB_USER_WS_OPEN_TIMEOUT_SEC", min=1.0, max=60.0, required=False)
        self._add_float("CLOB_MARKET_WS_MAX_STALE_SEC", min=1.0, max=120.0, required=False)
        self._add_float("CLOB_USER_WS_MAX_STALE_SEC", min=1.0, max=120.0, required=False)
        self._add_float("CLOB_WS_STALE_WARN_SEC", min=0.0, max=60.0, required=False)
        self._add_float("CLOB_WS_STALE_SKIP_SEC", min=0.0, max=120.0, required=False)
        self._add_float("CLOB_WS_RECONNECT_BASE_SEC", min=0.1, max=10.0, required=False)
        self._add_float("CLOB_WS_RECONNECT_MAX_SEC", min=1.0, max=300.0, required=False)
        self._add_float("CLOB_WS_RECONNECT_JITTER_MS", min=0.0, max=5000.0, required=False)
        self._add_float("CLOB_WS_HEALTH_LOG_INTERVAL_SEC", min=10.0, max=3600.0, required=False)
        self._add_float("CLOB_MARKET_WS_PING_SEC", min=1.0, max=60.0, required=False)
        self._add_float("CLOB_USER_WS_PING_SEC", min=1.0, max=60.0, required=False)
        self._add_int("CLOB_USER_WS_MAX_ORDER_ENTRIES", min=100, max=100000, required=False)
        self._add_float("CLOB_BOOK_PULL_SEC", min=0.1, max=10.0, required=False)

        # ==============================================================================
        # LIVE TRADING PARAMETERS
        # ==============================================================================

        self._add_float("LIVE_CLOB_BOOK_HTTP_TIMEOUT", min=0.1, max=30.0, required=False)
        self._add_float("LIVE_ORDER_FILL_POLL_SEC", min=0.001, max=5.0, required=False)
        self._add_float("LIVE_ORDER_STALE_SEC", min=0.001, max=60.0, required=False)
        self._add_int("LIVE_ORDER_MAX_REPRICE", min=0, max=10, required=False)
        self._add_int("LIVE_ORDER_EMERGENCY_TICKS", min=0, max=10, required=False)
        self._add_float("LIVE_REPRICE_POST_CANCEL_SLEEP_SEC", min=0.0, max=5.0, required=False)
        self._add_int("LIVE_REPRICE_POST_CANCEL_FILL_POLLS", min=0, max=20, required=False)
        self._add_float("LIVE_REPRICE_POST_CANCEL_POLL_SEC", min=0.001, max=5.0, required=False)
        self._add_float("LIVE_EMERGENCY_BUY_BUMP", min=0.0, max=0.2, required=False)
        self._add_float("LIVE_EMERGENCY_SPREAD_CROSS_BUMP", min=0.0, max=0.2, required=False)
        self._add_float("LIVE_USDC_DEBIT_VERIFY_ABS_USD", min=0.0, max=10.0, required=False)
        self._add_float("LIVE_USDC_DEBIT_VERIFY_REL", min=0.0, max=0.5, required=False)
        self._add_str_list("LIVE_USDC_DEBIT_VERIFY_DELAYS_SEC", min_len=1, required=False)
        self._add_float("LIVE_INVENTORY_DUST_SHARES", min=0.0, required=False)
        self._add_float("LIVE_CHAIN_EXIT_DUST_SHARES", min=0.0, required=False)
        self._add_float("LIVE_SELL_CHAIN_DUST_SHARES", min=0.0, required=False)
        self._add_float("LIVE_IMMEDIATE_FILL_CHAIN_WAIT_SEC", min=0.0, max=10.0, required=False)
        self._add_float("LIVE_STRICT_CHAIN_EXTRA_WAIT_SEC", min=0.0, max=30.0, required=False)
        self._add_int("LIVE_STRICT_CHAIN_EXTRA_POLLS", min=0, max=50, required=False)
        self._add_float("LIVE_STRICT_CHAIN_EXTRA_POLL_GAP_SEC", min=0.0, max=5.0, required=False)
        self._add_float("LIVE_EMERGENCY_BUY_BALANCE_MARGIN", min=0.0, max=0.1, required=False)
        self._add_float("LIVE_BUY_REPRICE_TICK", min=0.0001, max=0.1, required=False)
        self._add_float("LIVE_SELL_REPRICE_TICK", min=0.0001, max=0.1, required=False)
        self._add_float("LIVE_BUY_PRICE_OFFSET", min=0.0, max=0.1, required=False)
        self._add_float("LIVE_TRUST_CLOB_WITHOUT_CHAIN_BALANCE", required=False)
        self._add_float("LIVE_ORDER_WS_TIMEOUT_SEC", min=0.1, max=120.0, required=False)

        # ==============================================================================
        # BALANCE CACHE
        # ==============================================================================

        self._add_float("BALANCE_CACHE_MAX_AGE_SEC", min=0.1, max=300.0, required=False)
        self._add_float("BALANCE_CONDITIONAL_MAX_AGE_SEC", min=0.1, max=600.0, required=False)
        self._add_int("BALANCE_CACHE_MAX_CONDITIONAL_ENTRIES", min=1, max=10000, required=False)
        self._add_float("ALLOWANCE_CACHE_TTL_SEC", min=10.0, max=3600.0, required=False)
        self._add_int("ALLOWANCE_CACHE_MAX_ENTRIES", min=1, max=100000, required=False)
        self._add_float("ALLOWANCE_CACHE_CLEAN_INTERVAL_SEC", min=5.0, max=3600.0, required=False)
        self._add_int("ALLOWANCE_REFRESH_QUEUE_MAX", min=1, max=10000, required=False)

        # ==============================================================================
        # SIMULATION PARAMETERS
        # ==============================================================================

        self._add_float("HFT_SIM_FEE_RATE", min=0.0, max=0.1, required=False)
        self._add_float("HFT_SIM_SLIPPAGE_EXTRA_FRACTION", min=0.0, max=1.0, required=False)
        self._add_float("HFT_SIM_SLIPPAGE_EXTRA_FRACTION_PER_SEC", min=0.0, max=1.0, required=False)
        self._add_float("HFT_SIM_SLIPPAGE_EXTRA_CAP_FRACTION", min=0.0, max=1.0, required=False)
        self._add_float("HFT_SIM_EXIT_SLIPPAGE_FRACTION", min=0.0, max=1.0, required=False)

        # ==============================================================================
        # REGIME FILTER
        # ==============================================================================

        self._add_float("HFT_GOOD_REGIME_WINRATE", min=0.0, max=1.0, required=False)
        self._add_float("HFT_BAD_REGIME_WINRATE", min=0.0, max=1.0, required=False)
        self._add_int("HFT_RECENT_TRADES_FOR_REGIME", min=1, max=1000, required=False)
        self._add_float("HFT_REGIME_COOLDOWN_SEC", min=0.0, required=False)

        # ==============================================================================
        # STRATEGY HUB
        # ==============================================================================

        self._add_int("HFT_STRATEGY_TIMEOUT_MS", min=0, max=10000, required=False)

        # ==============================================================================
        # BOOLEAN FLAGS (validated as strings: 0/1, true/false, yes/no)
        # ==============================================================================

        bool_flags = [
            "HFT_BYPASS_META_GATE",
            "USE_SMART_FAST",
            "HFT_DISABLE_BINANCE_FAST",
            "HFT_ENABLE_LSTM",
            "HFT_ENABLE_PHASE_ROUTING",
            "HFT_PARALLEL_STRATEGIES",
            "HFT_USE_GATHER",
            "HFT_DEBUG_LOG_ENABLED",
            "HFT_PULSE_LOG_ENABLED",
            "HFT_LOG_MARKET_PROFILE",
            "HFT_REUSE_ENTRY_CONTEXT",
            "HFT_CACHE_TREND_STATE",
            "HFT_OBJECT_POOL_SIZE",
            "HFT_USE_UVLOOP",
            "HFT_USE_INCREMENTAL_INDICATORS",
            "HFT_RSI_CACHE_ENABLED",
            "HFT_ADX_CACHE_ENABLED",
            "HFT_CACHE_BOOK_SNAPSHOT",
            "HFT_INCREMENTAL_IMBALANCE",
            "HFT_USE_INCREMENTAL_ZSCORE",
            "HFT_ENTRY_ACCEL_ENABLED",
            "HFT_TRAILING_TP_ENABLED",
            "HFT_TRAILING_SL_ENABLED",
            "HFT_RSI_SLOPE_EXIT_ENABLED",
            "HFT_ENTRY_RSI_SLOPE_FILTER_ENABLED",
            "HFT_ENTRY_MOMENTUM_ALT_ENABLED",
            "HFT_AGGRESSIVE_EXTREME_ASK_BLOCK",
            "HFT_REGIME_FILTER_ENABLED",
            "HFT_REGIME_BYPASS_SOFT_FLOW",
            "HFT_LOG_PHASE_DIAGNOSTICS",
            "HFT_PERF_RESET_ON_NEW_MARKET",
            "HFT_NO_ENTRY_GUARDS",
            "HFT_RSI_ALLOW_BYPASS_STRONG_EDGE",
            "HFT_RSI_ALLOW_BYPASS_AGGRESSIVE_EDGE",
            "HFT_SOFT_ENTRY_MOMENTUM_ALT_ENABLED",
            "HFT_SOFT_RSI_ALLOW_BYPASS_AGGRESSIVE_EDGE",
            "CLOB_MARKET_WS_ENABLED",
            "CLOB_USER_WS_ENABLED",
            "CLOB_MARKET_WS_CUSTOM_FEATURES",
            "CLOB_MARKET_WS_PRIMARY",
            "LIVE_USDC_DEBIT_VERIFY",
            "LIVE_SKIP_PRESELL_BALANCE",
            "LIVE_SKIP_COOLDOWN_ON_SLIPPAGE_ABORT",
            "LIVE_APPLY_COOLDOWN_ON_STALE_NO_FILL",
            "LIVE_TRUST_CLOB_WITHOUT_CHAIN_BALANCE",
            "POLY_RTDS_BTC_ONLY",
        ]
        for flag in bool_flags:
            self._add_bool(flag, required=False)

        # ==============================================================================
        # STRING PARAMETERS
        # ==============================================================================

        self._add_str("HFT_ACTIVE_STRATEGY", choices={"latency_arbitrage", "phase_router"}, required=False)
        self._add_str("HFT_LIVE_SIGNAL_STRATEGY", choices={"latency_arbitrage"}, required=False)
        self._add_str_enum("HFT_ZSCORE_MONOTONIC_STRICTNESS", ZscoreStrictness.valid_values(), required=False)

        # ==============================================================================
        # SPECIAL / OPTIONAL PARAMETERS (not in main config files but may be set)
        # ==============================================================================

        self._add_float("HFT_LOOP_SLEEP_SEC", min=0.0, required=False)
        self._add_float("PULSE_INTERVAL_SEC", min=0.0, required=False)
        self._add_float("HFT_FAST_LOG_MIN_SEC", min=0.0, required=False)
        self._add_float("HFT_SLOT_POLL_SEC", min=0.0, required=False)
        self._add_float("HFT_MIN_SLOT_POLL_SEC", min=0.0, required=False)
        self._add_float("HFT_WS_RECONNECT_SEC", min=0.1, max=60.0, required=False)
        self._add_float("HFT_MAX_RUNTIME_SEC", min=0.0, required=False)
        self._add_float("STATS_INTERVAL_SEC", min=10.0, max=3600.0, required=True)
        self._add_float("HFT_LOG_DEDUPE_SAME_MSG_SEC", min=0.0, required=False)
        self._add_float("HFT_GAMMA_CACHE_TTL_SEC", min=0.0, required=False)
        self._add_int("HFT_OBJECT_POOL_SIZE", min=1, max=10000, required=False)
        self._add_float("HFT_SMART_CB_BN_BASELINE_USD", min=0.0, required=False)
        self._add_float("HFT_SMART_EXCESS_THRESHOLD_USD", min=0.0, required=False)
        self._add_float("HFT_SMART_BINANCE_BLEND", min=0.0, max=1.0, required=False)
        self._add_float("HFT_SMART_DRIFT_THRESHOLD_USD", min=0.0, required=False)
        self._add_float("HFT_LOG_KEEP_FILES", min=1, max=10000, required=False)
        self._add_str("HFT_LOG_DIR", required=False)  # string path, no validation
        self._add_str("DEBUG_LOG_PATH", required=False)  # string path, no validation
        self._add_float("HFT_NIGHT_START_UTC_HOUR", min=0, max=23, required=False)
        self._add_float("HFT_NIGHT_END_UTC_HOUR", min=0, max=23, required=False)
        self._add_float("CLOB_BOOK_HTTP_FALLBACK_INTERVAL_SEC", min=1.0, max=300.0, required=False)
        self._add_float("GAMMA_API_CACHE_TTL_SEC", min=0.0, max=60.0, required=False)
        self._add_str_list("LIVE_CLOSE_CHAIN_PROBE_DELAYS_SEC", min_len=1, required=False)
        self._add_float("LIVE_CLOSE_WAIT_PENDING_SEC", min=0.0, required=False)
        self._add_float("LIVE_SELL_GTC_OFFSET_FROM_BID", required=False)
        self._add_float("LIVE_SELL_PLACE_ATTEMPTS", min=1, max=10, required=False)
        self._add_float("LIVE_SELL_FAK_ATTEMPTS", min=1, max=10, required=False)
        self._add_float("LIVE_SELL_PLACE_RETRY_SLEEP_SEC", min=0.0, required=False)
        self._add_float("LIVE_SELL_FAK_RETRY_SLEEP_SEC", min=0.0, required=False)
        self._add_str_list("LIVE_SELL_BALANCE_WAIT_DELAYS_SEC", min_len=1, required=False)
        self._add_float("LIVE_CHAIN_EXIT_DUST_SHARES", min=0.0, required=False)
        self._add_float("LIVE_SELL_CHAIN_DUST_SHARES", min=0.0, required=False)
        self._add_float("LIVE_POST_SELL_CHAIN_DELAY_SEC", min=0.0, required=False)
        self._add_float("LIVE_BUY_COLLATERAL_SAFETY", min=0.0, max=1.0, required=False)
        self._add_float("LIVE_BALANCE_MIN_FRAC", min=0.0, max=1.0, required=False)
        self._add_str_list("LIVE_BALANCE_CONFIRM_DELAYS_SEC", min_len=1, required=False)
        self._add_float("LIVE_MAX_BOOK_AGE_SEC", min=0.0, required=False)
        self._add_float("LIVE_INVENTORY_RECONCILE_SEC", min=0.0, required=False)
        self._add_float("LIVE_INVENTORY_RECONCILE_AFTER_CLOSE_SEC", min=0.0, required=False)
        self._add_float("LIVE_SKIP_COOLDOWN_SEC", min=0.0, required=False)
        self._add_float("HFT_LIVE_SKIP_STATS_LOG_SEC", min=0.0, required=False)
        self._add_str("TRADE_JOURNAL_PATH", required=False)  # string path

        # ==============================================================================
        # DEPENDENCY VALIDATIONS (cross-parameter)
        # ==============================================================================

        # These will be checked after individual parameters are validated
        self._dependency_checks: list[tuple[list[str], Callable[[], str | None]]] = [
            (["HFT_RSI_EXIT_CLAMP_HIGH", "HFT_RSI_EXIT_CLAMP_LOW"], self._check_clamp_order),
            (
                ["HFT_RSI_SLOPE_EXIT_UP", "HFT_RSI_SLOPE_EXIT_DOWN"],
                self._check_slope_signs,
            ),
            (
                ["HFT_ENTRY_MIN_SKEW_MS", "HFT_ENTRY_MAX_SKEW_MS"],
                self._check_skew_order,
            ),
            (
                ["HFT_DYNAMIC_AMOUNT_MIN_USD", "HFT_DYNAMIC_AMOUNT_MAX_USD"],
                self._check_min_max,
            ),
            (
                ["HFT_ENTRY_MIN_ASK_UP", "HFT_ENTRY_MAX_ASK_UP"],
                self._check_ask_band,
            ),
            (
                ["HFT_ENTRY_MIN_ASK_DOWN", "HFT_ENTRY_MAX_ASK_DOWN"],
                self._check_ask_band,
            ),
            (
                ["HFT_REACTION_W_RSI", "HFT_REACTION_W_MA", "HFT_REACTION_W_MACD"],
                self._check_reaction_weights,
            ),
            (
                ["HFT_RSI_EXIT_UPPER_BASE", "HFT_RSI_EXIT_LOWER_BASE"],
                self._check_rsi_base_order,
            ),
        ]

    def _add_float(
        self,
        name: str,
        min: float | None = None,
        max: float | None = None,
        required: bool = True,
        custom_validator: Callable[[Any], str | None] | None = None,
    ) -> None:
        self._specs[name] = ParameterSpec(
            name=name,
            type=float,
            required=required,
            min=min,
            max=max,
            custom_validator=custom_validator,
        )

    def _add_int(
        self,
        name: str,
        min: int | None = None,
        max: int | None = None,
        required: bool = True,
    ) -> None:
        self._specs[name] = ParameterSpec(
            name=name,
            type=int,
            required=required,
            min=min,
            max=max,
        )

    def _add_bool(self, name: str, required: bool = True) -> None:
        self._specs[name] = ParameterSpec(
            name=name,
            type=bool,
            required=required,
        )

    def _add_str(self, name: str, choices: set[str] | None = None, required: bool = True) -> None:
        self._specs[name] = ParameterSpec(
            name=name,
            type=str,
            required=required,
            choices=choices,
        )

    def _add_str_enum(self, name: str, choices: set[str], required: bool = True) -> None:
        self._add_str(name, choices=choices, required=required)

    def _add_str_list(self, name: str, min_len: int = 0, required: bool = True) -> None:
        """Special handler for comma-separated string lists."""
        self._specs[name] = ParameterSpec(
            name=name,
            type=str,
            required=required,
        )
        # Store min_len in a separate structure for custom validation
        if not hasattr(self, "_str_list_min_lens"):
            self._str_list_min_lens = {}
        self._str_list_min_lens[name] = min_len

    # ---------------------------------------------------------------------------
    # Custom validators
    # ---------------------------------------------------------------------------

    def _validate_clamp_high(self, value: Any) -> str | None:
        """HFT_RSI_EXIT_CLAMP_HIGH must be > HFT_RSI_EXIT_CLAMP_LOW."""
        low = os.getenv("HFT_RSI_EXIT_CLAMP_LOW")
        if low is not None:
            try:
                low_val = float(low)
                if value <= low_val:
                    return f"must be > HFT_RSI_EXIT_CLAMP_LOW ({low_val}), got {value}"
            except ValueError:
                pass  # low will be validated separately
        return None

    def _validate_clamp_low(self, value: Any) -> str | None:
        """HFT_RSI_EXIT_CLAMP_LOW must be < HFT_RSI_EXIT_CLAMP_HIGH."""
        high = os.getenv("HFT_RSI_EXIT_CLAMP_HIGH")
        if high is not None:
            try:
                high_val = float(high)
                if value >= high_val:
                    return f"must be < HFT_RSI_EXIT_CLAMP_HIGH ({high_val}), got {value}"
            except ValueError:
                pass
        return None

    def _validate_slope_up(self, value: Any) -> str | None:
        """HFT_RSI_SLOPE_EXIT_UP must be < 0 (negative for UP exit)."""
        if value >= 0:
            return f"must be < 0 (negative) for UP exit, got {value}"
        return None

    def _validate_slope_down(self, value: Any) -> str | None:
        """HFT_RSI_SLOPE_EXIT_DOWN must be > 0 (positive for DOWN exit)."""
        if value <= 0:
            return f"must be > 0 (positive) for DOWN exit, got {value}"
        return None

    def _check_clamp_order(self) -> str | None:
        """Check clamp ordering after both are loaded."""
        high = os.getenv("HFT_RSI_EXIT_CLAMP_HIGH")
        low = os.getenv("HFT_RSI_EXIT_CLAMP_LOW")
        if high and low:
            try:
                if float(high) <= float(low):
                    return f"HFT_RSI_EXIT_CLAMP_HIGH ({high}) must be > HFT_RSI_EXIT_CLAMP_LOW ({low})"
            except ValueError:
                pass
        return None

    def _check_slope_signs(self) -> str | None:
        """Check slope sign constraints."""
        up = os.getenv("HFT_RSI_SLOPE_EXIT_UP")
        down = os.getenv("HFT_RSI_SLOPE_EXIT_DOWN")
        errors = []
        if up:
            try:
                if float(up) >= 0:
                    errors.append(f"HFT_RSI_SLOPE_EXIT_UP={up} must be < 0")
            except ValueError:
                pass
        if down:
            try:
                if float(down) <= 0:
                    errors.append(f"HFT_RSI_SLOPE_EXIT_DOWN={down} must be > 0")
            except ValueError:
                pass
        return "; ".join(errors) if errors else None

    def _check_skew_order(self) -> str | None:
        """Check that min_skew <= max_skew (if both set)."""
        min_skew = os.getenv("HFT_ENTRY_MIN_SKEW_MS")
        max_skew = os.getenv("HFT_ENTRY_MAX_SKEW_MS")
        if min_skew and max_skew:
            try:
                if float(min_skew) > float(max_skew):
                    return f"HFT_ENTRY_MIN_SKEW_MS ({min_skew}) must be <= HFT_ENTRY_MAX_SKEW_MS ({max_skew})"
            except ValueError:
                pass
        return None

    def _check_min_max(self) -> str | None:
        """Check min <= max for dynamic amount."""
        min_val = os.getenv("HFT_DYNAMIC_AMOUNT_MIN_USD")
        max_val = os.getenv("HFT_DYNAMIC_AMOUNT_MAX_USD")
        if min_val and max_val:
            try:
                if float(min_val) > float(max_val):
                    return f"HFT_DYNAMIC_AMOUNT_MIN_USD ({min_val}) must be <= HFT_DYNAMIC_AMOUNT_MAX_USD ({max_val})"
            except ValueError:
                pass
        return None

    def _check_ask_band(self) -> str | None:
        """Check that min_ask <= max_ask for both UP and DOWN."""
        errors = []
        # Check UP pair
        min_up = os.getenv("HFT_ENTRY_MIN_ASK_UP")
        max_up = os.getenv("HFT_ENTRY_MAX_ASK_UP")
        if min_up and max_up:
            try:
                if float(min_up) > float(max_up):
                    errors.append(f"HFT_ENTRY_MIN_ASK_UP ({min_up}) must be <= HFT_ENTRY_MAX_ASK_UP ({max_up})")
            except ValueError:
                pass
        # Check DOWN pair
        min_down = os.getenv("HFT_ENTRY_MIN_ASK_DOWN")
        max_down = os.getenv("HFT_ENTRY_MAX_ASK_DOWN")
        if min_down and max_down:
            try:
                if float(min_down) > float(max_down):
                    errors.append(f"HFT_ENTRY_MIN_ASK_DOWN ({min_down}) must be <= HFT_ENTRY_MAX_ASK_DOWN ({max_down})")
            except ValueError:
                pass
        return "; ".join(errors) if errors else None

    def _check_reaction_weights(self) -> str | None:
        """Check that reaction score weights sum to approximately 1.0."""
        w_rsi = os.getenv("HFT_REACTION_W_RSI")
        w_ma = os.getenv("HFT_REACTION_W_MA")
        w_macd = os.getenv("HFT_REACTION_W_MACD")

        weights = []
        for w in [w_rsi, w_ma, w_macd]:
            if w:
                try:
                    weights.append(float(w))
                except ValueError:
                    pass

        if weights:
            total = sum(weights)
            if abs(total - 1.0) > 0.001:
                return f"Reaction weights sum to {total:.4f}, must be 1.0 (HFT_REACTION_W_RSI, HFT_REACTION_W_MA, HFT_REACTION_W_MACD)"
        return None

    def _check_rsi_base_order(self) -> str | None:
        """Check that RSI exit upper base > lower base."""
        upper = os.getenv("HFT_RSI_EXIT_UPPER_BASE")
        lower = os.getenv("HFT_RSI_EXIT_LOWER_BASE")
        if upper and lower:
            try:
                if float(upper) <= float(lower):
                    return f"HFT_RSI_EXIT_UPPER_BASE ({upper}) must be > HFT_RSI_EXIT_LOWER_BASE ({lower})"
            except ValueError:
                pass
        return None

    # ---------------------------------------------------------------------------
    # Public API
    # ---------------------------------------------------------------------------

    def validate(self) -> None:
        """Validate all configuration parameters from environment.

        This validates that all set parameters have valid values and that all
        required parameters are present.

        Raises:
            ValidationError: If any parameter fails validation.
            SystemExit: If validation fails (with clear error message).
        """
        errors: list[str] = []

        # First pass: validate individual parameters
        for name, spec in self._specs.items():
            raw_val = os.environ.get(name)
            if raw_val is None or not str(raw_val).strip():
                if spec.required:
                    errors.append(f"  {name} - required but not set")
                continue

            # Skip custom validators that will be handled in dependency checks
            if name in {
                "HFT_RSI_EXIT_CLAMP_HIGH",
                "HFT_RSI_EXIT_CLAMP_LOW",
                "HFT_RSI_SLOPE_EXIT_UP",
                "HFT_RSI_SLOPE_EXIT_DOWN",
            }:
                continue

            try:
                value = self._parse_value(raw_val, spec.type, name)
                self._validate_range(value, spec)
                self._validate_choices(value, spec)
                if spec.name in getattr(self, "_str_list_min_lens", {}):
                    self._validate_str_list(value, spec.name)
            except ValidationError as e:
                errors.append(f"  {e.param_name} - {e.reason}")

        # Second pass: cross-parameter dependencies
        for dep_names, check_fn in self._dependency_checks:
            try:
                result = check_fn()
                if result:
                    errors.append(f"  {result}")
            except Exception as e:
                errors.append(f"  {' & '.join(dep_names)} - dependency check failed: {e}")

        if errors:
            lines = "\n".join(errors)
            _abort = (
                f"\n{'=' * 60}\n"
                f"🛑  CONFIGURATION VALIDATION FAILED:\n"
                f"{lines}\n"
                f"\nPlease fix the above issues in your .env or runtime.env file.\n"
                f"{'=' * 60}\n"
            )
            logging.critical("%s", _abort)
            raise SystemExit(1)

    def _parse_value(self, raw: str, target_type: type, param_name: str) -> Any:
        """Parse raw string value to target type."""
        if target_type is bool:
            return self._parse_bool(raw)
        elif target_type is float:
            try:
                return float(raw)
            except ValueError:
                raise ValidationError(param_name, raw, "cannot parse as float")
        elif target_type is int:
            try:
                return int(raw)
            except ValueError:
                raise ValidationError(param_name, raw, "cannot parse as int")
        elif target_type is str:
            return str(raw).strip()
        else:
            return raw

    def _parse_bool(self, raw: str) -> bool:
        """Parse boolean from string: 0/1, false/true, no/yes (case-insensitive)."""
        normalized = raw.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        elif normalized in ("0", "false", "no", "off", ""):
            return False
        else:
            raise ValidationError("unknown", raw, "not a valid boolean (expected 0/1, true/false, yes/no)")

    def _validate_range(self, value: Any, spec: ParameterSpec) -> None:
        """Check min/max constraints."""
        if spec.min is not None and value < spec.min:
            raise ValidationError(spec.name, value, f"must be >= {spec.min}")
        if spec.max is not None and value > spec.max:
            raise ValidationError(spec.name, value, f"must be <= {spec.max}")

    def _validate_choices(self, value: Any, spec: ParameterSpec) -> None:
        """Check enum choices."""
        if spec.choices and value not in spec.choices:
            choices_str = ", ".join(sorted(str(c) for c in spec.choices))
            raise ValidationError(
                spec.name,
                value,
                f"must be one of: {choices_str}",
            )

    def _validate_str_list(self, value: str, name: str) -> None:
        """Validate comma-separated string list has at least min_len items."""
        parts = [p.strip() for p in str(value).split(",") if p.strip()]
        min_len = getattr(self, "_str_list_min_lens", {}).get(name, 0)
        if len(parts) < min_len:
            raise ValidationError(
                name,
                value,
                f"must have at least {min_len} comma-separated values",
            )


# Singleton validator instance
_validator = ConfigValidator()


def validate_config() -> None:
    """Validate all configuration parameters from environment.

    This function should be called once during bot startup after environment
    variables are loaded but before any components that use them are initialized.

    This validates that all set parameters have valid values. Required parameters
    are checked by validate_required_config() separately.

    Raises:
        SystemExit: If validation fails (with clear error message).
    """
    _validator.validate()
