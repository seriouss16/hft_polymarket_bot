"""Signal, risk, and simulated execution for Polymarket latency strategy."""

import json
import logging
import os
import time
from collections import deque
from typing import Any

import numpy as np

from ml.indicators import (
    compute_ema_last,
    compute_macd_last,
    compute_reaction_score,
    compute_rsi,
    dynamic_rsi_bands,
)

from core.engine_entry_candidates import entry_candidate_from_state, entry_momentum_alt_signal
from core.engine_entry_gates import (
    anchor_gate,
    entry_aggressive_trend_age_ok,
    entry_ask_allows_open,
    entry_edge_jump_ok,
    entry_latency_allows_entry,
    entry_liquidity_spread_ok,
    entry_outcome_price_allows,
    entry_rsi_slope_allows,
    entry_skew_allows_entry,
    entry_slot_window_allows,
    entry_speed_acceleration_ok,
    entry_trend_flip_settled_ok,
    entry_zscore_trend_ok,
    latency_expiry_edge_multiplier,
    low_speed_edge_multiplier,
    max_entry_latency_ms_all_profiles,
    record_entry_samples,
    zscore_monotonic_for_direction,
)
from core.engine_price import price_array_for_rsi
from core.engine_rsi_exit import exit_rsi as clamp_exit_rsi
from core.engine_rsi_exit import rsi_range_exit_triggered, rsi_slope_per_tick
from core.engine_sizing import (
    calc_dynamic_amount,
    deposit_trade_notional,
    hold_met,
    pnl_target_and_stop_lines,
    position_notional_usd,
    reset_trailing_state,
    tier_dynamic_amount,
    trailing_sl_triggered,
    trailing_tp_triggered,
    update_trailing_state,
)
from core.engine_trend import dynamic_edge_threshold as compute_dynamic_edge_threshold
from core.engine_trend import update_trend as apply_update_trend

DEBUG_LOG_PATH = os.getenv("DEBUG_LOG_PATH")
DEBUG_SESSION_ID = os.getenv("DEBUG_SESSION_ID")
_DEBUG_LOG_ENABLED = os.getenv("HFT_DEBUG_LOG_ENABLED", "0") == "1"


def _append_debug_log(payload: dict) -> None:
    """Append one NDJSON line to active debug session file (gated by HFT_DEBUG_LOG_ENABLED)."""
    if not _DEBUG_LOG_ENABLED:
        return
    try:
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except OSError:
        pass


def _env_float_default(key: str, default: float) -> float:
    """Read float from env; use ``default`` when unset or empty. Explicit ``0`` is kept."""
    v = os.getenv(key)
    if v is None or not str(v).strip():
        return default
    return float(v)


# Default last-window no-entry: 1.3 min before 5m slot end (unstable near resolution).
_DEFAULT_NO_ENTRY_LAST_SEC = 1.3 * 60.0


def poly_book_outcome_quotes(poly_orderbook: dict) -> tuple[float, float, float, float, float, float]:
    """Return UP/DOWN bids, asks, and mids from a Polymarket binary book dict.

    Mirrors the outcome-side math in ``HFTEngine.process_tick`` so callers
    (including ``generate_live_signal``) stay aligned with entry_candidate_from_state
    extreme-price and edge_mult handling.
    """
    up_ask = float(poly_orderbook["ask"])
    up_bid = float(poly_orderbook["bid"])
    up_mid = (up_bid + up_ask) * 0.5
    down_bid_raw = float(poly_orderbook.get("down_bid", 0.0))
    down_ask_raw = float(poly_orderbook.get("down_ask", 0.0))
    if 0.0 < down_bid_raw < down_ask_raw <= 1.0:
        down_bid = down_bid_raw
        down_ask = down_ask_raw
    else:
        down_ask = max(0.01, min(0.99, 1.0 - up_bid))
        down_bid = max(0.01, min(0.99, 1.0 - up_ask))
    down_mid = (down_bid + down_ask) * 0.5
    return up_bid, up_ask, down_bid, down_ask, up_mid, down_mid


class HFTEngine:
    """Signal, risk, and simulated execution for Polymarket latency strategy.

    The same ``process_tick`` / ``execute`` path runs for SIM and LIVE. Whether
    balances and inventory update inside ``PnLTracker.log_trade`` is controlled
    only by ``PnLTracker.live_mode`` (LIVE suppresses BUY/SELL ledger writes so
    the main loop can apply fills from the CLOB). Strategy logic does not branch
    on SIM vs LIVE.
    """

    def __init__(self, pnl_tracker, strategy_label="latency_arbitrage"):
        self.pnl = pnl_tracker
        self._strategy_label = str(strategy_label)
        
        # --- Базовый Edge (в пунктах цены) ---
        self.noise_edge = float(os.getenv("HFT_NOISE_EDGE"))
        self.buy_edge = float(os.getenv("HFT_BUY_EDGE"))
        self.sell_edge = -float(os.getenv("HFT_SELL_EDGE_ABS"))
        
        # --- Тайминги и объемы ---
        self.cooldown = float(os.getenv("HFT_COOLDOWN_SEC"))
        self.last_trade_time = 0.0
        self.max_position = float(os.getenv("HFT_MAX_POSITION_USD"))
        self.trade_amount_usd = float(os.getenv("HFT_DEFAULT_TRADE_USD"))
        self.min_hold_sec = float(os.getenv("HFT_MIN_HOLD_SEC"))
        self.exit_on_opposite_trend = os.getenv("HFT_EXIT_ON_OPPOSITE_TREND") == "1"
        self.opposite_trend_exit_min_hold_sec = float(
            os.getenv("HFT_OPPOSITE_TREND_EXIT_MIN_HOLD_SEC")
        )
        self.opposite_trend_exit_min_abs_edge = float(
            os.getenv("HFT_OPPOSITE_TREND_EXIT_MIN_ABS_EDGE")
        )
        self.post_close_reentry_sec = float(os.getenv("HFT_POST_CLOSE_REENTRY_COOLDOWN_SEC"))
        self.last_close_time = 0.0
        self.reaction_timeout_sec = float(os.getenv("HFT_REACTION_TIMEOUT_SEC"))
        self.entry_poly_mid = None
        self.entry_outcome_mid = None
        self.entry_fast_price = None
        self.entry_time = 0.0
        self._live_entry_sync_pending = False

        # --- Тейки и Стопы (в пунктах и USD) ---
        self.poly_take_profit_move = float(os.getenv("HFT_POLY_TP_MOVE"))
        self.poly_stop_move = float(os.getenv("HFT_POLY_SL_MOVE"))
        self.target_profit_usd = float(os.getenv("HFT_TARGET_PROFIT_USD"))
        self.stop_loss_usd = float(os.getenv("HFT_STOP_LOSS_USD"))
        self.pnl_tp_pct = float(os.getenv("HFT_PNL_TP_PERCENT"))
        self.pnl_sl_pct = float(os.getenv("HFT_PNL_SL_PERCENT"))
        self.pnl_tp_min_hold_sec = float(os.getenv("HFT_PNL_TP_MIN_HOLD_SEC"))

        # --- Trailing TP/SL ---
        self.trailing_tp_enabled = os.getenv("HFT_TRAILING_TP_ENABLED") == "1"
        self.trailing_tp_activate_usd = float(os.getenv("HFT_TRAILING_TP_ACTIVATE_USD"))
        self.trailing_tp_pullback_pct = float(os.getenv("HFT_TRAILING_TP_PULLBACK_PCT"))
        self.trailing_tp_min_pullback_usd = float(os.getenv("HFT_TRAILING_TP_MIN_PULLBACK_USD"))
        self.trailing_sl_enabled = os.getenv("HFT_TRAILING_SL_ENABLED") == "1"
        self.trailing_sl_breakeven_at_usd = float(os.getenv("HFT_TRAILING_SL_BREAKEVEN_AT_USD"))
        self.trailing_sl_step_usd = float(os.getenv("HFT_TRAILING_SL_STEP_USD"))
        self.trailing_sl_step_lock_pct = float(os.getenv("HFT_TRAILING_SL_STEP_LOCK_PCT"))
        self._peak_unrealized = 0.0
        self._trailing_sl_floor = None

        # --- RSI логика ---
        self.rsi_period = 14
        self.rsi_price_len = int(os.getenv("HFT_RSI_PRICE_LEN"))
        self._last_rsi = 50.0
        self.rsi_entry_up_low = float(
            os.getenv("HFT_RSI_ENTRY_UP_LOW") or os.getenv("HFT_RSI_ENTRY_YES_LOW")
        )
        self.rsi_entry_up_high = float(
            os.getenv("HFT_RSI_ENTRY_UP_HIGH") or os.getenv("HFT_RSI_ENTRY_YES_HIGH")
        )
        self.rsi_entry_down_low = float(
            os.getenv("HFT_RSI_ENTRY_DOWN_LOW") or os.getenv("HFT_RSI_ENTRY_NO_LOW")
        )
        self.rsi_entry_down_high = float(
            os.getenv("HFT_RSI_ENTRY_DOWN_HIGH") or os.getenv("HFT_RSI_ENTRY_NO_HIGH")
        )
        
        # Выходы по RSI
        self.rsi_exit_upper_base = float(os.getenv("HFT_RSI_EXIT_UPPER_BASE"))
        self.rsi_exit_lower_base = float(os.getenv("HFT_RSI_EXIT_LOWER_BASE"))
        self.rsi_range_exit_min_profit_usd = float(os.getenv("HFT_RSI_RANGE_EXIT_MIN_PROFIT_USD"))
        self.rsi_range_exit_band_margin = float(os.getenv("HFT_RSI_RANGE_EXIT_BAND_MARGIN"))
        self.rsi_extreme_high = float(os.getenv("HFT_RSI_EXTREME_HIGH"))
        self.rsi_extreme_low = float(os.getenv("HFT_RSI_EXTREME_LOW"))
        self.rsi_band_vol_k = float(os.getenv("HFT_RSI_BAND_VOL_K"))
        self.rsi_range_exit_profit_frac = float(os.getenv("HFT_RSI_RANGE_EXIT_PROFIT_FRAC"))
        self.rsi_range_exit_min_hold_sec = float(os.getenv("HFT_RSI_RANGE_EXIT_MIN_HOLD_SEC"))
        # Extra RSI points beyond margin before fade exit (reduces sensitivity; not time-based).
        self.rsi_range_exit_fade_buffer = float(os.getenv("HFT_RSI_RANGE_EXIT_FADE_BUFFER", "0") or 0.0)
        self.rsi_exit_clamp_high = float(os.getenv("HFT_RSI_EXIT_CLAMP_HIGH"))
        self.rsi_exit_clamp_low = float(os.getenv("HFT_RSI_EXIT_CLAMP_LOW"))

        # --- RSI Slope (Наклон) ---
        self.rsi_slope_exit_enabled = os.getenv("HFT_RSI_SLOPE_EXIT_ENABLED") == "1"
        self.rsi_slope_up_exit = -2.0
        self.rsi_slope_down_exit = 2.0
        self._rsi_tick_history = deque(maxlen=10)
        self._last_rsi_upper = 70.0
        self._last_rsi_lower = 30.0
        self._last_rsi_slope = 0.0
        self._last_rsi_raw = 50.0
        self._last_ma_fast = 0.0
        self._last_macd_hist = 0.0
        self.reaction_score_enabled = os.getenv("HFT_REACTION_SCORE_ENABLED") == "1"
        self.reaction_ma_period = int(os.getenv("HFT_REACTION_MA_PERIOD"))
        self.reaction_macd_fast = int(os.getenv("HFT_REACTION_MACD_FAST"))
        self.reaction_macd_slow = int(os.getenv("HFT_REACTION_MACD_SLOW"))
        self.reaction_macd_signal = int(os.getenv("HFT_REACTION_MACD_SIGNAL"))
        self.reaction_ma_rel_scale = float(os.getenv("HFT_REACTION_MA_REL_SCALE"))
        self.reaction_macd_hist_scale = float(os.getenv("HFT_REACTION_MACD_HIST_SCALE"))
        self.reaction_w_rsi = float(os.getenv("HFT_REACTION_W_RSI"))
        self.reaction_w_ma = float(os.getenv("HFT_REACTION_W_MA"))
        self.reaction_w_macd = float(os.getenv("HFT_REACTION_W_MACD"))
        self.rsi_hold_up_floor = float(
            os.getenv("HFT_RSI_HOLD_UP_FLOOR") or os.getenv("HFT_RSI_HOLD_YES_FLOOR")
        )
        self.rsi_hold_down_ceiling = float(
            os.getenv("HFT_RSI_HOLD_DOWN_CEILING") or os.getenv("HFT_RSI_HOLD_NO_CEILING")
        )
        self.rsi_allow_bypass_on_strong_edge = os.getenv(
            "HFT_RSI_ALLOW_BYPASS_STRONG_EDGE", "0"
        ) == "1"
        self.rsi_allow_bypass_on_aggressive_edge = os.getenv(
            "HFT_RSI_ALLOW_BYPASS_AGGRESSIVE_EDGE", "1"
        ) == "1"
        self.aggressive_entry_relax_speed = float(os.getenv("HFT_AGGRESSIVE_ENTRY_RELAX_SPEED"))
        self.aggressive_entry_relax_speed_down = float(os.getenv("HFT_AGGRESSIVE_ENTRY_RELAX_SPEED_DOWN"))

        # --- Подтверждение входа (Entry Confirm) ---
        self.entry_confirm_age = float(os.getenv("HFT_ENTRY_CONFIRM_AGE_SEC"))
        self.reversal_confirm_age = float(os.getenv("HFT_REVERSAL_CONFIRM_AGE_SEC"))
        self.entry_extreme_min_edge = float(os.getenv("HFT_ENTRY_EXTREME_MIN_EDGE"))
        self.entry_extreme_price_low = float(os.getenv("HFT_ENTRY_EXTREME_PRICE_LOW"))
        self.entry_extreme_price_high = float(os.getenv("HFT_ENTRY_EXTREME_PRICE_HIGH"))
        self.entry_depth_mult = float(os.getenv("HFT_ENTRY_DEPTH_MULT"))
        self.entry_up_speed_min = float(os.getenv("HFT_ENTRY_UP_SPEED_MIN"))
        self.entry_down_speed_max = float(os.getenv("HFT_ENTRY_DOWN_SPEED_MAX"))

        # --- Скорость и Акселерация ---
        self.speed_floor = float(os.getenv("HFT_SPEED_FLOOR"))
        self.entry_accel_enabled = os.getenv("HFT_ENTRY_ACCEL_ENABLED") == "1"
        self.entry_accel_min = float(os.getenv("HFT_ENTRY_ACCEL_MIN"))
        self.reversal_speed_floor = float(os.getenv("HFT_REVERSAL_SPEED_FLOOR"))

        # --- Динамический объем (Risk Management) ---
        self.dynamic_risk_per_tick_usd = float(os.getenv("HFT_DYNAMIC_RISK_PER_TICK_USD"))
        self.dynamic_amount_min_usd = float(os.getenv("HFT_DYNAMIC_AMOUNT_MIN_USD"))
        self.dynamic_amount_max_usd = float(os.getenv("HFT_DYNAMIC_AMOUNT_MAX_USD"))
        self.dynamic_cheap_price_below = float(os.getenv("HFT_DYNAMIC_CHEAP_PRICE_BELOW"))
        self.dynamic_rich_price_above = float(os.getenv("HFT_DYNAMIC_RICH_PRICE_ABOVE"))
        self.dynamic_min_exec_price = float(os.getenv("HFT_DYNAMIC_MIN_EXEC_PRICE"))
        self.dynamic_floor_notional_usd = float(os.getenv("HFT_DYNAMIC_FLOOR_NOTIONAL_USD"))
        self.dynamic_amount_cheap_usd = float(os.getenv("HFT_DYNAMIC_AMOUNT_CHEAP_USD"))
        self.dynamic_amount_rich_usd = float(os.getenv("HFT_DYNAMIC_AMOUNT_RICH_USD"))

        self.deposit_usd = float(os.getenv("HFT_DEPOSIT_USD"))
        self.trade_pct_of_deposit = float(os.getenv("HFT_TRADE_PCT_OF_DEPOSIT"))
        self.fixed_trade_usd = float(os.getenv("HFT_DEFAULT_TRADE_USD"))

        # --- Стакан (Orderbook) и Ликвидность ---
        self.book_move_entry_min = float(os.getenv("HFT_BOOK_MOVE_ENTRY_MIN"))
        self.book_move_stop_max = float(os.getenv("HFT_BOOK_MOVE_STOP_MAX"))
        self.book_stall_ticks_limit = int(os.getenv("HFT_BOOK_STALL_TICKS"))
        self.max_entry_spread = float(os.getenv("HFT_MAX_ENTRY_SPREAD"))
        self.max_entry_ask = float(os.getenv("HFT_MAX_ENTRY_ASK"))
        self._prev_up_mid = None
        self._prev_down_mid = None
        self._book_stall_ticks = 0
        self.strong_edge_rsi_mult = float(os.getenv("HFT_STRONG_EDGE_RSI_MULT"))
        self.aggressive_edge_mult = float(os.getenv("HFT_AGGRESSIVE_EDGE_MULT"))
        self.entry_confirm_age_strong = float(os.getenv("HFT_ENTRY_CONFIRM_AGE_STRONG_SEC"))
        self.wide_spread_min_edge = float(os.getenv("HFT_WIDE_SPREAD_MIN_EDGE"))
        self.entry_liquidity_max_spread = float(os.getenv("HFT_ENTRY_LIQUIDITY_MAX_SPREAD"))
        # When trend is UP, multiply max spread limits (legacy + liquidity) so BUY_UP is not
        # blocked as often as BUY_DOWN. 1.0 = no change. Values < 1.0 are clamped to 1.0.
        self.spread_gate_up_relax_mult = max(
            1.0, float(os.getenv("HFT_SPREAD_GATE_UP_RELAX_MULT", "1.0"))
        )
        self.entry_momentum_alt_enabled = os.getenv("HFT_ENTRY_MOMENTUM_ALT_ENABLED") == "1"

        # --- Задержка (Latency Guard) ---
        self.entry_max_latency_ms = float(os.getenv("HFT_ENTRY_MAX_LATENCY_MS"))
        self.entry_max_skew_ms = float(os.getenv("HFT_ENTRY_MAX_SKEW_MS"))
        self.entry_min_ask_up_cap = float(os.getenv("HFT_ENTRY_MIN_ASK_UP"))
        self.entry_max_ask_up_cap = float(os.getenv("HFT_ENTRY_MAX_ASK_UP"))
        self.entry_min_ask_down_cap = float(os.getenv("HFT_ENTRY_MIN_ASK_DOWN"))
        self.entry_max_ask_down_cap = float(os.getenv("HFT_ENTRY_MAX_ASK_DOWN"))
        self.latency_high_ms = float(os.getenv("HFT_LATENCY_HIGH_MS"))
        self.latency_high_edge_mult = float(os.getenv("HFT_LATENCY_HIGH_EDGE_MULT"))
        self.expiry_tight_sec = float(os.getenv("HFT_EXPIRY_TIGHT_SEC"))
        self.expiry_edge_mult = float(os.getenv("HFT_EXPIRY_EDGE_MULT"))
        self.slot_interval_sec = float(os.getenv("HFT_SLOT_INTERVAL_SEC"))
        self.no_entry_first_sec = float(os.getenv("HFT_NO_ENTRY_FIRST_SEC"))
        self.no_entry_last_sec = _env_float_default(
            "HFT_NO_ENTRY_LAST_SEC", _DEFAULT_NO_ENTRY_LAST_SEC
        )
        self.slot_force_close_last_sec = float(os.getenv("HFT_SLOT_FORCE_CLOSE_LAST_SEC"))
        self.slot_99c_max_sec = float(os.getenv("HFT_SLOT_99C_MAX_SEC"))
        self.slot_expiry_info_max_sec = float(os.getenv("HFT_SLOT_EXPIRY_INFO_MAX_SEC"))
        self.trend_flip_min_age_sec = float(os.getenv("HFT_TREND_FLIP_MIN_AGE_SEC"))
        self._load_rsi_slope_entry_params()
        self.entry_low_speed_abs = float(os.getenv("HFT_ENTRY_LOW_SPEED_ABS"))
        self.entry_low_speed_edge_mult = float(os.getenv("HFT_ENTRY_LOW_SPEED_EDGE_MULT"))

        # --- Z-Score (Статистический вход) ---
        self.entry_zscore_trend_enabled = os.getenv("HFT_ENTRY_ZSCORE_TREND_ENABLED") == "1"
        self.entry_zscore_strict_ticks = int(os.getenv("HFT_ENTRY_ZSCORE_STRICT_TICKS"))

        # --- Anti-spoof: reject one-tick CEX spikes vs lagging Poly oracle. ---
        self.entry_max_edge_jump_pts = float(os.getenv("HFT_ENTRY_MAX_EDGE_JUMP_PTS"))
        self.entry_edge_jump_bypass_abs_speed = float(os.getenv("HFT_ENTRY_EDGE_JUMP_BYPASS_ABS_SPEED"))
        self.entry_zscore_bypass_abs_speed = float(os.getenv("HFT_ENTRY_ZSCORE_BYPASS_ABS_SPEED"))
        self.entry_aggressive_min_trend_age_sec = float(os.getenv("HFT_AGGRESSIVE_MIN_TREND_AGE_SEC"))

        # --- Вспомогательные состояния ---
        self.soft_exits_enabled = True
        self.no_entry_guards = os.getenv("HFT_NO_ENTRY_GUARDS") == "1"
        self.edge_window = deque(maxlen=120)
        self.last_edge_sign = 0
        self.trend_dir = "FLAT"
        self.trend_since_ts = 0.0
        self.trend_depth = 0.0
        self._speed_samples = deque(maxlen=12)
        self._zscore_samples = deque(maxlen=12)
        self.position_trend = "FLAT"
        self.entry_context = {}
        self._last_entry_noise_log_ts = 0.0
        self._last_regime_skip_log_ts = 0.0
        self._last_slot_expiry_info_log_ts = 0.0
        self._last_feed_gate_log_ts = 0.0
        self._last_entry_cap_deny_log_ts = 0.0
        self.filter_diag_log_sec = float(os.getenv("HFT_FILTER_DIAG_LOG_SEC"))
        self._last_filter_diag_log_ts = time.time()
        self._filter_diag_stats: dict[str, int] = {}
        self._init_entry_profiles()

    _PROFILE_ATTRS = (
        "noise_edge",
        "buy_edge",
        "sell_edge",
        "entry_confirm_age",
        "reversal_confirm_age",
        "entry_confirm_age_strong",
        "strong_edge_rsi_mult",
        "aggressive_edge_mult",
        "entry_momentum_alt_enabled",
        "entry_max_edge_jump_pts",
        "entry_max_latency_ms",
        "speed_floor",
        "entry_low_speed_abs",
        "entry_low_speed_edge_mult",
        "entry_aggressive_min_trend_age_sec",
        "rsi_allow_bypass_on_aggressive_edge",
    )

    def _capture_profile_tuple(self) -> dict[str, float | bool]:
        """Return a copy of entry-related parameters for profile switching."""
        out: dict[str, float | bool] = {}
        for name in self._PROFILE_ATTRS:
            out[name] = getattr(self, name)
        return out

    def _build_soft_flow_profile(self, latency_base: dict[str, float | bool]) -> dict[str, float | bool]:
        """Build soft-flow profile from HFT_SOFT_* env with calmer defaults than latency."""
        sell_abs = os.getenv("HFT_SELL_EDGE_ABS")
        soft: dict[str, float | bool] = dict(latency_base)
        soft["noise_edge"] = float(os.getenv("HFT_SOFT_NOISE_EDGE"))
        soft["buy_edge"] = float(os.getenv("HFT_SOFT_BUY_EDGE"))
        soft["sell_edge"] = -float(os.getenv("HFT_SOFT_SELL_EDGE_ABS") or sell_abs)
        soft["entry_confirm_age"] = float(os.getenv("HFT_SOFT_ENTRY_CONFIRM_AGE_SEC"))
        soft["reversal_confirm_age"] = float(
            os.getenv("HFT_SOFT_REVERSAL_CONFIRM_AGE_SEC") or str(latency_base["reversal_confirm_age"])
        )
        soft["entry_confirm_age_strong"] = float(os.getenv("HFT_SOFT_ENTRY_CONFIRM_AGE_STRONG_SEC"))
        soft["strong_edge_rsi_mult"] = float(os.getenv("HFT_SOFT_STRONG_EDGE_RSI_MULT"))
        soft["aggressive_edge_mult"] = float(os.getenv("HFT_SOFT_AGGRESSIVE_EDGE_MULT"))
        soft["entry_momentum_alt_enabled"] = (
            os.getenv("HFT_SOFT_ENTRY_MOMENTUM_ALT_ENABLED") == "1"
        )
        soft["entry_max_edge_jump_pts"] = float(os.getenv("HFT_SOFT_ENTRY_MAX_EDGE_JUMP_PTS"))
        soft["entry_max_latency_ms"] = float(os.getenv("HFT_SOFT_ENTRY_MAX_LATENCY_MS"))
        soft["speed_floor"] = float(os.getenv("HFT_SOFT_SPEED_FLOOR") or str(latency_base["speed_floor"]))
        soft["entry_low_speed_abs"] = float(os.getenv("HFT_SOFT_ENTRY_LOW_SPEED_ABS"))
        soft["entry_low_speed_edge_mult"] = float(os.getenv("HFT_SOFT_ENTRY_LOW_SPEED_EDGE_MULT"))
        soft["entry_aggressive_min_trend_age_sec"] = float(os.getenv("HFT_SOFT_ENTRY_AGGRESSIVE_MIN_TREND_AGE_SEC"))
        soft["rsi_allow_bypass_on_aggressive_edge"] = (
            os.getenv("HFT_SOFT_RSI_ALLOW_BYPASS_AGGRESSIVE_EDGE") == "1"
        )
        return soft

    def _init_entry_profiles(self) -> None:
        """Snapshot latency parameters and build soft-flow profile for apply_profile()."""
        latency = self._capture_profile_tuple()
        self._profile_snapshots = {
            "latency": latency,
            "soft_flow": self._build_soft_flow_profile(latency),
        }
        self._active_profile = "latency"

    def _diag_inc(self, key: str, n: int = 1) -> None:
        """Increment in-memory diagnostic counter by key."""
        self._filter_diag_stats[key] = int(self._filter_diag_stats.get(key, 0)) + int(n)

    def _emit_filter_diag_if_due(self, now_ts: float | None = None) -> None:
        """Log aggregated filter diagnostics with configured threshold values."""
        if self.filter_diag_log_sec <= 0.0:
            return
        now_val = float(now_ts if now_ts is not None else time.time())
        if now_val - self._last_filter_diag_log_ts < self.filter_diag_log_sec:
            return
        st = self._filter_diag_stats
        logging.info(
            "FilterDiag stats: ticks=%s entry_checks=%s entry_no_signal=%s "
            "entry_block_regime=%s entry_block_spread_gate=%s entry_block_slot=%s "
            "entry_block_meta=%s entry_block_can_trade=%s entry_block_ask_cap=%s "
            "entry_block_rsi=%s entry_block_book=%s entry_open_ok=%s entry_open_no_fill=%s "
            "exit_checks=%s exit_close=%s exit_hold=%s exit_reason_flip=%s "
            "exit_reason_tp=%s exit_reason_stop=%s exit_reason_rsi=%s exit_reason_pnl_sl=%s",
            st.get("ticks", 0),
            st.get("entry_checks", 0),
            st.get("entry_no_signal", 0),
            st.get("entry_block_regime", 0),
            st.get("entry_block_spread_gate", 0),
            st.get("entry_block_slot", 0),
            st.get("entry_block_meta", 0),
            st.get("entry_block_can_trade", 0),
            st.get("entry_block_ask_cap", 0),
            st.get("entry_block_rsi", 0),
            st.get("entry_block_book", 0),
            st.get("entry_open_ok", 0),
            st.get("entry_open_no_fill", 0),
            st.get("exit_checks", 0),
            st.get("exit_close", 0),
            st.get("exit_hold", 0),
            st.get("exit_reason_trend_flip", 0),
            st.get("exit_reason_reaction_tp", 0),
            st.get("exit_reason_reaction_stop", 0),
            st.get("exit_reason_rsi_range", 0),
            st.get("exit_reason_pnl_sl", 0),
        )
        logging.info(
            "FilterDiag params: profile=%s spread_max=%.4f liq_spread_max=%.4f spread_gate_up_relax=%.2f "
            "max_entry_ask=%.4f "
            "ask_up=[%.3f,%.3f] ask_down=[%.3f,%.3f] max_latency_ms=%.0f max_skew_ms=%.0f "
            "trend_flip_min_age=%.2f strong_jump_min_age=%s no_entry_guards=%s cooldown=%.2fs "
            "post_close_reentry=%.2fs min_hold=%.2fs.",
            self.get_active_profile(),
            self.max_entry_spread,
            self.entry_liquidity_max_spread,
            self.spread_gate_up_relax_mult,
            self.max_entry_ask,
            self.entry_min_ask_up_cap,
            self.entry_max_ask_up_cap,
            self.entry_min_ask_down_cap,
            self.entry_max_ask_down_cap,
            self.entry_max_latency_ms,
            self.entry_max_skew_ms,
            self.trend_flip_min_age_sec,
            os.getenv("HFT_STRONG_JUMP_MIN_TREND_AGE_SEC"),
            int(self.no_entry_guards),
            self.cooldown,
            self.post_close_reentry_sec,
            self.min_hold_sec,
        )
        self._filter_diag_stats = {}
        self._last_filter_diag_log_ts = now_val

    def apply_profile(self, name: str) -> None:
        """Apply a named parameter snapshot (latency or soft_flow) to this engine."""
        snap = self._profile_snapshots.get(name)
        if not snap:
            return
        for key, val in snap.items():
            setattr(self, key, val)
        self._active_profile = str(name)

    def get_active_profile(self) -> str:
        """Return the last applied entry profile name."""
        return str(self._active_profile)

    def _regime_allows_new_entries(self) -> bool:
        """Return True when rolling PnL regime allows new entries (optional soft_flow bypass)."""
        if os.getenv("HFT_REGIME_FILTER_ENABLED") == "0":
            return True
        if (
            os.getenv("HFT_REGIME_BYPASS_SOFT_FLOW") == "1"
            and self.get_active_profile() == "soft_flow"
        ):
            return True
        return bool(self.pnl.is_good_regime())

    def _display_strategy_name(self) -> str:
        """Return log label for who initiated the trade: phase_router -> latency or soft."""
        if self._strategy_label != "phase_router":
            return self._strategy_label
        return "soft" if self.get_active_profile() == "soft_flow" else "latency"

    def _strategy_name_for_sim_log(self, side: str) -> str:
        """SIM log tag: open uses current profile label; close uses entry attribution."""
        if side == "SELL" and self.entry_context:
            sn = self.entry_context.get("strategy_name")
            if sn:
                return str(sn)
        return self._display_strategy_name()

    def max_entry_latency_ms_all_profiles(self) -> float:
        """Return the largest entry_max_latency_ms across profiles for feed warnings."""
        return max_entry_latency_ms_all_profiles(self._profile_snapshots)

    def _entry_ask_allows_open(self, ask_px: float) -> bool:
        """Return False when best ask is at or above max entry price (no buys at 99¢+)."""
        return entry_ask_allows_open(ask_px, self.max_entry_ask)

    def _entry_outcome_price_allows(self, side: str, up_ask: float, down_ask: float) -> bool:
        """Return True only when outcome ask is inside configured entry bounds."""
        return entry_outcome_price_allows(
            side,
            up_ask,
            down_ask,
            entry_min_ask_up_cap=self.entry_min_ask_up_cap,
            entry_max_ask_up_cap=self.entry_max_ask_up_cap,
            entry_min_ask_down_cap=self.entry_min_ask_down_cap,
            entry_max_ask_down_cap=self.entry_max_ask_down_cap,
        )

    def _hold_met(self, hold_sec: float) -> bool:
        """Return True when min-hold delay does not apply or is satisfied."""
        return hold_met(self.min_hold_sec, hold_sec)

    def _update_trailing_state(self, unrealized: float) -> None:
        """Track peak unrealized PnL and ratchet the trailing SL floor upward."""
        update_trailing_state(self, unrealized)

    def _trailing_tp_triggered(self, unrealized: float, hold_sec: float) -> bool:
        """Return True when profit has pulled back from peak beyond the trailing threshold."""
        return trailing_tp_triggered(self, unrealized, hold_sec)

    def _trailing_sl_triggered(self, unrealized: float, hold_sec: float) -> bool:
        """Return True when unrealized PnL drops below the ratcheted trailing SL floor.

        Floor is always >= 0: at breakeven_at activation it equals 0 (breakeven),
        then ratchets up by step_usd * lock_pct for each additional step_usd of peak profit.
        """
        return trailing_sl_triggered(self, unrealized, hold_sec)

    def _reset_trailing_state(self) -> None:
        """Clear trailing tracking on position close or new entry."""
        reset_trailing_state(self)

    def _snapshot_orderbook_mids(self, poly_orderbook: dict[str, Any]) -> tuple[float, float, float]:
        """Return poly oracle mid, UP outcome mid, and DOWN outcome mid from one book snapshot."""
        poly_mid = float(
            poly_orderbook.get("btc_oracle")
            or poly_orderbook.get("mid", 0.0)
            or 0.0
        )
        up_ask = float(poly_orderbook["ask"])
        up_bid = float(poly_orderbook["bid"])
        up_mid = (up_bid + up_ask) * 0.5
        down_bid_raw = float(poly_orderbook.get("down_bid", 0.0))
        down_ask_raw = float(poly_orderbook.get("down_ask", 0.0))
        if 0.0 < down_bid_raw < down_ask_raw <= 1.0:
            down_bid = down_bid_raw
            down_ask = down_ask_raw
        else:
            down_ask = max(0.01, min(0.99, 1.0 - up_bid))
            down_bid = max(0.01, min(0.99, 1.0 - up_ask))
        down_mid = (down_bid + down_ask) * 0.5
        return poly_mid, up_mid, down_mid

    def apply_live_entry_after_fill(
        self,
        poly_orderbook: dict[str, Any],
        fast_price: float,
        book_px: float,
        exec_px: float,
        shares_filled: float,
        cost_usd: float,
    ) -> None:
        """Set entry clock and midpoint baselines after a confirmed live BUY fill.

        When log_trade is suppressed, OPEN is emitted before the CLOB confirms;
        without this call, entry_time would include order latency and exit timing
        would diverge from paper mode.
        """
        if not self._live_entry_sync_pending:
            return
        pos = self.pnl.position_side or "UP"
        poly_mid, up_mid, down_mid = self._snapshot_orderbook_mids(poly_orderbook)
        outcome_mid = down_mid if pos == "DOWN" else up_mid
        _ts = time.time()
        self.entry_time = _ts
        self.last_trade_time = _ts
        self.entry_poly_mid = poly_mid
        self.entry_outcome_mid = outcome_mid
        self.entry_fast_price = fast_price
        self._live_entry_sync_pending = False
        self._reset_trailing_state()
        self.entry_context["entry_book_px"] = float(book_px)
        self.entry_context["entry_exec_px"] = float(exec_px)
        self.entry_context["shares_bought"] = float(shares_filled)
        self.entry_context["cost_usd"] = float(cost_usd)
        self.entry_context["entry_up_bid"] = float(poly_orderbook.get("bid", 0.0))
        self.entry_context["entry_up_ask"] = float(poly_orderbook.get("ask", 0.0))
        self.entry_context["entry_down_bid"] = float(poly_orderbook.get("down_bid", 0.0))
        self.entry_context["entry_down_ask"] = float(poly_orderbook.get("down_ask", 0.0))

    def rollback_live_open_signal(self) -> None:
        """Clear deferred live OPEN state when the CLOB BUY did not fill."""
        if not self._live_entry_sync_pending:
            return
        self._live_entry_sync_pending = False
        self.entry_time = 0.0
        self.entry_poly_mid = None
        self.entry_outcome_mid = None
        self.entry_fast_price = None
        self.entry_context = {}
        self.position_trend = "FLAT"
        self._reset_trailing_state()

    def _opposite_trend_exit_triggered(
        self,
        pos_side: str,
        trend_name: str,
        hold_sec: float,
        abs_edge: float,
    ) -> bool:
        """Return True when fast-vs-poly trend opposes the held outcome and exit is allowed."""
        if not self.exit_on_opposite_trend:
            return False
        if trend_name == "FLAT":
            return False
        opposite = (pos_side == "DOWN" and trend_name == "UP") or (
            pos_side == "UP" and trend_name == "DOWN"
        )
        if not opposite:
            return False
        if self.opposite_trend_exit_min_hold_sec > 0.0 and hold_sec < self.opposite_trend_exit_min_hold_sec:
            return False
        if self.opposite_trend_exit_min_abs_edge > 0.0 and abs_edge < self.opposite_trend_exit_min_abs_edge:
            return False
        return True

    def _deposit_trade_notional(self) -> float:
        """Return target trade USD based on current live balance and sizing mode.

        When HFT_TRADE_PCT_OF_DEPOSIT = 0: fixed HFT_DEFAULT_TRADE_USD, capped
        by current balance (size shrinks if balance falls below the fixed step).

        When HFT_TRADE_PCT_OF_DEPOSIT > 0: profit-scaling mode.
          size = fixed_step + pct% of profit above starting deposit
          - Balance at or below start → size = fixed_step (or balance if lower).
          - Balance above start → bonus = (balance - deposit_usd) * pct / 100.
          - Total is always capped by current balance.

        Example: deposit=2, step=2, pct=10.
          balance=2.00 → 2.00 + 0.00 = 2.00
          balance=2.20 → 2.00 + 0.02 = 2.02
          balance=3.00 → 2.00 + 0.10 = 2.10
          balance=1.80 → min(2.00, 1.80) = 1.80
        """
        return deposit_trade_notional(
            self.pnl,
            self.deposit_usd,
            self.fixed_trade_usd,
            self.trade_pct_of_deposit,
        )

    def _tier_dynamic_amount(self, exec_price: float) -> float:
        """Compute notional from price tier and risk-per-tick before deposit cap."""
        return tier_dynamic_amount(
            exec_price,
            dynamic_min_exec_price=self.dynamic_min_exec_price,
            dynamic_floor_notional_usd=self.dynamic_floor_notional_usd,
            dynamic_cheap_price_below=self.dynamic_cheap_price_below,
            dynamic_rich_price_above=self.dynamic_rich_price_above,
            dynamic_amount_min_usd=self.dynamic_amount_min_usd,
            dynamic_amount_max_usd=self.dynamic_amount_max_usd,
            dynamic_amount_cheap_usd=self.dynamic_amount_cheap_usd,
            dynamic_amount_rich_usd=self.dynamic_amount_rich_usd,
            dynamic_risk_per_tick_usd=self.dynamic_risk_per_tick_usd,
        )

    def _calc_dynamic_amount(self, exec_price: float) -> float:
        """Size notional USD: tier estimate capped by deposit rules and dynamic min/max."""
        return calc_dynamic_amount(
            exec_price,
            self.pnl,
            deposit_usd=self.deposit_usd,
            fixed_trade_usd=self.fixed_trade_usd,
            trade_pct_of_deposit=self.trade_pct_of_deposit,
            dynamic_amount_max_usd=self.dynamic_amount_max_usd,
            dynamic_amount_min_usd=self.dynamic_amount_min_usd,
            dynamic_min_exec_price=self.dynamic_min_exec_price,
            dynamic_floor_notional_usd=self.dynamic_floor_notional_usd,
            dynamic_cheap_price_below=self.dynamic_cheap_price_below,
            dynamic_rich_price_above=self.dynamic_rich_price_above,
            dynamic_amount_cheap_usd=self.dynamic_amount_cheap_usd,
            dynamic_amount_rich_usd=self.dynamic_amount_rich_usd,
            dynamic_risk_per_tick_usd=self.dynamic_risk_per_tick_usd,
        )

    def get_rsi_v5_state(self):
        """Return RSI (or blended reaction score), bands, slope, and MA/MACD extras."""
        return {
            "rsi": self._last_rsi,
            "rsi_raw": self._last_rsi_raw,
            "upper": self._last_rsi_upper,
            "lower": self._last_rsi_lower,
            "slope": self._last_rsi_slope,
            "ma_fast": self._last_ma_fast,
            "macd_hist": self._last_macd_hist,
            "reaction_on": 1.0 if self.reaction_score_enabled else 0.0,
        }

    def _rsi_slope_per_tick(self):
        """Approximate RSI slope over the last few engine ticks."""
        return rsi_slope_per_tick(self._rsi_tick_history)

    def _exit_rsi(self, rsi: float) -> float:
        """Clamp RSI for exit logic to limit spurious 100/0 from short price history."""
        return clamp_exit_rsi(rsi, self.rsi_exit_clamp_high, self.rsi_exit_clamp_low)

    def _rsi_range_exit_triggered(
        self, position_side, current_rsi, unrealized, hold_sec: float = 0.0
    ):
        """Return True when RSI band exit is allowed (take-profit at band or fade exit past margin).

        Fade exits (RSI past band against the position) respect ``rsi_range_exit_min_hold_sec``
        to avoid immediate churn when RSI spikes on a short lookback. TP-at-band exits are unchanged.
        """
        return rsi_range_exit_triggered(
            self, position_side, current_rsi, unrealized, hold_sec
        )

    def can_trade(self):
        """Return True when risk limits allow new trade."""
        usd_exposure = abs(self.pnl.inventory * self.pnl.entry_price) if self.pnl.inventory > 0 else 0.0
        return usd_exposure < self.max_position

    def update_trend(self, fast_price, poly_mid):
        """Track crossing of target price and estimate trend speed/depth."""
        return apply_update_trend(self, fast_price, poly_mid)

    def dynamic_edge_threshold(self, price_history, recent_pnl=0.0, latency_ms=0.0, extra_mult=1.0):
        """Return adaptive edge threshold in price units from recent volatility."""
        return compute_dynamic_edge_threshold(
            self, price_history, recent_pnl, latency_ms, extra_mult
        )

    def _load_rsi_slope_entry_params(self) -> None:
        """Read RSI slope entry gates from env (see entry_rsi_slope_allows). Single call site."""
        self.entry_rsi_slope_filter_enabled = (
            os.getenv("HFT_ENTRY_RSI_SLOPE_FILTER_ENABLED") == "1"
        )
        self.rsi_up_entry_max = float(os.getenv("HFT_RSI_UP_ENTRY_MAX"))
        self.rsi_up_slope_min = float(os.getenv("HFT_RSI_UP_SLOPE_MIN"))
        self.rsi_down_entry_min = float(os.getenv("HFT_RSI_DOWN_ENTRY_MIN"))
        self.rsi_down_slope_max = float(os.getenv("HFT_RSI_DOWN_SLOPE_MAX"))

    def reload_profile_params(self) -> None:
        """Re-read session-profile-controlled env-vars into cached attributes.

        Call this after session_profile.apply_profile() switches NIGHT/DAWN/DAY
        so that the running engine immediately picks up the new thresholds without
        requiring a full restart.
        """
        self.entry_up_speed_min = float(os.getenv("HFT_ENTRY_UP_SPEED_MIN"))
        self.entry_down_speed_max = float(os.getenv("HFT_ENTRY_DOWN_SPEED_MAX"))
        self.entry_max_latency_ms = float(os.getenv("HFT_ENTRY_MAX_LATENCY_MS"))
        self.entry_zscore_trend_enabled = os.getenv("HFT_ENTRY_ZSCORE_TREND_ENABLED") == "1"
        self.entry_zscore_strict_ticks = int(os.getenv("HFT_ENTRY_ZSCORE_STRICT_TICKS"))
        self.entry_low_speed_edge_mult = float(os.getenv("HFT_ENTRY_LOW_SPEED_EDGE_MULT"))
        self.entry_low_speed_abs = float(os.getenv("HFT_ENTRY_LOW_SPEED_ABS"))
        self.no_entry_first_sec = float(os.getenv("HFT_NO_ENTRY_FIRST_SEC"))
        self.no_entry_last_sec = _env_float_default(
            "HFT_NO_ENTRY_LAST_SEC", _DEFAULT_NO_ENTRY_LAST_SEC
        )
        self.slot_force_close_last_sec = float(os.getenv("HFT_SLOT_FORCE_CLOSE_LAST_SEC"))
        self.slot_99c_max_sec = float(os.getenv("HFT_SLOT_99C_MAX_SEC"))
        self.slot_expiry_info_max_sec = float(os.getenv("HFT_SLOT_EXPIRY_INFO_MAX_SEC"))
        self.speed_floor = float(os.getenv("HFT_SPEED_FLOOR"))
        self.reaction_score_enabled = os.getenv("HFT_REACTION_SCORE_ENABLED") == "1"
        self.reaction_ma_period = int(os.getenv("HFT_REACTION_MA_PERIOD"))
        self.reaction_macd_fast = int(os.getenv("HFT_REACTION_MACD_FAST"))
        self.reaction_macd_slow = int(os.getenv("HFT_REACTION_MACD_SLOW"))
        self.reaction_macd_signal = int(os.getenv("HFT_REACTION_MACD_SIGNAL"))
        self.reaction_ma_rel_scale = float(os.getenv("HFT_REACTION_MA_REL_SCALE"))
        self.reaction_macd_hist_scale = float(os.getenv("HFT_REACTION_MACD_HIST_SCALE"))
        self.reaction_w_rsi = float(os.getenv("HFT_REACTION_W_RSI"))
        self.reaction_w_ma = float(os.getenv("HFT_REACTION_W_MA"))
        self.reaction_w_macd = float(os.getenv("HFT_REACTION_W_MACD"))
        self._load_rsi_slope_entry_params()
        self.poly_take_profit_move = float(os.getenv("HFT_POLY_TP_MOVE"))
        self.poly_stop_move = float(os.getenv("HFT_POLY_SL_MOVE"))
        self.min_hold_sec = float(os.getenv("HFT_MIN_HOLD_SEC"))
        logging.info(
            "[ENGINE] Profile params reloaded: speed_min=%.2f speed_max=%.2f "
            "latency=%.0fms zscore=%s low_speed_mult=%.2f reaction=%s "
            "rsi_up_max=%.1f rsi_down_min=%.1f slope_up=%.2f slope_dn=%.2f",
            self.entry_up_speed_min,
            self.entry_down_speed_max,
            self.entry_max_latency_ms,
            self.entry_zscore_trend_enabled,
            self.entry_low_speed_edge_mult,
            "on" if self.reaction_score_enabled else "off",
            self.rsi_up_entry_max,
            self.rsi_down_entry_min,
            self.rsi_up_slope_min,
            self.rsi_down_slope_max,
        )

    def reset_for_new_market(self):
        """Clear trend/book memory when switching Polymarket token or slot."""
        self.edge_window.clear()
        self.last_edge_sign = 0
        self.trend_dir = "FLAT"
        self.trend_since_ts = 0.0
        self.trend_depth = 0.0
        self._prev_up_mid = None
        self._prev_down_mid = None
        self._book_stall_ticks = 0
        self._speed_samples.clear()
        self._zscore_samples.clear()
        self._last_regime_skip_log_ts = 0.0
        self._last_slot_expiry_info_log_ts = 0.0
        self._last_feed_gate_log_ts = 0.0
        self._last_entry_cap_deny_log_ts = 0.0
        self._filter_diag_stats = {}
        self._last_filter_diag_log_ts = time.time()
        self.last_close_time = 0.0
        self._live_entry_sync_pending = False
        self.apply_profile("latency")

    def _position_notional_usd(self):
        """Return absolute position notional in USD for percent-based TP/SL."""
        return position_notional_usd(self.pnl)

    def _pnl_target_and_stop_lines(self):
        """Return (take_profit_usd, stop_loss_usd) thresholds from percent or fixed env."""
        return pnl_target_and_stop_lines(
            self.pnl,
            pnl_tp_pct=self.pnl_tp_pct,
            target_profit_usd=self.target_profit_usd,
            pnl_sl_pct=self.pnl_sl_pct,
            stop_loss_usd=self.stop_loss_usd,
        )

    def _is_strong_oracle_edge(self, edge: float) -> bool:
        """Return True when abs(fast-oracle edge) exceeds buy_edge * strong multiplier."""
        return abs(edge) >= self.buy_edge * self.strong_edge_rsi_mult

    def _is_aggressive_oracle_edge(self, edge: float) -> bool:
        """Return True for very large edge; used for logging and relaxed confirm age."""
        return abs(edge) >= self.buy_edge * self.aggressive_edge_mult

    def _latency_expiry_edge_multiplier(self, latency_ms: float, seconds_to_expiry: float | None) -> float:
        """Raise required edge when feed staleness_ms is high or the market slot is near expiry."""
        return latency_expiry_edge_multiplier(self, latency_ms, seconds_to_expiry)

    def _entry_slot_window_allows(self, seconds_to_expiry: float | None) -> bool:
        """Allow entries only outside first and last slot guard windows.

        When ``seconds_to_expiry`` is within ``no_entry_last_sec`` of the slot
        end (default 78 s ≈ 1.3 min on a 5m slot), entries are blocked — near
        resolution the book is often unstable. At startup, if the bot is
        started in this window, no new entries occur until the next slot
        (effectively skipping the remainder of that slot).
        """
        return entry_slot_window_allows(self, seconds_to_expiry)

    async def _close_position_slot_edge(
        self,
        reason: str,
        exit_price: float,
        hold_sec: float,
        fast_price: float,
        poly_mid: float,
        pos_side: str,
        up_bid: float,
        up_ask: float,
        down_bid: float,
        down_ask: float,
    ) -> dict:
        """Execute SELL for slot-boundary exits and return CLOSE payload after state reset."""
        close_event = await self.execute("SELL", exit_price)
        ce = close_event or {}
        _pk = ce.get("performance_key")
        result = {
            "event": "CLOSE",
            "reason": reason,
            "entry_edge": self.entry_context.get("entry_edge", 0.0),
            "exit_edge": fast_price - poly_mid,
            "duration_sec": hold_sec,
            "entry_trend": self.entry_context.get("entry_trend", "FLAT"),
            "entry_speed": self.entry_context.get("entry_speed", 0.0),
            "entry_depth": self.entry_context.get("entry_depth", 0.0),
            "entry_imbalance": self.entry_context.get("entry_imbalance", 0.0),
            "latency_ms": self.entry_context.get("latency_ms", 0.0),
            "pnl": float(ce.get("pnl") or 0.0),
            "side": pos_side,
            "strategy_name": self.entry_context.get("strategy_name"),
            "entry_profile": self.entry_context.get("entry_profile"),
            "performance_key": _pk,
            "entry_book_px": self.entry_context.get("entry_book_px", 0.0),
            "entry_exec_px": self.entry_context.get("entry_exec_px", 0.0),
            "exit_book_px": float(ce.get("book_px") or exit_price),
            "exit_exec_px": float(ce.get("exec_px") or 0.0),
            "shares_bought": self.entry_context.get("shares_bought", 0.0),
            "shares_sold": float(ce.get("shares_sold") or 0.0),
            "cost_usd": self.entry_context.get("cost_usd", 0.0),
            "proceeds_usd": float(ce.get("proceeds_usd") or 0.0),
            "cost_basis_usd": float(ce.get("cost_basis_usd") or 0.0),
            "entry_up_bid": self.entry_context.get("entry_up_bid"),
            "entry_up_ask": self.entry_context.get("entry_up_ask"),
            "entry_down_bid": self.entry_context.get("entry_down_bid"),
            "entry_down_ask": self.entry_context.get("entry_down_ask"),
            "exit_up_bid": up_bid,
            "exit_up_ask": up_ask,
            "exit_down_bid": down_bid,
            "exit_down_ask": down_ask,
        }
        self.entry_poly_mid = None
        self.entry_outcome_mid = None
        self.entry_fast_price = None
        self.entry_time = 0.0
        self.position_trend = "FLAT"
        self.entry_context = {}
        self._book_stall_ticks = 0
        self._prev_up_mid = None
        self._prev_down_mid = None
        self.last_close_time = time.time()
        self._reset_trailing_state()
        return result

    def _anchor_gate(self, fast_price: float, slot_anchor_price: float) -> tuple[bool, bool]:
        """Return (up_allowed, down_allowed) based on slot anchor price filter.

        When HFT_ANCHOR_FILTER_ENABLED=1 and a valid anchor is present, entries
        counter to the anchor direction require the price to have moved at least
        HFT_ANCHOR_COUNTER_MIN_DELTA_PCT away from the anchor before they are
        allowed.  Trades aligned with the anchor direction are always allowed.
        When the filter is disabled or anchor is unknown, both gates are True.
        """
        return anchor_gate(fast_price, slot_anchor_price)

    def _low_speed_edge_multiplier(self, speed: float) -> float:
        """Raise required oracle edge when edge speed is low (fade / chop risk)."""
        return low_speed_edge_multiplier(
            speed, self.entry_low_speed_abs, self.entry_low_speed_edge_mult
        )

    def entry_latency_allows_entry(self, latency_ms: float) -> bool:
        """Block entries when max feed staleness_ms exceeds entry_max_latency_ms."""
        return entry_latency_allows_entry(self.entry_max_latency_ms, latency_ms)

    def entry_skew_allows_entry(self, skew_ms: float) -> bool:
        """Block entries when cross-feed skew is larger than the limit (0 disables the gate)."""
        return entry_skew_allows_entry(self.entry_max_skew_ms, skew_ms)

    def entry_edge_jump_ok(self, edge_now: float, edge_speed: float = 0.0) -> bool:
        """Return False when oracle edge jumps too far in one tick (bad CEX print vs Poly).

        When ``HFT_ENTRY_EDGE_JUMP_BYPASS_ABS_SPEED`` > 0 and ``abs(edge_speed)`` meets or
        exceeds it, allow the jump so entries are not blocked during sharp moves.
        """
        return entry_edge_jump_ok(
            edge_now,
            edge_speed,
            entry_max_edge_jump_pts=self.entry_max_edge_jump_pts,
            entry_edge_jump_bypass_abs_speed=self.entry_edge_jump_bypass_abs_speed,
            edge_window=self.edge_window,
        )

    def entry_aggressive_trend_age_ok(self, edge_now: float, trend_age: float) -> bool:
        """Require extra seconds after trend start when edge is in aggressive magnitude."""
        return entry_aggressive_trend_age_ok(
            edge_now,
            trend_age,
            buy_edge=self.buy_edge,
            aggressive_edge_mult=self.aggressive_edge_mult,
            entry_aggressive_min_trend_age_sec=self.entry_aggressive_min_trend_age_sec,
        )

    def entry_trend_flip_settled_ok(self, trend_age: float) -> bool:
        """Avoid entries right after a trend cross (chop / saw)."""
        return entry_trend_flip_settled_ok(trend_age, self.trend_flip_min_age_sec)

    def entry_rsi_slope_allows(self, side: str, current_rsi: float) -> bool:
        """Require RSI oversold/overbought with favorable slope for UP/DOWN entries."""
        return entry_rsi_slope_allows(
            side,
            current_rsi,
            self._last_rsi_slope,
            entry_rsi_slope_filter_enabled=self.entry_rsi_slope_filter_enabled,
            rsi_up_entry_max=self.rsi_up_entry_max,
            rsi_up_slope_min=self.rsi_up_slope_min,
            rsi_down_entry_min=self.rsi_down_entry_min,
            rsi_down_slope_max=self.rsi_down_slope_max,
        )

    def _record_entry_samples(self, speed: float, zscore: float) -> None:
        """Append latest trend speed and z-score for acceleration and z-trend filters."""
        record_entry_samples(self, speed, zscore)

    def entry_liquidity_spread_ok(
        self,
        spread_up: float,
        spread_down: float,
        edge: float,
        trend_dir: str,
    ) -> bool:
        """Return False when UP/DOWN book spread is too wide unless oracle edge is very large."""
        return entry_liquidity_spread_ok(
            spread_up,
            spread_down,
            edge,
            trend_dir,
            entry_liquidity_max_spread=self.entry_liquidity_max_spread,
            spread_gate_up_relax_mult=self.spread_gate_up_relax_mult,
            wide_spread_min_edge=self.wide_spread_min_edge,
        )

    def entry_speed_acceleration_ok(self, trend_dir: str, speed: float) -> bool:
        """Require edge-speed acceleration in the trade direction when enabled."""
        return entry_speed_acceleration_ok(
            trend_dir,
            speed,
            self._speed_samples,
            entry_accel_enabled=self.entry_accel_enabled,
            entry_accel_min=self.entry_accel_min,
        )

    def entry_zscore_trend_ok(self, trend_dir: str, edge_speed: float = 0.0) -> bool:
        """Require z-score to move monotonically with the intended side for several ticks.

        When ``HFT_ENTRY_ZSCORE_BYPASS_ABS_SPEED`` > 0 and ``abs(edge_speed)`` meets or exceeds
        it, skip the monotonic z-score requirement so fast price breaks are not delayed.
        """
        return entry_zscore_trend_ok(
            trend_dir,
            edge_speed,
            self._zscore_samples,
            entry_zscore_trend_enabled=self.entry_zscore_trend_enabled,
            entry_zscore_strict_ticks=self.entry_zscore_strict_ticks,
            entry_zscore_bypass_abs_speed=self.entry_zscore_bypass_abs_speed,
        )

    def _zscore_monotonic_for_direction(self, trend_dir: str) -> bool:
        """Return True if recent z-score ticks are strictly monotone in the trade direction."""
        return zscore_monotonic_for_direction(
            self._zscore_samples, self.entry_zscore_strict_ticks, trend_dir
        )

    def _entry_momentum_alt_signal(
        self,
        edge: float,
        trend: str,
        speed: float,
        price_history,
        recent_pnl: float,
        latency_ms: float,
        edge_mult: float,
    ):
        """Secondary entry path: momentum + monotone z-score + acceleration without full trend age."""
        return entry_momentum_alt_signal(
            self,
            edge,
            trend,
            speed,
            price_history,
            recent_pnl,
            latency_ms,
            edge_mult,
        )

    def _entry_candidate_from_state(
        self,
        edge,
        age,
        trend,
        speed,
        price_history,
        recent_pnl=0.0,
        latency_ms=0.0,
        up_mid=0.0,
        down_mid=0.0,
        edge_mult=1.0,
    ):
        """Return BUY_UP/BUY_DOWN/None from trend vs oracle (no cooldown / no update_trend here)."""
        return entry_candidate_from_state(
            self,
            edge,
            age,
            trend,
            speed,
            price_history,
            recent_pnl,
            latency_ms,
            up_mid,
            down_mid,
            edge_mult,
        )

    def _book_move_for_outcome(self, token_mid, prev_key, want_up):
        """Return move size and whether it aligns with the trade direction."""
        if token_mid <= 0.0:
            return 0.0, False
        prev = getattr(self, prev_key)
        if prev is None:
            setattr(self, prev_key, token_mid)
            return 0.0, False
        move = token_mid - prev
        setattr(self, prev_key, token_mid)
        abs_move = abs(move)
        if want_up:
            aligned = move >= 0.0
        else:
            aligned = move <= 0.0
        return abs_move, aligned

    def generate_live_signal(
        self,
        fast_price,
        poly_mid,
        zscore,
        price_history=None,
        recent_pnl=0.0,
        latency_ms=0.0,
        *,
        poly_orderbook: dict | None = None,
        seconds_to_expiry: float | None = None,
    ):
        """Return raw entry candidate (BUY_UP/BUY_DOWN/None) from trend state.

        Call after ``process_tick`` in the same loop iteration so ``update_trend`` has run.

        When ``poly_orderbook`` is passed, uses the same ``up_mid`` / ``down_mid`` /
        ``edge_mult`` as ``process_tick`` (extreme-price gate + expiry edge scaling).
        When omitted, mids default to 0 and ``edge_mult`` to 1.0 — stricter parity with
        ``process_tick`` requires passing the current book.
        """
        _ = fast_price
        _ = poly_mid
        _ = zscore
        if price_history is None:
            price_history = []
        now = time.time()
        if now - self.last_trade_time < self.cooldown:
            return None
        tr = self.get_trend_state()
        if poly_orderbook is not None:
            *_, up_mid, down_mid = poly_book_outcome_quotes(poly_orderbook)
            edge_mult = self._latency_expiry_edge_multiplier(latency_ms, seconds_to_expiry)
        else:
            up_mid = 0.0
            down_mid = 0.0
            edge_mult = 1.0
        return self._entry_candidate_from_state(
            tr["edge"],
            tr["age"],
            tr["trend"],
            tr["speed"],
            price_history,
            recent_pnl=recent_pnl,
            latency_ms=latency_ms,
            up_mid=up_mid,
            down_mid=down_mid,
            edge_mult=edge_mult,
        )

    def get_trend_state(self):
        """Expose latest trend analytics for debug output."""
        speed = 0.0
        edge = 0.0
        if self.edge_window:
            edge = self.edge_window[-1][1]
        if len(self.edge_window) >= 2:
            t0, e0 = self.edge_window[-2]
            t1, e1 = self.edge_window[-1]
            speed = (e1 - e0) / max(t1 - t0, 1e-6)
        age = time.time() - self.trend_since_ts if self.trend_since_ts else 0.0
        return {
            "trend": self.trend_dir,
            "edge": edge,
            "speed": speed,
            "depth": self.trend_depth,
            "age": age,
        }

    async def process_tick(
        self,
        fast_price,
        poly_orderbook,
        price_history,
        lstm_forecast,
        zscore=0.0,
        latency_ms=0.0,
        recent_pnl=0.0,
        meta_enabled=True,
        seconds_to_expiry=None,
        skew_ms=0.0,
        slot_anchor_price: float = 0.0,
        **_ignored,
    ):
        self._diag_inc("ticks")
        self._emit_filter_diag_if_due()
        if not fast_price or not poly_orderbook['ask']:
            return
        _ = lstm_forecast

        if self.pnl.inventory == 0 and not self._regime_allows_new_entries():
            self._diag_inc("entry_block_regime")
            _now = time.time()
            _regime_log_sec = float(os.getenv("HFT_REGIME_SKIP_LOG_MIN_SEC"))
            if _regime_log_sec <= 0.0 or _now - self._last_regime_skip_log_ts >= _regime_log_sec:
                logging.info(
                    "Regime filter: recent performance is bad -> skip all entries "
                    "(set HFT_REGIME_BYPASS_SOFT_FLOW=1 to allow soft_flow despite bad streak, "
                    "or HFT_REGIME_FILTER_ENABLED=0 to disable)."
                )
                self._last_regime_skip_log_ts = _now
            return None

        px = price_array_for_rsi(price_history, self.rsi_price_len)
        raw_rsi = float(compute_rsi(px, period=self.rsi_period))
        self._last_rsi_raw = raw_rsi
        self._last_ma_fast = float(compute_ema_last(px, self.reaction_ma_period))
        _ml, _ms, _mh = compute_macd_last(
            px,
            fast=self.reaction_macd_fast,
            slow=self.reaction_macd_slow,
            signal=self.reaction_macd_signal,
        )
        self._last_macd_hist = float(_mh)
        _min_rx = max(
            self.reaction_macd_slow + self.reaction_macd_signal + 1,
            self.reaction_ma_period + 2,
            self.rsi_period + 2,
        )
        if self.reaction_score_enabled and int(px.size) >= _min_rx:
            current_rsi = float(
                compute_reaction_score(
                    raw_rsi,
                    float(px[-1]),
                    self._last_ma_fast,
                    self._last_macd_hist,
                    ma_rel_scale=self.reaction_ma_rel_scale,
                    macd_hist_scale=self.reaction_macd_hist_scale,
                    w_rsi=self.reaction_w_rsi,
                    w_ma=self.reaction_w_ma,
                    w_macd=self.reaction_w_macd,
                )
            )
        else:
            current_rsi = raw_rsi
        self._last_rsi = current_rsi
        self._rsi_tick_history.append(current_rsi)
        upper_b, lower_b = dynamic_rsi_bands(
            px,
            base_upper=self.rsi_exit_upper_base,
            base_lower=self.rsi_exit_lower_base,
            k=self.rsi_band_vol_k,
        )
        self._last_rsi_upper = upper_b
        self._last_rsi_lower = lower_b
        self._last_rsi_slope = self._rsi_slope_per_tick()

        poly_mid = float(
            poly_orderbook.get("btc_oracle")
            or poly_orderbook.get("mid", 0.0)
            or 0.0
        )
        bid_size = float(poly_orderbook.get("bid_size_top", 1.0))
        ask_size = float(poly_orderbook.get("ask_size_top", 1.0))
        db_top = float(poly_orderbook.get("down_bid_size_top", 0.0))
        da_top = float(poly_orderbook.get("down_ask_size_top", 0.0))
        if self.pnl.inventory > 0 and self.pnl.position_side == "DOWN" and db_top + da_top > 0.0:
            imbalance = db_top / (db_top + da_top + 1e-9)
        else:
            imbalance = bid_size / (bid_size + ask_size + 1e-9)

        up_bid, up_ask, down_bid, down_ask, up_mid, down_mid = poly_book_outcome_quotes(
            poly_orderbook
        )

        self.update_trend(fast_price, poly_mid)
        trend = self.get_trend_state()
        edge_now = trend["edge"]
        spread_up = max(0.0, up_ask - up_bid)
        spread_down = max(0.0, down_ask - down_bid)
        edge_mult = self._latency_expiry_edge_multiplier(latency_ms, seconds_to_expiry)
        self._record_entry_samples(trend["speed"], float(zscore))
        if self.max_entry_spread <= 0.0:
            spread_gate_legacy = True
        else:
            _max_sp = self.max_entry_spread
            if trend["trend"] == "UP" and self.spread_gate_up_relax_mult > 1.0:
                _max_sp = _max_sp * self.spread_gate_up_relax_mult
            spread_gate_legacy = (
                spread_up <= _max_sp or abs(edge_now) >= self.wide_spread_min_edge
            )
        liquidity_ok = self.entry_liquidity_spread_ok(
            spread_up, spread_down, edge_now, trend["trend"]
        )
        speed_ok = self.entry_speed_acceleration_ok(trend["trend"], trend["speed"])
        z_ok = self.entry_zscore_trend_ok(trend["trend"], edge_speed=trend["speed"])
        entry_context_ok = speed_ok and z_ok
        chop_latency_ok = (
            self.entry_latency_allows_entry(latency_ms)
            and self.entry_skew_allows_entry(skew_ms)
            and self.entry_trend_flip_settled_ok(trend["age"])
        )
        edge_jump_ok = self.entry_edge_jump_ok(edge_now, trend["speed"])
        aggressive_age_ok = self.entry_aggressive_trend_age_ok(edge_now, trend["age"])
        if self.no_entry_guards:
            spread_gate = True
        else:
            spread_gate = (
                spread_gate_legacy
                and liquidity_ok
                and entry_context_ok
                and chop_latency_ok
                and edge_jump_ok
                and aggressive_age_ok
            )
        signal = None
        slot_entry_ok = self._entry_slot_window_allows(seconds_to_expiry)
        anchor_ok_up, anchor_ok_down = self._anchor_gate(fast_price, slot_anchor_price)
        _now_entries = time.time()
        _post_close_ok = (
            self.last_close_time <= 0.0
            or self.post_close_reentry_sec <= 0.0
            or _now_entries - self.last_close_time >= self.post_close_reentry_sec
        )
        if self.pnl.inventory == 0 and (_now_entries - self.last_trade_time >= self.cooldown) and _post_close_ok:
            self._diag_inc("entry_checks")
            signal = self._entry_candidate_from_state(
                edge_now,
                trend["age"],
                trend["trend"],
                trend["speed"],
                price_history,
                recent_pnl=recent_pnl,
                latency_ms=latency_ms,
                up_mid=up_mid,
                down_mid=down_mid,
                edge_mult=edge_mult,
            )
            if signal is None:
                signal = self._entry_momentum_alt_signal(
                    edge_now,
                    trend["trend"],
                    trend["speed"],
                    price_history,
                    recent_pnl,
                    latency_ms,
                    edge_mult,
                )
            if signal is None:
                self._diag_inc("entry_no_signal")
            if (
                signal is not None
                and not spread_gate
            ):
                self._diag_inc("entry_block_spread_gate")
                if not spread_gate_legacy:
                    self._diag_inc("entry_block_spread_legacy")
                if not liquidity_ok:
                    self._diag_inc("entry_block_liquidity")
                if not speed_ok:
                    self._diag_inc("entry_block_speed")
                if not z_ok:
                    self._diag_inc("entry_block_zscore")
                if not self.entry_latency_allows_entry(latency_ms):
                    self._diag_inc("entry_block_latency")
                if not self.entry_skew_allows_entry(skew_ms):
                    self._diag_inc("entry_block_skew")
                if not self.entry_trend_flip_settled_ok(trend["age"]):
                    self._diag_inc("entry_block_trend_flip_age")
                if not edge_jump_ok:
                    self._diag_inc("entry_block_edge_jump")
                if not aggressive_age_ok:
                    self._diag_inc("entry_block_aggressive_age")
                _now = time.time()
                _fg_log = float(os.getenv("HFT_FEED_GATE_LOG_MIN_SEC"))
                if (
                    _fg_log > 0.0
                    and _now - self._last_feed_gate_log_ts >= _fg_log
                ):
                    logging.info(
                        "Entry blocked by spread_gate: signal=%s stale=%.0fms skew=%.0fms "
                        "spread_legacy=%s liq=%s speed_z=%s/%s chop_lat_skew_flip=%s/%s/%s "
                        "edge_jump=%s agr_age=%s",
                        signal,
                        latency_ms,
                        skew_ms,
                        spread_gate_legacy,
                        liquidity_ok,
                        speed_ok,
                        z_ok,
                        self.entry_latency_allows_entry(latency_ms),
                        self.entry_skew_allows_entry(skew_ms),
                        self.entry_trend_flip_settled_ok(trend["age"]),
                        edge_jump_ok,
                        aggressive_age_ok,
                    )
                    self._last_feed_gate_log_ts = _now

        if self.pnl.inventory == 0:
            abs_move_up, aligned_up = self._book_move_for_outcome(up_mid, "_prev_up_mid", want_up=True)
            abs_move_down, aligned_down = self._book_move_for_outcome(down_mid, "_prev_down_mid", want_up=True)
            if self.book_move_entry_min <= 0.0:
                book_entry_up = True
                book_entry_down = True
            else:
                book_entry_up = aligned_up and abs_move_up >= self.book_move_entry_min
                book_entry_down = aligned_down and abs_move_down >= self.book_move_entry_min
        else:
            book_entry_up = False
            book_entry_down = False

        strong_rsi = self._is_strong_oracle_edge(edge_now)
        aggressive_edge = self._is_aggressive_oracle_edge(edge_now)
        rsi_agg_bypass = (
            aggressive_edge and self.rsi_allow_bypass_on_aggressive_edge
        )
        if self.no_entry_guards:
            rsi_ok_up = True
            rsi_ok_down = True
            book_ok_up = True
            book_ok_down = True
        elif self.entry_rsi_slope_filter_enabled:
            rsi_ok_up = (
                (strong_rsi and self.rsi_allow_bypass_on_strong_edge)
                or rsi_agg_bypass
                or self.entry_rsi_slope_allows("UP", current_rsi)
            )
            rsi_ok_down = (
                (strong_rsi and self.rsi_allow_bypass_on_strong_edge)
                or rsi_agg_bypass
                or self.entry_rsi_slope_allows("DOWN", current_rsi)
            )
            book_ok_up = book_entry_up or strong_rsi or aggressive_edge
            book_ok_down = book_entry_down or strong_rsi or aggressive_edge
        else:
            in_band_up = (
                self.rsi_entry_up_low < current_rsi < self.rsi_entry_up_high
            )
            in_band_down = (
                self.rsi_entry_down_low < current_rsi < self.rsi_entry_down_high
            )
            rsi_ok_up = in_band_up or (
                strong_rsi and self.rsi_allow_bypass_on_strong_edge
            ) or rsi_agg_bypass
            rsi_ok_down = in_band_down or (
                strong_rsi and self.rsi_allow_bypass_on_strong_edge
            ) or rsi_agg_bypass
            book_ok_up = book_entry_up or strong_rsi or aggressive_edge
            book_ok_down = book_entry_down or strong_rsi or aggressive_edge

        _t_cap = time.time()
        _cap_log_sec = float(os.getenv("HFT_ENTRY_CAP_DENY_LOG_SEC"))
        if (
            self.pnl.inventory == 0
            and signal == "BUY_UP"
            and not self._entry_outcome_price_allows("UP", up_ask, down_ask)
            and _cap_log_sec > 0.0
            and _t_cap - self._last_entry_cap_deny_log_ts >= _cap_log_sec
        ):
            self._diag_inc("entry_block_ask_cap")
            logging.info(
                "Entry blocked: BUY_UP up_ask=%.4f outside [HFT_ENTRY_MIN_ASK_UP=%.4f, HFT_ENTRY_MAX_ASK_UP=%.4f].",
                up_ask,
                self.entry_min_ask_up_cap,
                self.entry_max_ask_up_cap,
            )
            self._last_entry_cap_deny_log_ts = _t_cap
        if (
            self.pnl.inventory == 0
            and signal == "BUY_DOWN"
            and not self._entry_outcome_price_allows("DOWN", up_ask, down_ask)
            and _cap_log_sec > 0.0
            and _t_cap - self._last_entry_cap_deny_log_ts >= _cap_log_sec
        ):
            self._diag_inc("entry_block_ask_cap")
            logging.info(
                "Entry blocked: BUY_DOWN down_ask=%.4f outside [HFT_ENTRY_MIN_ASK_DOWN=%.4f, HFT_ENTRY_MAX_ASK_DOWN=%.4f].",
                down_ask,
                self.entry_min_ask_down_cap,
                self.entry_max_ask_down_cap,
            )
            self._last_entry_cap_deny_log_ts = _t_cap

        if (
            signal == "BUY_UP"
            and self.pnl.inventory == 0
            and self.can_trade()
            and self._entry_ask_allows_open(up_ask)
            and self._entry_outcome_price_allows("UP", up_ask, down_ask)
            and rsi_ok_up
            and book_ok_up
            and spread_gate
            and slot_entry_ok
            and anchor_ok_up
            and meta_enabled
        ):
            _notional_up = self._calc_dynamic_amount(up_ask)
            open_event = await self.execute("BUY_UP", up_ask, _notional_up)
            if not open_event:
                self._diag_inc("entry_open_no_fill")
                logging.warning(
                    "SIM BUY_UP skipped: no fill (balance=%.2f notional=%.2f ask=%.4f).",
                    float(self.pnl.balance),
                    float(_notional_up),
                    float(up_ask),
                )
            else:
                self._diag_inc("entry_open_ok")
                _sup = bool(open_event.get("suppressed"))
                if not _sup:
                    self.last_trade_time = time.time()
                    self._reset_trailing_state()
                    self.entry_poly_mid = poly_mid
                    self.entry_outcome_mid = up_mid
                    self.entry_fast_price = fast_price
                    self.entry_time = time.time()
                else:
                    self._live_entry_sync_pending = True
                self.position_trend = trend["trend"]
                self.entry_context = {
                    "entry_edge": fast_price - poly_mid,
                    "entry_trend": trend["trend"],
                    "entry_speed": trend["speed"],
                    "entry_depth": trend["depth"],
                    "entry_imbalance": imbalance,
                    "latency_ms": latency_ms,
                    "skew_ms": float(skew_ms),
                    "entry_book_px": float((open_event or {}).get("book_px") or 0.0),
                    "entry_exec_px": float((open_event or {}).get("exec_px") or 0.0),
                    "shares_bought": float((open_event or {}).get("shares_filled") or 0.0),
                    "cost_usd": float((open_event or {}).get("amount_usd") or 0.0),
                    "entry_up_bid": up_bid,
                    "entry_up_ask": up_ask,
                    "entry_down_bid": down_bid,
                    "entry_down_ask": down_ask,
                    "strategy_name": self._display_strategy_name(),
                    "entry_profile": self.get_active_profile(),
                }
                logging.info(
                    "🧭 Entry context: poly_mid=%.4f fast=%.2f edge=%.2f trend=%s imb=%.2f | "
                    "strategy=%s profile=%s",
                    poly_mid,
                    fast_price,
                    fast_price - poly_mid,
                    self.position_trend,
                    imbalance,
                    self._display_strategy_name(),
                    self.get_active_profile(),
                )
                return {"event": "OPEN", "side": "UP", "trade": open_event}
        elif signal == "BUY_UP" and self.pnl.inventory == 0:
            if not self.can_trade():
                self._diag_inc("entry_block_can_trade")
            if not self._entry_ask_allows_open(up_ask):
                self._diag_inc("entry_block_ask_cap")
            if not rsi_ok_up:
                self._diag_inc("entry_block_rsi")
            if not book_ok_up:
                self._diag_inc("entry_block_book")
            if not spread_gate:
                self._diag_inc("entry_block_spread_gate")
            if not slot_entry_ok:
                self._diag_inc("entry_block_slot")
            if not anchor_ok_up:
                self._diag_inc("entry_block_anchor")
            if not meta_enabled:
                self._diag_inc("entry_block_meta")

        if (
            signal == "BUY_DOWN"
            and self.pnl.inventory == 0
            and self.can_trade()
            and self._entry_ask_allows_open(down_ask)
            and self._entry_outcome_price_allows("DOWN", up_ask, down_ask)
            and rsi_ok_down
            and book_ok_down
            and spread_gate
            and slot_entry_ok
            and anchor_ok_down
            and meta_enabled
        ):
            _notional_dn = self._calc_dynamic_amount(down_ask)
            open_event = await self.execute("BUY_DOWN", down_ask, _notional_dn)
            if not open_event:
                self._diag_inc("entry_open_no_fill")
                logging.warning(
                    "SIM BUY_DOWN skipped: no fill (balance=%.2f notional=%.2f ask=%.4f).",
                    float(self.pnl.balance),
                    float(_notional_dn),
                    float(down_ask),
                )
            else:
                self._diag_inc("entry_open_ok")
                _sup = bool(open_event.get("suppressed"))
                if not _sup:
                    self.last_trade_time = time.time()
                    self._reset_trailing_state()
                    self.entry_poly_mid = poly_mid
                    self.entry_outcome_mid = down_mid
                    self.entry_fast_price = fast_price
                    self.entry_time = time.time()
                else:
                    self._live_entry_sync_pending = True
                self.position_trend = trend["trend"]
                self.entry_context = {
                    "entry_edge": fast_price - poly_mid,
                    "entry_trend": trend["trend"],
                    "entry_speed": trend["speed"],
                    "entry_depth": trend["depth"],
                    "entry_imbalance": imbalance,
                    "latency_ms": latency_ms,
                    "skew_ms": float(skew_ms),
                    "entry_book_px": float((open_event or {}).get("book_px") or 0.0),
                    "entry_exec_px": float((open_event or {}).get("exec_px") or 0.0),
                    "shares_bought": float((open_event or {}).get("shares_filled") or 0.0),
                    "cost_usd": float((open_event or {}).get("amount_usd") or 0.0),
                    "entry_up_bid": up_bid,
                    "entry_up_ask": up_ask,
                    "entry_down_bid": down_bid,
                    "entry_down_ask": down_ask,
                    "strategy_name": self._display_strategy_name(),
                    "entry_profile": self.get_active_profile(),
                }
                logging.info(
                    "🧭 Entry context: side=BUY_DOWN poly_mid=%.4f fast=%.2f edge=%.2f trend=%s imb=%.2f | "
                    "strategy=%s profile=%s",
                    poly_mid,
                    fast_price,
                    fast_price - poly_mid,
                    self.position_trend,
                    imbalance,
                    self._display_strategy_name(),
                    self.get_active_profile(),
                )
                return {"event": "OPEN", "side": "DOWN", "trade": open_event}
        elif signal == "BUY_DOWN" and self.pnl.inventory == 0:
            if not self.can_trade():
                self._diag_inc("entry_block_can_trade")
            if not self._entry_ask_allows_open(down_ask):
                self._diag_inc("entry_block_ask_cap")
            if not rsi_ok_down:
                self._diag_inc("entry_block_rsi")
            if not book_ok_down:
                self._diag_inc("entry_block_book")
            if not spread_gate:
                self._diag_inc("entry_block_spread_gate")
            if not slot_entry_ok:
                self._diag_inc("entry_block_slot")
            if not anchor_ok_down:
                self._diag_inc("entry_block_anchor")
            if not meta_enabled:
                self._diag_inc("entry_block_meta")

        if self.pnl.inventory > 0:
            self._diag_inc("exit_checks")
            now = time.time()
            hold_sec = now - self.entry_time if self.entry_time else 0.0
            poly_move = 0.0
            if self.entry_poly_mid and self.entry_poly_mid > 0:
                poly_move = (poly_mid - self.entry_poly_mid) / self.entry_poly_mid
            side_mid = down_mid if self.pnl.position_side == "DOWN" else up_mid
            side_move = 0.0
            if self.entry_outcome_mid and self.entry_outcome_mid > 0:
                side_move = (side_mid - self.entry_outcome_mid) / self.entry_outcome_mid

            pos_side = self.pnl.position_side or "UP"
            sec_left = float(seconds_to_expiry) if seconds_to_expiry is not None else None

            if (
                sec_left is not None
                and self.slot_force_close_last_sec > 0.0
                and sec_left <= self.slot_force_close_last_sec
            ):
                logging.warning(
                    "⚠️ СЛОТ: до конца ≤%.1fс (осталось %.2fс) — принудительное закрытие по 99¢.",
                    self.slot_force_close_last_sec,
                    sec_left,
                )
                return await self._close_position_slot_edge(
                    "SLOT_END_FORCE",
                    0.99,
                    hold_sec,
                    fast_price,
                    poly_mid,
                    pos_side,
                    up_bid,
                    up_ask,
                    down_bid,
                    down_ask,
                )

            if sec_left is not None and sec_left < self.slot_99c_max_sec:
                reached_99c = (
                    (down_bid >= 0.99 or down_ask >= 0.99)
                    if pos_side == "DOWN"
                    else (up_bid >= 0.99 or up_ask >= 0.99)
                )
                if reached_99c:
                    logging.warning(
                        "⚠️ СЛОТ: осталось %.0fс (< %.0fс) и 99¢ на книге — закрываем по 99¢.",
                        sec_left,
                        self.slot_99c_max_sec,
                    )
                    return await self._close_position_slot_edge(
                        "SLOT_EXPIRY_99C",
                        0.99,
                        hold_sec,
                        fast_price,
                        poly_mid,
                        pos_side,
                        up_bid,
                        up_ask,
                        down_bid,
                        down_ask,
                    )

            if sec_left is not None and sec_left < self.slot_expiry_info_max_sec:
                _now = time.time()
                _slot_log_sec = float(os.getenv("HFT_SLOT_EXPIRY_INFO_LOG_MIN_SEC"))
                if _slot_log_sec <= 0.0 or _now - self._last_slot_expiry_info_log_ts >= _slot_log_sec:
                    logging.info(
                        "⏳ СЛОТ ЗАКАНЧИВАЕТСЯ (%.0fс), но 99¢ не достигнуты -> продолжаем плановый выход.",
                        sec_left,
                    )
                    self._last_slot_expiry_info_log_ts = _now

            if self.pnl.position_side == "DOWN":
                reaction_confirmed = self._hold_met(hold_sec) and side_move >= self.poly_take_profit_move
                protective_stop = self._hold_met(hold_sec) and side_move <= -self.poly_stop_move
            else:
                reaction_confirmed = self._hold_met(hold_sec) and side_move >= self.poly_take_profit_move
                protective_stop = self._hold_met(hold_sec) and side_move <= -self.poly_stop_move
            unrealized = self.pnl.get_unrealized_pnl(poly_orderbook)
            self._update_trailing_state(unrealized)
            trailing_tp = self._trailing_tp_triggered(unrealized, hold_sec)
            trailing_sl = self._trailing_sl_triggered(unrealized, hold_sec)
            tp_line, sl_line = self._pnl_target_and_stop_lines()
            pnl_sl = self._hold_met(hold_sec) and unrealized <= -sl_line
            
            # Diagnostic logging for exit conditions
            if hold_sec >= self.min_hold_sec:
                logging.debug(
                    "EXIT_DIAG: hold_sec=%.2f side_move=%.4f poly_tp_move=%.4f poly_sl_move=%.4f "
                    "unrealized=%.4f sl_line=%.4f tp_line=%.4f "
                    "reaction_confirmed=%s protective_stop=%s pnl_sl=%s",
                    hold_sec, side_move, self.poly_take_profit_move, self.poly_stop_move,
                    unrealized, sl_line, tp_line,
                    reaction_confirmed, protective_stop, pnl_sl,
                )
            pos_side = self.pnl.position_side or "UP"
            rsi_x = self._exit_rsi(current_rsi)
            # Imbalance gate: when RSI is in extreme territory, imbalance alone
            # is not reliable — a large bid can appear without a real reversal.
            # Only allow imbalance to trigger internal_reversal when RSI is above
            # the extreme-low threshold (DOWN) or below extreme-high (UP).
            _imb_rsi_gate = float(os.getenv("HFT_INTERNAL_REVERSAL_IMB_RSI_GATE"))
            if pos_side == "DOWN":
                # DOWN token profits when BTC falls → exit when BTC is overbought
                # (Rx high) signalling a reversal back up that would hurt the position.
                # Imbalance gate: only fire on ask-heavy book when RSI not extreme-low.
                _imb_reversal_ok = imbalance >= 0.55 and rsi_x > _imb_rsi_gate
                internal_reversal = (
                    _imb_reversal_ok
                    or rsi_x >= upper_b
                    or self._last_rsi_slope >= self.rsi_slope_down_exit
                )
            else:
                # UP token profits when BTC rises → take profit when BTC is overbought
                # (Rx >= upper_b), meaning UP-token price is at its peak.
                # slope <= rsi_slope_up_exit (e.g. -2) means Rx turning down: momentum fading.
                # Imbalance gate: bid-light book (imbalance <= 0.45) signals sellers returning.
                _imb_reversal_ok = imbalance <= 0.45 and rsi_x < (100.0 - _imb_rsi_gate)
                internal_reversal = (
                    _imb_reversal_ok
                    or rsi_x >= upper_b
                    or self._last_rsi_slope <= self.rsi_slope_up_exit
                )
            reaction_tp_confirmed = reaction_confirmed and internal_reversal
            rsi_range_exit = self._rsi_range_exit_triggered(
                pos_side,
                current_rsi,
                unrealized,
                hold_sec,
            )

            opposite_trend = (
                (pos_side == "DOWN" and trend["trend"] == "UP")
                or (pos_side == "UP" and trend["trend"] == "DOWN")
            )
            trend_flip_exit = self._opposite_trend_exit_triggered(
                pos_side,
                str(trend["trend"]),
                hold_sec,
                abs(float(edge_now)),
            )

            should_close = (
                trend_flip_exit
                or trailing_tp
                or trailing_sl
                or reaction_tp_confirmed
                or protective_stop
                or rsi_range_exit
                or pnl_sl
            )
            # region agent log
            if opposite_trend and not should_close:
                _append_debug_log(
                    {
                        "sessionId": DEBUG_SESSION_ID,
                        "runId": "post-fix",
                        "hypothesisId": "H2",
                        "location": "core/engine.py:process_tick",
                        "message": "Opposite trend while keeping position open.",
                        "data": {
                            "position_side": pos_side,
                            "trend": trend["trend"],
                            "hold_sec": hold_sec,
                            "side_mid": side_mid,
                            "side_move": side_move,
                            "poly_move": poly_move,
                            "unrealized": unrealized,
                            "reaction_tp_confirmed": reaction_tp_confirmed,
                            "protective_stop": protective_stop,
                            "rsi_range_exit": rsi_range_exit,
                            "pnl_sl": pnl_sl,
                        },
                        "timestamp": int(time.time() * 1000),
                    }
                )
            # endregion
            if should_close:
                self._diag_inc("exit_close")
                reason = "REACTION_TP"
                if trend_flip_exit:
                    self._diag_inc("exit_reason_trend_flip")
                    reason = "TREND_FLIP_EXIT"
                elif trailing_tp:
                    self._diag_inc("exit_reason_trailing_tp")
                    reason = "TRAILING_TP"
                elif trailing_sl:
                    self._diag_inc("exit_reason_trailing_sl")
                    reason = "TRAILING_SL"
                elif protective_stop:
                    self._diag_inc("exit_reason_reaction_stop")
                    reason = "REACTION_STOP"
                elif rsi_range_exit:
                    self._diag_inc("exit_reason_rsi_range")
                    reason = "RSI_RANGE_EXIT"
                elif pnl_sl:
                    self._diag_inc("exit_reason_pnl_sl")
                    reason = "PNL_SL"
                else:
                    self._diag_inc("exit_reason_reaction_tp")
                
                # Diagnostic logging for exit reason priority
                logging.info(
                    "EXIT_REASON_DIAG: reason=%s hold=%.2f "
                    "trend_flip=%s trailing_tp=%s trailing_sl=%s reaction_tp=%s "
                    "protective_stop=%s rsi_range=%s pnl_sl=%s "
                    "side_move=%.4f poly_sl_move=%.4f unrealized=%.4f sl_line=%.4f",
                    reason, hold_sec,
                    trend_flip_exit, trailing_tp, trailing_sl, reaction_tp_confirmed,
                    protective_stop, rsi_range_exit, pnl_sl,
                    side_move, self.poly_stop_move, unrealized, sl_line,
                )
                
                _trail_info = ""
                if self.trailing_tp_enabled or self.trailing_sl_enabled:
                    _sf = self._trailing_sl_floor
                    _sl_floor_s = f"{_sf:.4f}" if _sf is not None else "None"
                    _trail_info = (
                        f" peak_pnl={self._peak_unrealized:+.4f}"
                        f" sl_floor={_sl_floor_s}"
                    )
                logging.info(
                    "📌 Exit reason=%s hold=%.1fs poly_move=%.4f side_move=%.4f edge=%.2f pnl=%.2f imb=%.2f "
                    "rsi=%.1f band=[%.1f,%.1f] slope=%+.2f%s",
                    reason,
                    hold_sec,
                    poly_move,
                    side_move,
                    fast_price - poly_mid,
                    unrealized,
                    imbalance,
                    current_rsi,
                    lower_b,
                    upper_b,
                    self._last_rsi_slope,
                    _trail_info,
                )
                exit_price = down_bid if self.pnl.position_side == "DOWN" else up_bid
                close_event = await self.execute("SELL", exit_price)
                ce = close_event or {}
                _pk = ce.get("performance_key")
                result = {
                    "event": "CLOSE",
                    "reason": reason,
                    "entry_edge": self.entry_context.get("entry_edge", 0.0),
                    "exit_edge": fast_price - poly_mid,
                    "duration_sec": hold_sec,
                    "entry_trend": self.entry_context.get("entry_trend", "FLAT"),
                    "entry_speed": self.entry_context.get("entry_speed", 0.0),
                    "entry_depth": self.entry_context.get("entry_depth", 0.0),
                    "entry_imbalance": self.entry_context.get("entry_imbalance", 0.0),
                    "latency_ms": self.entry_context.get("latency_ms", 0.0),
                    "pnl": float(ce.get("pnl") or 0.0),
                    "side": pos_side,
                    "strategy_name": self.entry_context.get("strategy_name"),
                    "entry_profile": self.entry_context.get("entry_profile"),
                    "performance_key": _pk,
                    "entry_book_px": self.entry_context.get("entry_book_px", 0.0),
                    "entry_exec_px": self.entry_context.get("entry_exec_px", 0.0),
                    "exit_book_px": float(ce.get("book_px") or exit_price),
                    "exit_exec_px": float(ce.get("exec_px") or 0.0),
                    "shares_bought": self.entry_context.get("shares_bought", 0.0),
                    "shares_sold": float(ce.get("shares_sold") or 0.0),
                    "cost_usd": self.entry_context.get("cost_usd", 0.0),
                    "proceeds_usd": float(ce.get("proceeds_usd") or 0.0),
                    "cost_basis_usd": float(ce.get("cost_basis_usd") or 0.0),
                    "entry_up_bid": self.entry_context.get("entry_up_bid"),
                    "entry_up_ask": self.entry_context.get("entry_up_ask"),
                    "entry_down_bid": self.entry_context.get("entry_down_bid"),
                    "entry_down_ask": self.entry_context.get("entry_down_ask"),
                    "exit_up_bid": up_bid,
                    "exit_up_ask": up_ask,
                    "exit_down_bid": down_bid,
                    "exit_down_ask": down_ask,
                }
                self.entry_poly_mid = None
                self.entry_outcome_mid = None
                self.entry_fast_price = None
                self.entry_time = 0.0
                self.position_trend = "FLAT"
                self.entry_context = {}
                self._book_stall_ticks = 0
                self._prev_up_mid = None
                self._prev_down_mid = None
                self.last_close_time = time.time()
                self._reset_trailing_state()
                return result
            self._diag_inc("exit_hold")

    def _build_performance_key(self) -> str | None:
        """Build attribution key from entry context and engine label."""
        if not self.entry_context:
            return None
        name = str(self.entry_context.get("strategy_name") or self._display_strategy_name())
        prof = str(self.entry_context.get("entry_profile") or self.get_active_profile())
        return f"{name}:{prof}"

    async def execute(self, side, price, amount_usd=None, settlement_fill=False):
        """Execute simulated trade with optional notional override."""
        if amount_usd is None:
            amount_usd = self.trade_amount_usd
        perf_key = None
        if side == "SELL":
            perf_key = self._build_performance_key()
        try:
            sn = self._strategy_name_for_sim_log(side)
        except Exception:
            logging.exception("HFT: strategy name for SIM log failed; using raw label.")
            sn = self._strategy_label
        try:
            return self.pnl.log_trade(
                side,
                price,
                amount_usd,
                settlement_fill=settlement_fill,
                performance_key=perf_key,
                strategy_name=sn,
            )
        except TypeError as err:
            if "strategy_name" not in str(err) and "unexpected keyword" not in str(err):
                raise
            logging.warning(
                "HFT: log_trade has no strategy_name= (old executor); retrying without it: %s",
                err,
            )
            return self.pnl.log_trade(
                side,
                price,
                amount_usd,
                settlement_fill=settlement_fill,
                performance_key=perf_key,
            )
