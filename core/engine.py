"""Signal, risk, and simulated execution for Polymarket latency strategy."""

import itertools
import logging
import os
import time
from collections import deque

import numpy as np

from ml.indicators import compute_rsi, dynamic_rsi_bands


def _price_array_for_rsi(price_history, max_len: int) -> np.ndarray:
    """Build a compact float array for RSI without copying unbounded history."""
    if not price_history:
        return np.empty(0, dtype=np.float64)
    n = len(price_history)
    if n <= max_len:
        return np.asarray(price_history, dtype=np.float64)
    return np.asarray(
        list(itertools.islice(price_history, n - max_len, None)),
        dtype=np.float64,
    )


class HFTEngine:
    """Signal, risk, and execution engine for Polymarket latency strategy."""

    def __init__(self, pnl_tracker, is_test_mode=True, strategy_label="latency_arbitrage"):
        self.pnl = pnl_tracker
        self.is_test_mode = is_test_mode
        self._strategy_label = str(strategy_label)
        
        # --- Базовый Edge (в пунктах цены) ---
        self.noise_edge = float(os.getenv("HFT_NOISE_EDGE", "0.8"))   # Уменьшаем порог шума для более чувствительного входа
        self.buy_edge = float(os.getenv("HFT_BUY_EDGE", "2.5"))      # Уменьшаем порог входа для большего количества сигналов
        self.sell_edge = -float(os.getenv("HFT_SELL_EDGE_ABS", "5.0"))  # Уменьшаем порог выхода
        
        # --- Тайминги и объемы ---
        self.cooldown = float(os.getenv("HFT_COOLDOWN_SEC", "0.05"))
        self.last_trade_time = 0.0
        self.max_position = float(os.getenv("HFT_MAX_POSITION_USD", "100.0"))
        self.trade_amount_usd = float(os.getenv("HFT_DEFAULT_TRADE_USD", "10.0"))
        self.min_hold_sec = float(os.getenv("HFT_MIN_HOLD_SEC", "2.0"))
        self.reaction_timeout_sec = float(os.getenv("HFT_REACTION_TIMEOUT_SEC", "10.0"))
        self.entry_poly_mid = None
        self.entry_fast_price = None
        self.entry_time = 0.0

        # --- Тейки и Стопы (в пунктах и USD) ---
        self.poly_take_profit_move = float(os.getenv("HFT_POLY_TP_MOVE", "0.0030"))
        self.poly_stop_move = float(os.getenv("HFT_POLY_SL_MOVE", "0.0025"))
        self.target_profit_usd = float(os.getenv("HFT_TARGET_PROFIT_USD", "2.5"))
        self.stop_loss_usd = float(os.getenv("HFT_STOP_LOSS_USD", "1.5"))
        self.pnl_tp_pct = float(os.getenv("HFT_PNL_TP_PERCENT", "0.07"))
        self.pnl_sl_pct = float(os.getenv("HFT_PNL_SL_PERCENT", "0.05"))
        self.pnl_tp_min_hold_sec = float(os.getenv("HFT_PNL_TP_MIN_HOLD_SEC", "2.0"))

        # --- RSI логика ---
        self.rsi_period = 14
        self.rsi_price_len = int(os.getenv("HFT_RSI_PRICE_LEN", "128"))
        self._last_rsi = 50.0
        self.rsi_entry_up_low = float(
            os.getenv("HFT_RSI_ENTRY_UP_LOW", os.getenv("HFT_RSI_ENTRY_YES_LOW", "20.0"))
        )
        self.rsi_entry_up_high = float(
            os.getenv("HFT_RSI_ENTRY_UP_HIGH", os.getenv("HFT_RSI_ENTRY_YES_HIGH", "80.0"))
        )
        self.rsi_entry_down_low = float(
            os.getenv("HFT_RSI_ENTRY_DOWN_LOW", os.getenv("HFT_RSI_ENTRY_NO_LOW", "20.0"))
        )
        self.rsi_entry_down_high = float(
            os.getenv("HFT_RSI_ENTRY_DOWN_HIGH", os.getenv("HFT_RSI_ENTRY_NO_HIGH", "80.0"))
        )
        
        # Выходы по RSI
        self.rsi_exit_upper_base = float(os.getenv("HFT_RSI_EXIT_UPPER_BASE", "85"))
        self.rsi_exit_lower_base = float(os.getenv("HFT_RSI_EXIT_LOWER_BASE", "15"))
        self.rsi_range_exit_min_profit_usd = float(os.getenv("HFT_RSI_RANGE_EXIT_MIN_PROFIT_USD", "0.3"))
        self.rsi_range_exit_band_margin = float(os.getenv("HFT_RSI_RANGE_EXIT_BAND_MARGIN", "10.0"))
        self.rsi_extreme_high = float(os.getenv("HFT_RSI_EXTREME_HIGH", "90"))
        self.rsi_extreme_low = float(os.getenv("HFT_RSI_EXTREME_LOW", "10"))
        self.rsi_band_vol_k = float(os.getenv("HFT_RSI_BAND_VOL_K", "0.12"))
        self.rsi_range_exit_profit_frac = float(os.getenv("HFT_RSI_RANGE_EXIT_PROFIT_FRAC", "0.6"))
        self.rsi_range_exit_min_hold_sec = float(os.getenv("HFT_RSI_RANGE_EXIT_MIN_HOLD_SEC", "0.0"))
        self.rsi_exit_clamp_high = float(os.getenv("HFT_RSI_EXIT_CLAMP_HIGH", "99.0"))
        self.rsi_exit_clamp_low = float(os.getenv("HFT_RSI_EXIT_CLAMP_LOW", "1.0"))

        # --- RSI Slope (Наклон) ---
        self.rsi_slope_exit_enabled = os.getenv("HFT_RSI_SLOPE_EXIT_ENABLED", "1") == "1"
        self.rsi_slope_up_exit = -2.0
        self.rsi_slope_down_exit = 2.0
        self._rsi_tick_history = deque(maxlen=10)
        self._last_rsi_upper = 70.0
        self._last_rsi_lower = 30.0
        self._last_rsi_slope = 0.0
        self.rsi_hold_up_floor = float(
            os.getenv("HFT_RSI_HOLD_UP_FLOOR", os.getenv("HFT_RSI_HOLD_YES_FLOOR", "40.0"))
        )
        self.rsi_hold_down_ceiling = float(
            os.getenv("HFT_RSI_HOLD_DOWN_CEILING", os.getenv("HFT_RSI_HOLD_NO_CEILING", "60.0"))
        )
        self.rsi_allow_bypass_on_strong_edge = os.getenv(
            "HFT_RSI_ALLOW_BYPASS_STRONG_EDGE", "0"
        ) == "1"
        self.rsi_allow_bypass_on_aggressive_edge = os.getenv(
            "HFT_RSI_ALLOW_BYPASS_AGGRESSIVE_EDGE", "1"
        ) == "1"
        self.aggressive_entry_relax_speed = float(
            os.getenv("HFT_AGGRESSIVE_ENTRY_RELAX_SPEED", "-8.0")
        )
        self.aggressive_entry_relax_speed_down = float(
            os.getenv("HFT_AGGRESSIVE_ENTRY_RELAX_SPEED_DOWN", "30.0")
        )

        # --- Подтверждение входа (Entry Confirm) ---
        self.entry_confirm_age = float(os.getenv("HFT_ENTRY_CONFIRM_AGE_SEC", "0.1"))
        self.reversal_confirm_age = float(os.getenv("HFT_REVERSAL_CONFIRM_AGE_SEC", "0.2"))
        self.entry_extreme_min_edge = float(os.getenv("HFT_ENTRY_EXTREME_MIN_EDGE", "5.0"))
        self.entry_extreme_price_low = float(os.getenv("HFT_ENTRY_EXTREME_PRICE_LOW", "0.20"))
        self.entry_extreme_price_high = float(os.getenv("HFT_ENTRY_EXTREME_PRICE_HIGH", "0.80"))
        self.entry_depth_mult = float(os.getenv("HFT_ENTRY_DEPTH_MULT", "0.85"))
        self.entry_up_speed_min = float(os.getenv("HFT_ENTRY_UP_SPEED_MIN", "2.0"))
        self.entry_down_speed_max = float(os.getenv("HFT_ENTRY_DOWN_SPEED_MAX", "-2.0"))

        # --- Скорость и Акселерация ---
        self.speed_floor = float(os.getenv("HFT_SPEED_FLOOR", "0.02"))
        self.entry_accel_enabled = os.getenv("HFT_ENTRY_ACCEL_ENABLED", "0") == "1"
        self.entry_accel_min = float(os.getenv("HFT_ENTRY_ACCEL_MIN", "0.10"))
        self.reversal_speed_floor = float(os.getenv("HFT_REVERSAL_SPEED_FLOOR", "0.15"))

        # --- Динамический объем (Risk Management) ---
        self.dynamic_risk_per_tick_usd = float(os.getenv("HFT_DYNAMIC_RISK_PER_TICK_USD", "5.0"))
        self.dynamic_amount_min_usd = float(os.getenv("HFT_DYNAMIC_AMOUNT_MIN_USD", "20.0"))
        self.dynamic_amount_max_usd = float(os.getenv("HFT_DYNAMIC_AMOUNT_MAX_USD", "100.0"))
        self.dynamic_cheap_price_below = float(os.getenv("HFT_DYNAMIC_CHEAP_PRICE_BELOW", "0.30"))
        self.dynamic_rich_price_above = float(os.getenv("HFT_DYNAMIC_RICH_PRICE_ABOVE", "0.70"))
        self.dynamic_min_exec_price = float(os.getenv("HFT_DYNAMIC_MIN_EXEC_PRICE", "0.01"))
        self.dynamic_floor_notional_usd = float(os.getenv("HFT_DYNAMIC_FLOOR_NOTIONAL_USD", "30.0"))
        self.dynamic_amount_cheap_usd = float(os.getenv("HFT_DYNAMIC_AMOUNT_CHEAP_USD", "45.0"))
        self.dynamic_amount_rich_usd = float(os.getenv("HFT_DYNAMIC_AMOUNT_RICH_USD", "80.0"))

        self.deposit_usd = float(os.getenv("HFT_DEPOSIT_USD", "100.0"))
        self.trade_pct_of_deposit = float(os.getenv("HFT_TRADE_PCT_OF_DEPOSIT", "10"))
        self.fixed_trade_usd = float(os.getenv("HFT_DEFAULT_TRADE_USD", "10.0"))

        # --- Стакан (Orderbook) и Ликвидность ---
        self.book_move_entry_min = float(os.getenv("HFT_BOOK_MOVE_ENTRY_MIN", "0.0001"))
        self.book_move_stop_max = float(os.getenv("HFT_BOOK_MOVE_STOP_MAX", "0.0008"))
        self.book_stall_ticks_limit = int(os.getenv("HFT_BOOK_STALL_TICKS", "30"))
        self.max_entry_spread = float(os.getenv("HFT_MAX_ENTRY_SPREAD", "0.015")) # Не входим если спред > 1.5%
        self.max_entry_ask = float(os.getenv("HFT_MAX_ENTRY_ASK", "0.99"))
        self._prev_up_mid = None
        self._prev_down_mid = None
        self._book_stall_ticks = 0
        self.strong_edge_rsi_mult = float(os.getenv("HFT_STRONG_EDGE_RSI_MULT", "2.0"))
        self.aggressive_edge_mult = float(os.getenv("HFT_AGGRESSIVE_EDGE_MULT", "3.0"))
        self.entry_confirm_age_strong = float(os.getenv("HFT_ENTRY_CONFIRM_AGE_STRONG_SEC", "0.35"))
        self.wide_spread_min_edge = float(os.getenv("HFT_WIDE_SPREAD_MIN_EDGE", "12.0"))
        self.entry_liquidity_max_spread = float(os.getenv("HFT_ENTRY_LIQUIDITY_MAX_SPREAD", "0.03"))
        self.entry_momentum_alt_enabled = os.getenv("HFT_ENTRY_MOMENTUM_ALT_ENABLED", "1") == "1"

        # --- Задержка (Latency Guard) ---
        self.entry_max_latency_ms = float(os.getenv("HFT_ENTRY_MAX_LATENCY_MS", "1200.0"))
        self.entry_max_skew_ms = float(os.getenv("HFT_ENTRY_MAX_SKEW_MS", "550.0"))
        self.entry_max_ask_up_cap = float(os.getenv("HFT_ENTRY_MAX_ASK_UP", "0.92"))
        self.entry_max_ask_down_cap = float(os.getenv("HFT_ENTRY_MAX_ASK_DOWN", "0.92"))
        self.latency_high_ms = float(os.getenv("HFT_LATENCY_HIGH_MS", "400.0"))
        self.latency_high_edge_mult = float(os.getenv("HFT_LATENCY_HIGH_EDGE_MULT", "1.3"))
        self.expiry_tight_sec = float(os.getenv("HFT_EXPIRY_TIGHT_SEC", "30.0"))
        self.expiry_edge_mult = float(os.getenv("HFT_EXPIRY_EDGE_MULT", "2.0"))
        self.slot_interval_sec = float(os.getenv("HFT_SLOT_INTERVAL_SEC", "300.0"))
        self.no_entry_first_sec = float(os.getenv("HFT_NO_ENTRY_FIRST_SEC", "5.0"))
        self.no_entry_last_sec = float(os.getenv("HFT_NO_ENTRY_LAST_SEC", "20.0"))
        self.trend_flip_min_age_sec = float(os.getenv("HFT_TREND_FLIP_MIN_AGE_SEC", "2.0"))
        self.entry_rsi_slope_filter_enabled = os.getenv(
            "HFT_ENTRY_RSI_SLOPE_FILTER_ENABLED", "0"
        ) == "1"
        self.rsi_up_entry_max = float(os.getenv("HFT_RSI_UP_ENTRY_MAX", "50.0"))
        self.rsi_up_slope_min = float(os.getenv("HFT_RSI_UP_SLOPE_MIN", "0.0"))
        self.rsi_down_entry_min = float(os.getenv("HFT_RSI_DOWN_ENTRY_MIN", "50.0"))
        self.rsi_down_slope_max = float(os.getenv("HFT_RSI_DOWN_SLOPE_MAX", "0.0"))
        self.entry_low_speed_abs = float(os.getenv("HFT_ENTRY_LOW_SPEED_ABS", "1.0"))
        self.entry_low_speed_edge_mult = float(os.getenv("HFT_ENTRY_LOW_SPEED_EDGE_MULT", "2.0"))

        # --- Z-Score (Статистический вход) ---
        self.entry_zscore_trend_enabled = os.getenv("HFT_ENTRY_ZSCORE_TREND_ENABLED", "1") == "1"
        self.entry_zscore_strict_ticks = int(os.getenv("HFT_ENTRY_ZSCORE_STRICT_TICKS", "2"))

        # --- CEX Дисбаланс (Coinbase/Binance) ---
        self.entry_cex_imbalance_enabled = os.getenv("HFT_ENTRY_CEX_IMBALANCE_ENABLED", "1") == "1"
        self.cex_imbalance_up_min = float(os.getenv("HFT_CEX_IMBALANCE_UP_MIN", "0.60"))
        self.cex_imbalance_down_max = float(os.getenv("HFT_CEX_IMBALANCE_DOWN_MAX", "0.40"))

        # --- Anti-spoof: reject one-tick CEX spikes vs lagging Poly oracle. ---
        self.entry_max_edge_jump_pts = float(os.getenv("HFT_ENTRY_MAX_EDGE_JUMP_PTS", "8.0"))
        self.entry_aggressive_min_trend_age_sec = float(
            os.getenv("HFT_AGGRESSIVE_MIN_TREND_AGE_SEC", "0.0")
        )

        # --- Вспомогательные состояния ---
        self.soft_exits_enabled = True
        self.no_entry_guards = os.getenv("HFT_NO_ENTRY_GUARDS", "0") == "1"
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
        sell_abs = os.getenv("HFT_SELL_EDGE_ABS", "5.0")
        soft: dict[str, float | bool] = dict(latency_base)
        soft["noise_edge"] = float(os.getenv("HFT_SOFT_NOISE_EDGE", "1.2"))
        soft["buy_edge"] = float(os.getenv("HFT_SOFT_BUY_EDGE", "4.0"))
        soft["sell_edge"] = -float(os.getenv("HFT_SOFT_SELL_EDGE_ABS", sell_abs))
        soft["entry_confirm_age"] = float(os.getenv("HFT_SOFT_ENTRY_CONFIRM_AGE_SEC", "1.0"))
        soft["reversal_confirm_age"] = float(
            os.getenv("HFT_SOFT_REVERSAL_CONFIRM_AGE_SEC", str(latency_base["reversal_confirm_age"]))
        )
        soft["entry_confirm_age_strong"] = float(
            os.getenv("HFT_SOFT_ENTRY_CONFIRM_AGE_STRONG_SEC", "2.0")
        )
        soft["strong_edge_rsi_mult"] = float(os.getenv("HFT_SOFT_STRONG_EDGE_RSI_MULT", "2.5"))
        soft["aggressive_edge_mult"] = float(os.getenv("HFT_SOFT_AGGRESSIVE_EDGE_MULT", "8.0"))
        soft["entry_momentum_alt_enabled"] = (
            os.getenv("HFT_SOFT_ENTRY_MOMENTUM_ALT_ENABLED", "0") == "1"
        )
        soft["entry_max_edge_jump_pts"] = float(os.getenv("HFT_SOFT_ENTRY_MAX_EDGE_JUMP_PTS", "4.0"))
        soft["entry_max_latency_ms"] = float(os.getenv("HFT_SOFT_ENTRY_MAX_LATENCY_MS", "900.0"))
        soft["speed_floor"] = float(os.getenv("HFT_SOFT_SPEED_FLOOR", str(latency_base["speed_floor"])))
        soft["entry_low_speed_abs"] = float(os.getenv("HFT_SOFT_ENTRY_LOW_SPEED_ABS", "0.5"))
        soft["entry_low_speed_edge_mult"] = float(os.getenv("HFT_SOFT_ENTRY_LOW_SPEED_EDGE_MULT", "1.5"))
        soft["entry_aggressive_min_trend_age_sec"] = float(
            os.getenv("HFT_SOFT_ENTRY_AGGRESSIVE_MIN_TREND_AGE_SEC", "1.0")
        )
        soft["rsi_allow_bypass_on_aggressive_edge"] = (
            os.getenv("HFT_SOFT_RSI_ALLOW_BYPASS_AGGRESSIVE_EDGE", "0") == "1"
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
        lat = self._profile_snapshots["latency"]["entry_max_latency_ms"]
        soft = self._profile_snapshots["soft_flow"]["entry_max_latency_ms"]
        return max(float(lat), float(soft))

    def _entry_ask_allows_open(self, ask_px: float) -> bool:
        """Return False when best ask is at or above max entry price (no buys at 99¢+)."""
        return float(ask_px) < self.max_entry_ask

    def _entry_outcome_price_allows(self, side: str, up_ask: float, down_ask: float) -> bool:
        """Return False when the outcome ask is above the cheap-entry cap (poor R/R near $1)."""
        if side == "UP":
            cap = float(self.entry_max_ask_up_cap)
            if cap <= 0.0 or cap >= 1.0:
                return True
            return float(up_ask) <= cap
        if side == "DOWN":
            cap = float(self.entry_max_ask_down_cap)
            if cap <= 0.0 or cap >= 1.0:
                return True
            return float(down_ask) <= cap
        return True

    def _hold_met(self, hold_sec: float) -> bool:
        """Return True when min-hold delay does not apply or is satisfied."""
        return self.min_hold_sec <= 0.0 or hold_sec >= self.min_hold_sec

    def _pnl_tp_hold_allows(self, hold_sec: float) -> bool:
        """Return True when position was held long enough for percent-based PNL take profit."""
        if self.min_hold_sec <= 0.0 and self.pnl_tp_min_hold_sec <= 0.0:
            return True
        req = max(self.min_hold_sec, self.pnl_tp_min_hold_sec)
        return hold_sec >= req

    def _deposit_trade_notional(self) -> float:
        """Return target trade USD from fixed size and optional percent sizing."""
        dep = max(0.0, self.deposit_usd)
        fixed = max(0.0, self.fixed_trade_usd)
        pct = self.trade_pct_of_deposit
        if pct <= 0.0:
            return min(fixed, dep)
        pct_amount = dep * (pct / 100.0)
        chosen = max(fixed, pct_amount)
        return max(0.0, min(chosen, dep))

    def _tier_dynamic_amount(self, exec_price: float) -> float:
        """Compute notional from price tier and risk-per-tick before deposit cap."""
        px = float(exec_price)
        if px < self.dynamic_min_exec_price:
            return self.dynamic_floor_notional_usd
        if px < self.dynamic_cheap_price_below:
            return min(
                self.dynamic_amount_max_usd,
                max(self.dynamic_amount_min_usd, self.dynamic_amount_cheap_usd),
            )
        if px > self.dynamic_rich_price_above:
            return min(
                self.dynamic_amount_max_usd,
                max(self.dynamic_amount_min_usd, self.dynamic_amount_rich_usd),
            )
        tick = 0.01
        shares = self.dynamic_risk_per_tick_usd / tick
        amount = shares * px
        return min(
            self.dynamic_amount_max_usd,
            max(self.dynamic_amount_min_usd, amount),
        )

    def _calc_dynamic_amount(self, exec_price: float) -> float:
        """Size notional USD: tier estimate capped by deposit rules and dynamic min/max."""
        base = self._deposit_trade_notional()
        tier = self._tier_dynamic_amount(exec_price)
        amount = min(base, tier)
        amount = min(amount, self.dynamic_amount_max_usd)
        if base < self.dynamic_amount_min_usd:
            floor = base
        else:
            floor = self.dynamic_amount_min_usd
        return max(floor, amount)

    def get_last_rsi(self):
        """Return RSI of the last tick (fast price series)."""
        return self._last_rsi

    def get_rsi_v5_state(self):
        """Return RSI value, dynamic exit bands, and per-tick slope for logging."""
        return {
            "rsi": self._last_rsi,
            "upper": self._last_rsi_upper,
            "lower": self._last_rsi_lower,
            "slope": self._last_rsi_slope,
        }

    def _rsi_slope_per_tick(self):
        """Approximate RSI slope over the last few engine ticks."""
        if len(self._rsi_tick_history) < 3:
            return 0.0
        r = list(self._rsi_tick_history)
        return (r[-1] - r[-3]) / 2.0

    def _rsi_suppresses_soft_exit(self, position_side, rsi):
        """Block trend/speed/imbalance exits while RSI still matches the held thesis."""
        if position_side == "UP":
            return rsi >= self.rsi_hold_up_floor
        if position_side == "DOWN":
            return rsi <= self.rsi_hold_down_ceiling
        return False

    def _exit_rsi(self, rsi: float) -> float:
        """Clamp RSI for exit logic to limit spurious 100/0 from short price history."""
        hi = float(self.rsi_exit_clamp_high)
        lo = float(self.rsi_exit_clamp_low)
        if hi > lo:
            return min(max(float(rsi), lo), hi)
        return float(rsi)

    def _rsi_range_exit_triggered(
        self, position_side, current_rsi, unrealized, hold_sec: float = 0.0
    ):
        """Return True when RSI band exit is allowed (take-profit at band or fade exit past margin).

        Fade exits (RSI past band against the position) respect ``rsi_range_exit_min_hold_sec``
        to avoid immediate churn when RSI spikes on a short lookback. TP-at-band exits are unchanged.
        """
        margin = self.rsi_range_exit_band_margin
        min_p = self.rsi_range_exit_min_profit_usd
        tp_line, _ = self._pnl_target_and_stop_lines()
        min_hold = float(self.rsi_range_exit_min_hold_sec)
        fade_need_pos = os.getenv("HFT_RSI_RANGE_EXIT_FADE_REQUIRE_POSITIVE", "0") == "1"
        rx = self._exit_rsi(current_rsi)
        if position_side == "UP":
            if rx >= self.rsi_entry_up_high and unrealized >= tp_line:
                return True
            if rx <= self.rsi_entry_up_low - margin:
                if min_hold > 0.0 and hold_sec < min_hold:
                    return False
                cond = unrealized > min_p or rx <= self.rsi_extreme_low
                if fade_need_pos and unrealized <= 0.0:
                    return unrealized > min_p
                return cond
            return False
        if position_side == "DOWN":
            if rx <= self.rsi_entry_down_low and unrealized >= tp_line:
                return True
            if rx >= self.rsi_entry_down_high + margin:
                if min_hold > 0.0 and hold_sec < min_hold:
                    return False
                cond = unrealized > min_p or rx >= self.rsi_extreme_high
                if fade_need_pos and unrealized <= 0.0:
                    return unrealized > min_p
                return cond
            return False
        return False

    def can_trade(self):
        """Return True when risk limits allow new trade."""
        usd_exposure = abs(self.pnl.inventory * self.pnl.entry_price) if self.pnl.inventory > 0 else 0.0
        return usd_exposure < self.max_position

    def update_trend(self, fast_price, poly_mid):
        """Track crossing of target price and estimate trend speed/depth."""
        now = time.time()
        edge = fast_price - poly_mid
        self.edge_window.append((now, edge))

        sign = 1 if edge > 0 else -1 if edge < 0 else 0
        crossed = sign != 0 and self.last_edge_sign != 0 and sign != self.last_edge_sign
        if crossed:
            self.trend_since_ts = now
            self.trend_depth = abs(edge)
            self.trend_dir = "UP" if sign > 0 else "DOWN"
            logging.info("🔁 Trend cross: %s edge=%.2f", self.trend_dir, edge)
        elif sign != 0:
            if self.trend_since_ts == 0.0:
                self.trend_since_ts = now
                self.trend_dir = "UP" if sign > 0 else "DOWN"
            self.trend_depth = max(self.trend_depth, abs(edge))
        else:
            self.trend_dir = "FLAT"

        if sign != 0:
            self.last_edge_sign = sign

        speed = 0.0
        if len(self.edge_window) >= 2:
            t0, e0 = self.edge_window[-2]
            t1, e1 = self.edge_window[-1]
            dt = max(t1 - t0, 1e-6)
            speed = (e1 - e0) / dt
        age = now - self.trend_since_ts if self.trend_since_ts else 0.0
        return edge, speed, self.trend_depth, age, self.trend_dir

    def dynamic_edge_threshold(self, price_history, recent_pnl=0.0, latency_ms=0.0, extra_mult=1.0):
        """Return adaptive edge threshold in price units from recent volatility."""
        if not price_history or len(price_history) < 30:
            be, se = self.buy_edge, abs(self.sell_edge)
            return be * extra_mult, se * extra_mult
        arr = _price_array_for_rsi(price_history, 50)
        vol = float(np.std(arr))
        pnl_penalty = 1.15 if recent_pnl < 0 else 1.0
        lo = 0.0 if self.no_entry_guards else 2.0
        edge = max(lo, min(20.0, vol * 0.6 * pnl_penalty))
        edge *= float(extra_mult)
        return edge, edge

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
        self.apply_profile("latency")

    def _position_notional_usd(self):
        """Return absolute position notional in USD for percent-based TP/SL."""
        inv = float(self.pnl.inventory or 0.0)
        ep = float(self.pnl.entry_price or 0.0)
        return abs(inv * ep)

    def _pnl_target_and_stop_lines(self):
        """Return (take_profit_usd, stop_loss_usd) thresholds from percent or fixed env."""
        n = self._position_notional_usd()
        if self.pnl_tp_pct > 0.0:
            tp = n * self.pnl_tp_pct
        else:
            tp = self.target_profit_usd
        if self.pnl_sl_pct > 0.0:
            sl = n * self.pnl_sl_pct
        else:
            sl = self.stop_loss_usd
        return tp, sl

    def _is_strong_oracle_edge(self, edge: float) -> bool:
        """Return True when abs(fast-oracle edge) exceeds buy_edge * strong multiplier."""
        return abs(edge) >= self.buy_edge * self.strong_edge_rsi_mult

    def _is_aggressive_oracle_edge(self, edge: float) -> bool:
        """Return True for very large edge; used for logging and relaxed confirm age."""
        return abs(edge) >= self.buy_edge * self.aggressive_edge_mult

    def _latency_expiry_edge_multiplier(self, latency_ms: float, seconds_to_expiry: float | None) -> float:
        """Raise required edge when feed staleness_ms is high or the market slot is near expiry."""
        if self.no_entry_guards:
            return 1.0
        m = 1.0
        if latency_ms > self.latency_high_ms:
            m *= self.latency_high_edge_mult
        elif latency_ms > 250.0:
            m *= 1.10
        if (
            seconds_to_expiry is not None
            and seconds_to_expiry >= 0.0
            and seconds_to_expiry < self.expiry_tight_sec
        ):
            m *= self.expiry_edge_mult
        return m

    def _entry_slot_window_allows(self, seconds_to_expiry: float | None) -> bool:
        """Allow entries only outside first and last slot guard windows."""
        if seconds_to_expiry is None:
            return True
        sec_to_end = max(0.0, float(seconds_to_expiry))
        if self.no_entry_last_sec > 0.0 and sec_to_end <= self.no_entry_last_sec:
            return False
        interval = max(1.0, float(self.slot_interval_sec))
        sec_from_start = max(0.0, interval - sec_to_end)
        if self.no_entry_first_sec > 0.0 and sec_from_start <= self.no_entry_first_sec:
            return False
        return True

    def _low_speed_edge_multiplier(self, speed: float) -> float:
        """Raise required oracle edge when edge speed is low (fade / chop risk)."""
        if abs(float(speed)) < self.entry_low_speed_abs:
            return self.entry_low_speed_edge_mult
        return 1.0

    def entry_latency_allows_entry(self, latency_ms: float) -> bool:
        """Block entries when max feed staleness_ms exceeds entry_max_latency_ms."""
        if self.entry_max_latency_ms <= 0.0:
            return True
        return float(latency_ms) <= self.entry_max_latency_ms

    def entry_skew_allows_entry(self, skew_ms: float) -> bool:
        """Block entries when cross-feed skew is larger than the limit (0 disables the gate)."""
        if self.entry_max_skew_ms <= 0.0:
            return True
        return abs(float(skew_ms)) <= self.entry_max_skew_ms

    def entry_edge_jump_ok(self, edge_now: float) -> bool:
        """Return False when oracle edge jumps too far in one tick (bad CEX print vs Poly)."""
        if self.entry_max_edge_jump_pts <= 0.0:
            return True
        if len(self.edge_window) < 2:
            return True
        prev_edge = float(self.edge_window[-2][1])
        return abs(float(edge_now) - prev_edge) <= self.entry_max_edge_jump_pts

    def entry_aggressive_trend_age_ok(self, edge_now: float, trend_age: float) -> bool:
        """Require extra seconds after trend start when edge is in aggressive magnitude."""
        if self.entry_aggressive_min_trend_age_sec <= 0.0:
            return True
        if abs(edge_now) < self.buy_edge * self.aggressive_edge_mult:
            return True
        return float(trend_age) >= self.entry_aggressive_min_trend_age_sec

    def entry_trend_flip_settled_ok(self, trend_age: float) -> bool:
        """Avoid entries right after a trend cross (chop / saw)."""
        if self.trend_flip_min_age_sec <= 0.0:
            return True
        return float(trend_age) >= self.trend_flip_min_age_sec

    def entry_rsi_slope_allows(self, side: str, current_rsi: float) -> bool:
        """Require RSI oversold/overbought with favorable slope for UP/DOWN entries."""
        if not self.entry_rsi_slope_filter_enabled:
            return True
        slope = float(self._last_rsi_slope)
        if side == "UP":
            return current_rsi < self.rsi_up_entry_max and slope > self.rsi_up_slope_min
        if side == "DOWN":
            return current_rsi > self.rsi_down_entry_min and slope < self.rsi_down_slope_max
        return True

    def _record_entry_samples(self, speed: float, zscore: float) -> None:
        """Append latest trend speed and z-score for acceleration and z-trend filters."""
        self._speed_samples.append(float(speed))
        self._zscore_samples.append(float(zscore))

    def entry_liquidity_spread_ok(
        self,
        spread_up: float,
        spread_down: float,
        edge: float,
        trend_dir: str,
    ) -> bool:
        """Return False when UP/DOWN book spread is too wide unless oracle edge is very large."""
        if self.entry_liquidity_max_spread <= 0.0:
            return True
        mx = self.entry_liquidity_max_spread
        strong = abs(edge) >= self.wide_spread_min_edge
        if trend_dir == "UP":
            return spread_up <= mx or strong
        if trend_dir == "DOWN":
            return spread_down <= mx or strong
        return True

    def entry_speed_acceleration_ok(self, trend_dir: str, speed: float) -> bool:
        """Require edge-speed acceleration in the trade direction when enabled."""
        if not self.entry_accel_enabled:
            return True
        if len(self._speed_samples) < 4:
            return True
        prev = list(self._speed_samples)[-4:-1]
        acc = float(speed) - float(np.mean(prev))
        if trend_dir == "UP":
            return acc >= self.entry_accel_min
        if trend_dir == "DOWN":
            return acc <= -self.entry_accel_min
        return True

    def entry_zscore_trend_ok(self, trend_dir: str) -> bool:
        """Require z-score to move monotonically with the intended side for several ticks."""
        if not self.entry_zscore_trend_enabled:
            return True
        k = max(2, self.entry_zscore_strict_ticks)
        if len(self._zscore_samples) < k:
            return True
        zs = list(self._zscore_samples)[-k:]
        if trend_dir == "UP":
            return all(zs[i] < zs[i + 1] for i in range(len(zs) - 1))
        if trend_dir == "DOWN":
            return all(zs[i] > zs[i + 1] for i in range(len(zs) - 1))
        return True

    def entry_cex_bid_imbalance_ok(self, trend_dir: str, cex_bid_imbalance: float | None) -> bool:
        """Optional CEX bid-heavy filter when upstream passes bid/(bid+ask) for the fast feed."""
        if not self.entry_cex_imbalance_enabled or cex_bid_imbalance is None:
            return True
        x = float(cex_bid_imbalance)
        if trend_dir == "UP":
            return x >= self.cex_imbalance_up_min
        if trend_dir == "DOWN":
            return x <= self.cex_imbalance_down_max
        return True

    def _zscore_monotonic_for_direction(self, trend_dir: str) -> bool:
        """Return True if recent z-score ticks are strictly monotone in the trade direction."""
        k = max(2, self.entry_zscore_strict_ticks)
        if len(self._zscore_samples) < k:
            return False
        zs = list(self._zscore_samples)[-k:]
        if trend_dir == "UP":
            return all(zs[i] < zs[i + 1] for i in range(len(zs) - 1))
        if trend_dir == "DOWN":
            return all(zs[i] > zs[i + 1] for i in range(len(zs) - 1))
        return False

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
        if not self.pnl.is_good_regime():
            return None
        if not self.entry_momentum_alt_enabled:
            return None
        buy_edge_dyn, sell_edge_dyn = self.dynamic_edge_threshold(
            price_history=price_history,
            recent_pnl=recent_pnl,
            latency_ms=latency_ms,
            extra_mult=edge_mult,
        )
        lsm = self._low_speed_edge_multiplier(speed)
        buy_edge_dyn *= lsm
        sell_edge_dyn *= lsm
        if abs(edge) < self.noise_edge * 2.0:
            return None
        if not self._zscore_monotonic_for_direction(trend):
            return None
        if not self.entry_speed_acceleration_ok(trend, speed):
            return None
        if trend == "UP" and edge >= buy_edge_dyn * 0.85 and speed >= self.speed_floor:
            return "BUY_UP"
        if trend == "DOWN" and edge <= -sell_edge_dyn * 0.85 and speed <= -self.speed_floor:
            return "BUY_DOWN"
        return None

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
        if not self.pnl.is_good_regime():
            return None
        buy_edge_dyn, sell_edge_dyn = self.dynamic_edge_threshold(
            price_history=price_history,
            recent_pnl=recent_pnl,
            latency_ms=latency_ms,
            extra_mult=edge_mult,
        )
        lsm = self._low_speed_edge_multiplier(speed)
        buy_edge_dyn *= lsm
        sell_edge_dyn *= lsm
        if abs(edge) < self.noise_edge:
            return None
        strong = self._is_strong_oracle_edge(edge)
        aggressive = self._is_aggressive_oracle_edge(edge)
        if aggressive:
            now_ts = time.time()
            noise_min = float(os.getenv("HFT_AGGRESSIVE_ENTRY_LOG_MIN_SEC", "5.0"))
            if noise_min <= 0.0 or now_ts - self._last_entry_noise_log_ts >= noise_min:
                logging.info(
                    "🔥 AGGRESSIVE ENTRY candidate: edge=%.2f (>= %.1fx buy_edge=%.2f)",
                    edge,
                    self.aggressive_edge_mult,
                    self.buy_edge,
                )
                self._last_entry_noise_log_ts = now_ts
        age_need = self.entry_confirm_age_strong if strong else self.entry_confirm_age
        up_speed_ok = speed >= self.entry_up_speed_min or (
            strong and speed >= self.speed_floor
        )
        down_speed_ok = speed <= self.entry_down_speed_max or (
            strong and speed <= -self.speed_floor
        )
        if aggressive and trend == "UP" and edge >= buy_edge_dyn:
            up_speed_ok = up_speed_ok or speed >= self.aggressive_entry_relax_speed
        if aggressive and trend == "DOWN" and edge <= -sell_edge_dyn:
            down_speed_ok = down_speed_ok or speed <= self.aggressive_entry_relax_speed_down
        low = self.entry_extreme_price_low
        high = self.entry_extreme_price_high
        if (
            abs(edge) < self.entry_extreme_min_edge
            and not strong
            and (
                (up_mid > 0.0 and (up_mid < low or up_mid > high))
                or (down_mid > 0.0 and (down_mid < low or down_mid > high))
            )
        ):
            return None
        depth = self.trend_depth
        dm = self.entry_depth_mult
        if (
            trend == "UP"
            and age >= age_need
            and depth >= buy_edge_dyn * dm
            and edge >= buy_edge_dyn
            and speed >= self.speed_floor
            and up_speed_ok
        ):
            if len(self.edge_window) >= 2:
                last_edges = [e for _, e in list(self.edge_window)[-2:]]
                if not all(e > 0 for e in last_edges):
                    return None
            return "BUY_UP"
        speed_ok_down = speed <= -self.speed_floor or (
            aggressive
            and trend == "DOWN"
            and edge <= -sell_edge_dyn
            and speed <= self.aggressive_entry_relax_speed_down
        )
        if (
            trend == "DOWN"
            and age >= age_need
            and depth >= sell_edge_dyn * dm
            and edge <= -sell_edge_dyn
            and speed_ok_down
            and down_speed_ok
        ):
            if len(self.edge_window) >= 2:
                last_edges = [e for _, e in list(self.edge_window)[-2:]]
                if not all(e < 0 for e in last_edges):
                    return None
            return "BUY_DOWN"
        if abs(edge) >= self.buy_edge * self.aggressive_edge_mult * 1.2:
            sj_min_age = float(os.getenv("HFT_STRONG_JUMP_MIN_TREND_AGE_SEC", "0.0"))
            if sj_min_age > 0.0 and age < sj_min_age:
                return None
            now_ts = time.time()
            noise_min = float(os.getenv("HFT_AGGRESSIVE_ENTRY_LOG_MIN_SEC", "5.0"))
            if noise_min <= 0.0 or now_ts - self._last_entry_noise_log_ts >= noise_min:
                logging.info(
                    "🚀 STRONG JUMP detected edge=%.2f -> forcing early entry",
                    edge,
                )
                self._last_entry_noise_log_ts = now_ts
            if trend == "UP" and edge > 0:
                return "BUY_UP"
            if trend == "DOWN" and edge < 0:
                return "BUY_DOWN"
        return None

    def _is_reversal_confirmed(self, side, trend):
        """Return True when trend has clearly flipped against open position."""
        if side == "UP":
            return (
                trend["trend"] == "DOWN"
                and trend["age"] >= self.reversal_confirm_age
                and trend["speed"] <= -self.reversal_speed_floor
            )
        if side == "DOWN":
            return (
                trend["trend"] == "UP"
                and trend["age"] >= self.reversal_confirm_age
                and trend["speed"] >= self.reversal_speed_floor
            )
        return False

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

    def _book_move_while_holding(self, token_mid, prev_key):
        """Track mid while in position; return absolute tick move (for stall detection)."""
        if token_mid <= 0.0:
            return 0.0, False
        prev = getattr(self, prev_key)
        if prev is None:
            setattr(self, prev_key, token_mid)
            return 0.0, True
        move = token_mid - prev
        setattr(self, prev_key, token_mid)
        return abs(move), True

    def generate_live_signal(self, fast_price, poly_mid, zscore, price_history=None, recent_pnl=0.0, latency_ms=0.0):
        """Return entry side for live orders; call after process_tick in the same loop (trend already updated)."""
        _ = fast_price
        _ = poly_mid
        _ = zscore
        if price_history is None:
            price_history = []
        now = time.time()
        if now - self.last_trade_time < self.cooldown:
            return None
        tr = self.get_trend_state()
        return self._entry_candidate_from_state(
            tr["edge"],
            tr["age"],
            tr["trend"],
            tr["speed"],
            price_history,
            recent_pnl=recent_pnl,
            latency_ms=latency_ms,
            up_mid=0.0,
            down_mid=0.0,
            edge_mult=1.0,
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
        cex_bid_imbalance=None,
        skew_ms=0.0,
    ):
        if not fast_price or not poly_orderbook['ask']:
            return
        _ = lstm_forecast

        if self.pnl.inventory == 0 and not self.pnl.is_good_regime():
            _now = time.time()
            _regime_log_sec = float(os.getenv("HFT_REGIME_SKIP_LOG_MIN_SEC", "15.0"))
            if _regime_log_sec <= 0.0 or _now - self._last_regime_skip_log_ts >= _regime_log_sec:
                logging.info(
                    "Regime filter: recent performance is bad -> skip all entries"
                )
                self._last_regime_skip_log_ts = _now
            return None

        px = _price_array_for_rsi(price_history, self.rsi_price_len)
        current_rsi = float(compute_rsi(px, period=self.rsi_period))
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

        self.update_trend(fast_price, poly_mid)
        trend = self.get_trend_state()
        edge_now = trend["edge"]
        spread_up = max(0.0, up_ask - up_bid)
        spread_down = max(0.0, down_ask - down_bid)
        edge_mult = self._latency_expiry_edge_multiplier(latency_ms, seconds_to_expiry)
        self._record_entry_samples(trend["speed"], float(zscore))
        spread_gate_legacy = (
            self.max_entry_spread <= 0.0
            or spread_up <= self.max_entry_spread
            or abs(edge_now) >= self.wide_spread_min_edge
        )
        liquidity_ok = self.entry_liquidity_spread_ok(
            spread_up, spread_down, edge_now, trend["trend"]
        )
        speed_ok = self.entry_speed_acceleration_ok(trend["trend"], trend["speed"])
        z_ok = self.entry_zscore_trend_ok(trend["trend"])
        cex_ok = self.entry_cex_bid_imbalance_ok(trend["trend"], cex_bid_imbalance)
        entry_context_ok = speed_ok and z_ok and cex_ok
        chop_latency_ok = (
            self.entry_latency_allows_entry(latency_ms)
            and self.entry_skew_allows_entry(skew_ms)
            and self.entry_trend_flip_settled_ok(trend["age"])
        )
        edge_jump_ok = self.entry_edge_jump_ok(edge_now)
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
        if self.pnl.inventory == 0 and (time.time() - self.last_trade_time >= self.cooldown):
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
            if (
                signal is not None
                and not spread_gate
            ):
                _now = time.time()
                _fg_log = float(os.getenv("HFT_FEED_GATE_LOG_MIN_SEC", "30.0"))
                if (
                    _fg_log > 0.0
                    and _now - self._last_feed_gate_log_ts >= _fg_log
                ):
                    logging.info(
                        "Entry blocked by spread_gate: signal=%s stale=%.0fms skew=%.0fms "
                        "spread_legacy=%s liq=%s speed_z_cex=%s/%s/%s chop_lat_skew_flip=%s/%s/%s "
                        "edge_jump=%s agr_age=%s",
                        signal,
                        latency_ms,
                        skew_ms,
                        spread_gate_legacy,
                        liquidity_ok,
                        speed_ok,
                        z_ok,
                        cex_ok,
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
        _cap_log_sec = float(os.getenv("HFT_ENTRY_CAP_DENY_LOG_SEC", "20.0"))
        if (
            self.pnl.inventory == 0
            and signal == "BUY_UP"
            and not self._entry_outcome_price_allows("UP", up_ask, down_ask)
            and _cap_log_sec > 0.0
            and _t_cap - self._last_entry_cap_deny_log_ts >= _cap_log_sec
        ):
            logging.info(
                "Entry blocked: BUY_UP up_ask=%.4f above HFT_ENTRY_MAX_ASK_UP=%.4f.",
                up_ask,
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
            logging.info(
                "Entry blocked: BUY_DOWN down_ask=%.4f above HFT_ENTRY_MAX_ASK_DOWN=%.4f.",
                down_ask,
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
            and meta_enabled
        ):
            _notional_up = self._calc_dynamic_amount(up_ask)
            open_event = await self.execute("BUY_UP", up_ask, _notional_up)
            if not open_event:
                logging.warning(
                    "SIM BUY_UP skipped: no fill (balance=%.2f notional=%.2f ask=%.4f).",
                    float(self.pnl.balance),
                    float(_notional_up),
                    float(up_ask),
                )
            else:
                self.last_trade_time = time.time()
                self.entry_poly_mid = poly_mid
                self.entry_fast_price = fast_price
                self.entry_time = time.time()
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
            and meta_enabled
        ):
            _notional_dn = self._calc_dynamic_amount(down_ask)
            open_event = await self.execute("BUY_DOWN", down_ask, _notional_dn)
            if not open_event:
                logging.warning(
                    "SIM BUY_DOWN skipped: no fill (balance=%.2f notional=%.2f ask=%.4f).",
                    float(self.pnl.balance),
                    float(_notional_dn),
                    float(down_ask),
                )
            else:
                self.last_trade_time = time.time()
                self.entry_poly_mid = poly_mid
                self.entry_fast_price = fast_price
                self.entry_time = time.time()
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

        if self.pnl.inventory > 0:
            now = time.time()
            hold_sec = now - self.entry_time if self.entry_time else 0.0
            poly_move = 0.0
            if self.entry_poly_mid and self.entry_poly_mid > 0:
                poly_move = (poly_mid - self.entry_poly_mid) / self.entry_poly_mid

            if seconds_to_expiry is not None and seconds_to_expiry < 45:
                pos_side = self.pnl.position_side or "UP"
                reached_99c = (
                    (down_bid >= 0.99 or down_ask >= 0.99)
                    if pos_side == "DOWN"
                    else (up_bid >= 0.99 or up_ask >= 0.99)
                )
                if reached_99c:
                    logging.warning(
                        "⚠️ СЛОТ ЗАКАНЧИВАЕТСЯ (%.0fс) и достигнуты 99¢ -> закрываем по 99¢",
                        float(seconds_to_expiry),
                    )
                    exit_price = 0.99
                    close_event = await self.execute("SELL", exit_price)
                    ce = close_event or {}
                    _pk = ce.get("performance_key")
                    result = {
                        "event": "CLOSE",
                        "reason": "SLOT_EXPIRY_99C",
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
                    self.entry_fast_price = None
                    self.entry_time = 0.0
                    self.position_trend = "FLAT"
                    self.entry_context = {}
                    self._book_stall_ticks = 0
                    self._prev_up_mid = None
                    self._prev_down_mid = None
                    return result
                _now = time.time()
                _slot_log_sec = float(os.getenv("HFT_SLOT_EXPIRY_INFO_LOG_MIN_SEC", "8.0"))
                if _slot_log_sec <= 0.0 or _now - self._last_slot_expiry_info_log_ts >= _slot_log_sec:
                    logging.info(
                        "⏳ СЛОТ ЗАКАНЧИВАЕТСЯ (%.0fс), но 99¢ не достигнуты -> продолжаем плановый выход.",
                        float(seconds_to_expiry),
                    )
                    self._last_slot_expiry_info_log_ts = _now

            if self.pnl.position_side == "DOWN":
                reaction_confirmed = self._hold_met(hold_sec) and poly_move <= -self.poly_take_profit_move
                protective_stop = self._hold_met(hold_sec) and poly_move >= self.poly_stop_move
            else:
                reaction_confirmed = self._hold_met(hold_sec) and poly_move >= self.poly_take_profit_move
                protective_stop = self._hold_met(hold_sec) and poly_move <= -self.poly_stop_move
            unrealized = self.pnl.get_unrealized_pnl(poly_orderbook)
            _, sl_line = self._pnl_target_and_stop_lines()
            pnl_sl = self._hold_met(hold_sec) and unrealized <= -sl_line
            pos_side = self.pnl.position_side or "UP"
            rsi_x = self._exit_rsi(current_rsi)
            if pos_side == "DOWN":
                internal_reversal = (
                    imbalance >= 0.55
                    or rsi_x >= upper_b
                    or self._last_rsi_slope >= self.rsi_slope_down_exit
                )
            else:
                internal_reversal = (
                    imbalance <= 0.45
                    or rsi_x <= lower_b
                    or self._last_rsi_slope <= self.rsi_slope_up_exit
                )
            reaction_tp_confirmed = reaction_confirmed and internal_reversal
            rsi_range_exit = self._rsi_range_exit_triggered(
                pos_side,
                current_rsi,
                unrealized,
                hold_sec,
            )

            should_close = (
                reaction_tp_confirmed
                or protective_stop
                or rsi_range_exit
                or pnl_sl
            )
            if should_close:
                reason = "REACTION_TP"
                if protective_stop:
                    reason = "REACTION_STOP"
                elif rsi_range_exit:
                    reason = "RSI_RANGE_EXIT"
                elif pnl_sl:
                    reason = "PNL_SL"
                logging.info(
                    "📌 Exit reason=%s hold=%.1fs poly_move=%.4f edge=%.2f pnl=%.2f imb=%.2f "
                    "rsi=%.1f band=[%.1f,%.1f] slope=%+.2f",
                    reason,
                    hold_sec,
                    poly_move,
                    fast_price - poly_mid,
                    unrealized,
                    imbalance,
                    current_rsi,
                    lower_b,
                    upper_b,
                    self._last_rsi_slope,
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
                self.entry_fast_price = None
                self.entry_time = 0.0
                self.position_trend = "FLAT"
                self.entry_context = {}
                self._book_stall_ticks = 0
                self._prev_up_mid = None
                self._prev_down_mid = None
                return result

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


LegacyLatencyEngine = HFTEngine