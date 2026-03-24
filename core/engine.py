import numpy as np
import time
import logging
import os
from collections import deque

from ml.indicators import compute_rsi, dynamic_rsi_bands


class HFTEngine:
    """Signal, risk, and execution engine for Polymarket latency strategy."""

    def __init__(self, pnl_tracker, is_test_mode=True):
        self.pnl = pnl_tracker
        self.is_test_mode = is_test_mode
        
        # --- Базовый Edge (в пунктах цены) ---
        self.noise_edge = float(os.getenv("HFT_NOISE_EDGE", "0.5"))   # Игнорим шум менее 0.5 пункта
        self.buy_edge = float(os.getenv("HFT_BUY_EDGE", "2.0"))      # Вход при разнице > 2.0
        self.sell_edge = -float(os.getenv("HFT_SELL_EDGE_ABS", "2.0"))
        
        # --- Тайминги и объемы ---
        self.cooldown = float(os.getenv("HFT_COOLDOWN_SEC", "0.1"))
        self.last_trade_time = 0.0
        self.max_position = float(os.getenv("HFT_MAX_POSITION_USD", "100.0"))
        self.trade_amount_usd = float(os.getenv("HFT_DEFAULT_TRADE_USD", "50.0"))
        self.min_hold_sec = float(os.getenv("HFT_MIN_HOLD_SEC", "1.0"))
        self.reaction_timeout_sec = float(os.getenv("HFT_REACTION_TIMEOUT_SEC", "10.0"))
        self.entry_poly_mid = None
        self.entry_fast_price = None
        self.entry_time = 0.0

        # --- Тейки и Стопы (в пунктах и USD) ---
        self.poly_take_profit_move = float(os.getenv("HFT_POLY_TP_MOVE", "0.0030"))
        self.poly_stop_move = float(os.getenv("HFT_POLY_SL_MOVE", "0.0025"))
        self.target_profit_usd = float(os.getenv("HFT_TARGET_PROFIT_USD", "2.5"))
        self.stop_loss_usd = float(os.getenv("HFT_STOP_LOSS_USD", "1.5"))
        self.pnl_tp_pct = float(os.getenv("HFT_PNL_TP_PERCENT", "0.05"))
        self.pnl_sl_pct = float(os.getenv("HFT_PNL_SL_PERCENT", "0.02"))

        # --- RSI логика ---
        self.rsi_period = 14
        self._last_rsi = 50.0
        self.rsi_entry_yes_low = float(os.getenv("HFT_RSI_ENTRY_YES_LOW", "20.0"))
        self.rsi_entry_yes_high = float(os.getenv("HFT_RSI_ENTRY_YES_HIGH", "80.0"))
        self.rsi_entry_no_low = float(os.getenv("HFT_RSI_ENTRY_NO_LOW", "20.0"))
        self.rsi_entry_no_high = float(os.getenv("HFT_RSI_ENTRY_NO_HIGH", "80.0"))
        
        # Выходы по RSI
        self.rsi_exit_upper_base = float(os.getenv("HFT_RSI_EXIT_UPPER_BASE", "85"))
        self.rsi_exit_lower_base = float(os.getenv("HFT_RSI_EXIT_LOWER_BASE", "15"))
        self.rsi_range_exit_min_profit_usd = float(os.getenv("HFT_RSI_RANGE_EXIT_MIN_PROFIT_USD", "0.3"))
        self.rsi_range_exit_band_margin = float(os.getenv("HFT_RSI_RANGE_EXIT_BAND_MARGIN", "10.0"))
        self.rsi_extreme_high = float(os.getenv("HFT_RSI_EXTREME_HIGH", "90"))
        self.rsi_extreme_low = float(os.getenv("HFT_RSI_EXTREME_LOW", "10"))
        self.rsi_band_vol_k = 0.12
        self.rsi_range_exit_profit_frac = float(os.getenv("HFT_RSI_RANGE_EXIT_PROFIT_FRAC", "0.6"))

        # --- RSI Slope (Наклон) ---
        self.rsi_slope_exit_enabled = os.getenv("HFT_RSI_SLOPE_EXIT_ENABLED", "1") == "1"
        self.rsi_slope_yes_exit = -2.0 # Выходим из YES если RSI резко падает
        self.rsi_slope_no_exit = 2.0  # Выходим из NO если RSI резко растет
        self._rsi_tick_history = deque(maxlen=10)
        self._last_rsi_upper = 70.0
        self._last_rsi_lower = 30.0
        self._last_rsi_slope = 0.0
        self.rsi_hold_yes_floor = float(os.getenv("HFT_RSI_HOLD_YES_FLOOR", "40.0"))
        self.rsi_hold_no_ceiling = float(os.getenv("HFT_RSI_HOLD_NO_CEILING", "60.0"))

        # --- Подтверждение входа (Entry Confirm) ---
        self.entry_confirm_age = float(os.getenv("HFT_ENTRY_CONFIRM_AGE_SEC", "0.1"))
        self.reversal_confirm_age = float(os.getenv("HFT_REVERSAL_CONFIRM_AGE_SEC", "0.2"))
        self.entry_extreme_min_edge = float(os.getenv("HFT_ENTRY_EXTREME_MIN_EDGE", "5.0"))
        self.entry_extreme_price_low = float(os.getenv("HFT_ENTRY_EXTREME_PRICE_LOW", "0.20"))
        self.entry_extreme_price_high = float(os.getenv("HFT_ENTRY_EXTREME_PRICE_HIGH", "0.80"))
        self.entry_depth_mult = float(os.getenv("HFT_ENTRY_DEPTH_MULT", "0.9"))
        self.entry_up_speed_min = float(os.getenv("HFT_ENTRY_UP_SPEED_MIN", "2.5"))
        self.entry_down_speed_max = float(os.getenv("HFT_ENTRY_DOWN_SPEED_MAX", "-2.5"))

        # --- Скорость и Акселерация ---
        self.speed_floor = float(os.getenv("HFT_SPEED_FLOOR", "0.02"))
        self.entry_accel_enabled = os.getenv("HFT_ENTRY_ACCEL_ENABLED", "1") == "1"
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

        # --- Стакан (Orderbook) и Ликвидность ---
        self.book_move_entry_min = float(os.getenv("HFT_BOOK_MOVE_ENTRY_MIN", "0.0001"))
        self.book_move_stop_max = float(os.getenv("HFT_BOOK_MOVE_STOP_MAX", "0.0008"))
        self.book_stall_ticks_limit = int(os.getenv("HFT_BOOK_STALL_TICKS", "30"))
        self.max_entry_spread = float(os.getenv("HFT_MAX_ENTRY_SPREAD", "0.015")) # Не входим если спред > 1.5%
        self._prev_yes_mid = None
        self._prev_no_mid = None
        self._book_stall_ticks = 0
        self.strong_edge_rsi_mult = float(os.getenv("HFT_STRONG_EDGE_RSI_MULT", "2.0"))
        self.aggressive_edge_mult = float(os.getenv("HFT_AGGRESSIVE_EDGE_MULT", "3.0"))
        self.entry_confirm_age_strong = float(os.getenv("HFT_ENTRY_CONFIRM_AGE_STRONG_SEC", "0.35"))
        self.wide_spread_min_edge = float(os.getenv("HFT_WIDE_SPREAD_MIN_EDGE", "12.0"))
        self.entry_liquidity_max_spread = float(os.getenv("HFT_ENTRY_LIQUIDITY_MAX_SPREAD", "0.03"))
        self.entry_momentum_alt_enabled = os.getenv("HFT_ENTRY_MOMENTUM_ALT_ENABLED", "1") == "1"

        # --- Задержка (Latency Guard) ---
        self.entry_max_latency_ms = float(os.getenv("HFT_ENTRY_MAX_LATENCY_MS", "600.0"))
        self.latency_high_ms = float(os.getenv("HFT_LATENCY_HIGH_MS", "400.0"))
        self.latency_high_edge_mult = float(os.getenv("HFT_LATENCY_HIGH_EDGE_MULT", "1.3"))
        self.expiry_tight_sec = float(os.getenv("HFT_EXPIRY_TIGHT_SEC", "30.0"))
        self.expiry_edge_mult = float(os.getenv("HFT_EXPIRY_EDGE_MULT", "2.0"))
        self.trend_flip_min_age_sec = float(os.getenv("HFT_TREND_FLIP_MIN_AGE_SEC", "2.0"))
        self.entry_rsi_slope_filter_enabled = os.getenv(
            "HFT_ENTRY_RSI_SLOPE_FILTER_ENABLED", "1"
        ) == "1"
        self.rsi_up_entry_max = float(os.getenv("HFT_RSI_UP_ENTRY_MAX", "30.0"))
        self.rsi_up_slope_min = float(os.getenv("HFT_RSI_UP_SLOPE_MIN", "0.0"))
        self.rsi_down_entry_min = float(os.getenv("HFT_RSI_DOWN_ENTRY_MIN", "70.0"))
        self.rsi_down_slope_max = float(os.getenv("HFT_RSI_DOWN_SLOPE_MAX", "0.0"))
        self.entry_low_speed_abs = float(os.getenv("HFT_ENTRY_LOW_SPEED_ABS", "1.0"))
        self.entry_low_speed_edge_mult = float(os.getenv("HFT_ENTRY_LOW_SPEED_EDGE_MULT", "2.0"))

        # --- Z-Score (Статистический вход) ---
        self.entry_zscore_trend_enabled = os.getenv("HFT_ENTRY_ZSCORE_TREND_ENABLED", "1") == "1"
        self.entry_zscore_strict_ticks = int(os.getenv("HFT_ENTRY_ZSCORE_STRICT_TICKS", "5"))

        # --- CEX Дисбаланс (Coinbase/Binance) ---
        self.entry_cex_imbalance_enabled = os.getenv("HFT_ENTRY_CEX_IMBALANCE_ENABLED", "1") == "1"
        self.cex_imbalance_up_min = float(os.getenv("HFT_CEX_IMBALANCE_UP_MIN", "0.60"))
        self.cex_imbalance_down_max = float(os.getenv("HFT_CEX_IMBALANCE_DOWN_MAX", "0.40"))

        # --- Вспомогательные состояния ---
        self.soft_exits_enabled = True
        self.no_entry_guards = False # ВКЛЮЧАЕМ защиту (False значит guards активны)
        self.edge_window = deque(maxlen=120)
        self.last_edge_sign = 0
        self.trend_dir = "FLAT"
        self.trend_since_ts = 0.0
        self.trend_depth = 0.0
        self._speed_samples = deque(maxlen=12)
        self._zscore_samples = deque(maxlen=12)
        self.position_trend = "FLAT"
        self.entry_context = {}

    def _hold_met(self, hold_sec: float) -> bool:
        """Return True when min-hold delay does not apply or is satisfied."""
        return self.min_hold_sec <= 0.0 or hold_sec >= self.min_hold_sec

    def _calc_dynamic_amount(self, exec_price: float) -> float:
        """Size notional USD conservatively: cheap tokens use smaller $, mid via risk-per-tick."""
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
        if position_side in ("UP", "YES"):
            return rsi >= self.rsi_hold_yes_floor
        if position_side in ("DOWN", "NO"):
            return rsi <= self.rsi_hold_no_ceiling
        return False

    def _rsi_range_exit_triggered(self, position_side, current_rsi, unrealized):
        """Return True when RSI band exit is allowed (take-profit at band or fade exit past margin)."""
        margin = self.rsi_range_exit_band_margin
        min_p = self.rsi_range_exit_min_profit_usd
        tp_line, _ = self._pnl_target_and_stop_lines()
        if position_side in ("UP", "YES"):
            if current_rsi >= self.rsi_entry_yes_high and unrealized >= tp_line:
                return True
            if current_rsi <= self.rsi_entry_yes_low - margin:
                return unrealized > min_p or current_rsi <= self.rsi_extreme_low
            return False
        if position_side in ("DOWN", "NO"):
            if current_rsi <= self.rsi_entry_no_low and unrealized >= tp_line:
                return True
            if current_rsi >= self.rsi_entry_no_high + margin:
                return unrealized > min_p or current_rsi >= self.rsi_extreme_high
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
        arr = np.array(price_history[-50:], dtype=np.float64)
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
        self._prev_yes_mid = None
        self._prev_no_mid = None
        self._book_stall_ticks = 0
        self._speed_samples.clear()
        self._zscore_samples.clear()

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
        """Raise required edge when latency is high or the market slot is near expiry."""
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

    def _low_speed_edge_multiplier(self, speed: float) -> float:
        """Raise required oracle edge when edge speed is low (fade / chop risk)."""
        if abs(float(speed)) < self.entry_low_speed_abs:
            return self.entry_low_speed_edge_mult
        return 1.0

    def entry_latency_allows_entry(self, latency_ms: float) -> bool:
        """Block entries when Poly vs fast-feed latency is too high (stale book)."""
        if self.entry_max_latency_ms <= 0.0:
            return True
        return float(latency_ms) <= self.entry_max_latency_ms

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
        if side in ("UP", "YES"):
            return current_rsi < self.rsi_up_entry_max and slope > self.rsi_up_slope_min
        if side in ("DOWN", "NO"):
            return current_rsi > self.rsi_down_entry_min and slope < self.rsi_down_slope_max
        return True

    def _record_entry_samples(self, speed: float, zscore: float) -> None:
        """Append latest trend speed and z-score for acceleration and z-trend filters."""
        self._speed_samples.append(float(speed))
        self._zscore_samples.append(float(zscore))

    def entry_liquidity_spread_ok(
        self,
        spread_yes: float,
        spread_no: float,
        edge: float,
        trend_dir: str,
    ) -> bool:
        """Return False when YES/NO book spread is too wide unless oracle edge is very large."""
        if self.entry_liquidity_max_spread <= 0.0:
            return True
        mx = self.entry_liquidity_max_spread
        strong = abs(edge) >= self.wide_spread_min_edge
        if trend_dir == "UP":
            return spread_yes <= mx or strong
        if trend_dir == "DOWN":
            return spread_no <= mx or strong
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
        k = max(3, self.entry_zscore_strict_ticks)
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
        k = max(3, self.entry_zscore_strict_ticks)
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
        yes_mid=0.0,
        no_mid=0.0,
        edge_mult=1.0,
    ):
        """Return BUY_UP/BUY_DOWN/None from trend vs oracle (no cooldown / no update_trend here)."""
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
        if self._is_aggressive_oracle_edge(edge):
            logging.info(
                "🔥 AGGRESSIVE ENTRY candidate: edge=%.2f (>= %.1fx buy_edge=%.2f)",
                edge,
                self.aggressive_edge_mult,
                self.buy_edge,
            )
        age_need = self.entry_confirm_age_strong if strong else self.entry_confirm_age
        up_speed_ok = speed >= self.entry_up_speed_min or (
            strong and speed >= self.speed_floor
        )
        down_speed_ok = speed <= self.entry_down_speed_max or (
            strong and speed <= -self.speed_floor
        )
        low = self.entry_extreme_price_low
        high = self.entry_extreme_price_high
        if (
            abs(edge) < self.entry_extreme_min_edge
            and not strong
            and (
                (yes_mid > 0.0 and (yes_mid < low or yes_mid > high))
                or (no_mid > 0.0 and (no_mid < low or no_mid > high))
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
            return "BUY_UP"
        if (
            trend == "DOWN"
            and age >= age_need
            and depth >= sell_edge_dyn * dm
            and edge <= -sell_edge_dyn
            and speed <= -self.speed_floor
            and down_speed_ok
        ):
            return "BUY_DOWN"
        return None

    def _is_reversal_confirmed(self, side, trend):
        """Return True when trend has clearly flipped against open position."""
        if side in ("UP", "YES"):
            return (
                trend["trend"] == "DOWN"
                and trend["age"] >= self.reversal_confirm_age
                and trend["speed"] <= -self.reversal_speed_floor
            )
        if side in ("DOWN", "NO"):
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
            yes_mid=0.0,
            no_mid=0.0,
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
    ):
        if not fast_price or not poly_orderbook['ask']:
            return
        _ = lstm_forecast

        px = np.array(price_history)
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
        if self.pnl.inventory > 0 and self.pnl.position_side in ("DOWN", "NO") and db_top + da_top > 0.0:
            imbalance = db_top / (db_top + da_top + 1e-9)
        else:
            imbalance = bid_size / (bid_size + ask_size + 1e-9)

        yes_ask = float(poly_orderbook["ask"])
        yes_bid = float(poly_orderbook["bid"])
        yes_mid = (yes_bid + yes_ask) * 0.5
        down_bid_raw = float(poly_orderbook.get("down_bid", 0.0))
        down_ask_raw = float(poly_orderbook.get("down_ask", 0.0))
        if 0.0 < down_bid_raw < down_ask_raw <= 1.0:
            no_bid = down_bid_raw
            no_ask = down_ask_raw
        else:
            no_ask = max(0.01, min(0.99, 1.0 - yes_bid))
            no_bid = max(0.01, min(0.99, 1.0 - yes_ask))
        no_mid = (no_bid + no_ask) * 0.5

        self.update_trend(fast_price, poly_mid)
        trend = self.get_trend_state()
        edge_now = trend["edge"]
        spread_yes = max(0.0, yes_ask - yes_bid)
        spread_no = max(0.0, no_ask - no_bid)
        edge_mult = self._latency_expiry_edge_multiplier(latency_ms, seconds_to_expiry)
        self._record_entry_samples(trend["speed"], float(zscore))
        spread_gate_legacy = (
            self.max_entry_spread <= 0.0
            or spread_yes <= self.max_entry_spread
            or abs(edge_now) >= self.wide_spread_min_edge
        )
        liquidity_ok = self.entry_liquidity_spread_ok(
            spread_yes, spread_no, edge_now, trend["trend"]
        )
        entry_context_ok = (
            self.entry_speed_acceleration_ok(trend["trend"], trend["speed"])
            and self.entry_zscore_trend_ok(trend["trend"])
            and self.entry_cex_bid_imbalance_ok(trend["trend"], cex_bid_imbalance)
        )
        chop_latency_ok = self.entry_latency_allows_entry(
            latency_ms
        ) and self.entry_trend_flip_settled_ok(trend["age"])
        if self.no_entry_guards:
            spread_gate = True
        else:
            spread_gate = (
                spread_gate_legacy
                and liquidity_ok
                and entry_context_ok
                and chop_latency_ok
            )
        signal = None
        if self.pnl.inventory == 0 and (time.time() - self.last_trade_time >= self.cooldown):
            signal = self._entry_candidate_from_state(
                edge_now,
                trend["age"],
                trend["trend"],
                trend["speed"],
                price_history,
                recent_pnl=recent_pnl,
                latency_ms=latency_ms,
                yes_mid=yes_mid,
                no_mid=no_mid,
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

        if self.pnl.inventory == 0:
            abs_move_yes, aligned_yes = self._book_move_for_outcome(yes_mid, "_prev_yes_mid", want_up=True)
            abs_move_no, aligned_no = self._book_move_for_outcome(no_mid, "_prev_no_mid", want_up=True)
            if self.book_move_entry_min <= 0.0:
                book_entry_yes = True
                book_entry_no = True
            else:
                book_entry_yes = aligned_yes and abs_move_yes >= self.book_move_entry_min
                book_entry_no = aligned_no and abs_move_no >= self.book_move_entry_min
        else:
            book_entry_yes = False
            book_entry_no = False

        strong_rsi = self._is_strong_oracle_edge(edge_now)
        if self.no_entry_guards:
            rsi_ok_up = True
            rsi_ok_down = True
            book_ok_yes = True
            book_ok_no = True
        elif self.entry_rsi_slope_filter_enabled:
            rsi_ok_up = strong_rsi or self.entry_rsi_slope_allows("UP", current_rsi)
            rsi_ok_down = strong_rsi or self.entry_rsi_slope_allows("DOWN", current_rsi)
            book_ok_yes = book_entry_yes or strong_rsi
            book_ok_no = book_entry_no or strong_rsi
        else:
            rsi_ok_up = strong_rsi or (
                self.rsi_entry_yes_low < current_rsi < self.rsi_entry_yes_high
            )
            rsi_ok_down = strong_rsi or (
                self.rsi_entry_no_low < current_rsi < self.rsi_entry_no_high
            )
            book_ok_yes = book_entry_yes or strong_rsi
            book_ok_no = book_entry_no or strong_rsi

        if (
            signal == "BUY_UP"
            and self.pnl.inventory == 0
            and self.can_trade()
            and rsi_ok_up
            and book_ok_yes
            and spread_gate
            and meta_enabled
        ):
            open_event = await self.execute("BUY_UP", yes_ask, self._calc_dynamic_amount(yes_ask))
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
                "entry_book_px": float((open_event or {}).get("book_px") or 0.0),
                "entry_exec_px": float((open_event or {}).get("exec_px") or 0.0),
                "shares_bought": float((open_event or {}).get("shares_filled") or 0.0),
                "cost_usd": float((open_event or {}).get("amount_usd") or 0.0),
                "entry_yes_bid": yes_bid,
                "entry_yes_ask": yes_ask,
                "entry_no_bid": no_bid,
                "entry_no_ask": no_ask,
            }
            logging.info(
                "🧭 Entry context: poly_mid=%.4f fast=%.2f edge=%.2f trend=%s imb=%.2f",
                poly_mid,
                fast_price,
                fast_price - poly_mid,
                self.position_trend,
                imbalance,
            )
            return {"event": "OPEN", "side": "UP", "trade": open_event}

        if (
            signal == "BUY_DOWN"
            and self.pnl.inventory == 0
            and self.can_trade()
            and rsi_ok_down
            and book_ok_no
            and spread_gate
            and meta_enabled
        ):
            open_event = await self.execute("BUY_DOWN", no_ask, self._calc_dynamic_amount(no_ask))
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
                "entry_book_px": float((open_event or {}).get("book_px") or 0.0),
                "entry_exec_px": float((open_event or {}).get("exec_px") or 0.0),
                "shares_bought": float((open_event or {}).get("shares_filled") or 0.0),
                "cost_usd": float((open_event or {}).get("amount_usd") or 0.0),
                "entry_yes_bid": yes_bid,
                "entry_yes_ask": yes_ask,
                "entry_no_bid": no_bid,
                "entry_no_ask": no_ask,
            }
            logging.info(
                "🧭 Entry context: side=BUY_DOWN poly_mid=%.4f fast=%.2f edge=%.2f trend=%s imb=%.2f",
                poly_mid,
                fast_price,
                fast_price - poly_mid,
                self.position_trend,
                imbalance,
            )
            return {"event": "OPEN", "side": "DOWN", "trade": open_event}

        if self.pnl.inventory > 0:
            now = time.time()
            hold_sec = now - self.entry_time if self.entry_time else 0.0
            poly_move = 0.0
            if self.entry_poly_mid and self.entry_poly_mid > 0:
                poly_move = (poly_mid - self.entry_poly_mid) / self.entry_poly_mid

            if self.pnl.position_side in ("DOWN", "NO"):
                reaction_confirmed = self._hold_met(hold_sec) and poly_move <= -self.poly_take_profit_move
                protective_stop = self._hold_met(hold_sec) and poly_move >= self.poly_stop_move
            else:
                reaction_confirmed = self._hold_met(hold_sec) and poly_move >= self.poly_take_profit_move
                protective_stop = self._hold_met(hold_sec) and poly_move <= -self.poly_stop_move
            timeout_no_reaction = (
                self.reaction_timeout_sec > 0.0
                and hold_sec >= self.reaction_timeout_sec
                and abs(fast_price - poly_mid) < self.noise_edge
            )
            unrealized = self.pnl.get_unrealized_pnl(poly_orderbook)
            tp_line, sl_line = self._pnl_target_and_stop_lines()
            pnl_tp = self._hold_met(hold_sec) and unrealized >= tp_line
            pnl_sl = self._hold_met(hold_sec) and unrealized <= -sl_line

            should_close = (
                reaction_confirmed
                or protective_stop
                or timeout_no_reaction
                or pnl_tp
                or pnl_sl
            )
            if should_close:
                reason = "REACTION_TP"
                if protective_stop:
                    reason = "REACTION_STOP"
                elif timeout_no_reaction:
                    reason = "TIMEOUT_EXIT"
                elif pnl_tp:
                    reason = "PNL_TP"
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
                exit_price = no_bid if self.pnl.position_side in ("DOWN", "NO") else yes_bid
                pos_side = self.pnl.position_side or "UP"
                close_event = await self.execute("SELL", exit_price)
                ce = close_event or {}
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
                    "entry_book_px": self.entry_context.get("entry_book_px", 0.0),
                    "entry_exec_px": self.entry_context.get("entry_exec_px", 0.0),
                    "exit_book_px": float(ce.get("book_px") or exit_price),
                    "exit_exec_px": float(ce.get("exec_px") or 0.0),
                    "shares_bought": self.entry_context.get("shares_bought", 0.0),
                    "shares_sold": float(ce.get("shares_sold") or 0.0),
                    "cost_usd": self.entry_context.get("cost_usd", 0.0),
                    "proceeds_usd": float(ce.get("proceeds_usd") or 0.0),
                    "cost_basis_usd": float(ce.get("cost_basis_usd") or 0.0),
                    "entry_yes_bid": self.entry_context.get("entry_yes_bid"),
                    "entry_yes_ask": self.entry_context.get("entry_yes_ask"),
                    "exit_yes_bid": yes_bid,
                    "exit_yes_ask": yes_ask,
                    "exit_no_bid": no_bid,
                    "exit_no_ask": no_ask,
                }
                self.entry_poly_mid = None
                self.entry_fast_price = None
                self.entry_time = 0.0
                self.position_trend = "FLAT"
                self.entry_context = {}
                self._book_stall_ticks = 0
                self._prev_yes_mid = None
                self._prev_no_mid = None
                return result

    async def execute(self, side, price, amount_usd=None):
        """Execute simulated trade with optional notional override."""
        if amount_usd is None:
            amount_usd = self.trade_amount_usd
        return self.pnl.log_trade(side, price, amount_usd)