import numpy as np
import time
import logging
from collections import deque

from ml.indicators import compute_rsi, dynamic_rsi_bands

class HFTEngine:
    """Signal, risk, and execution engine for Polymarket latency strategy."""

    def __init__(self, pnl_tracker, is_test_mode=True):
        self.pnl = pnl_tracker
        self.is_test_mode = is_test_mode
        self.noise_edge = 3.0
        self.buy_edge = 6.0
        self.sell_edge = -6.0
        self.cooldown = 1.0
        self.last_trade_time = 0.0
        self.max_position = 100.0
        self.trade_amount_usd = 100.0
        self.min_hold_sec = 3.0
        self.reaction_timeout_sec = 20.0
        self.poly_take_profit_move = 0.0004
        self.poly_stop_move = 0.0003
        self.entry_poly_mid = None
        self.entry_fast_price = None
        self.entry_time = 0.0
        self.position_trend = "FLAT"
        self.target_profit_usd = 0.30
        self.stop_loss_usd = 0.30
        self.speed_floor = 0.8
        self.edge_window = deque(maxlen=120)
        self.last_edge_sign = 0
        self.trend_dir = "FLAT"
        self.trend_since_ts = 0.0
        self.trend_depth = 0.0
        self.entry_context = {}
        self._last_rsi = 50.0
        self.rsi_hold_yes_floor = 40.0
        self.rsi_hold_no_ceiling = 60.0
        self.rsi_entry_yes_low = 28.0
        self.rsi_entry_yes_high = 78.0
        self.rsi_entry_no_low = 22.0
        self.rsi_entry_no_high = 72.0
        self.rsi_period = 14
        self.rsi_exit_upper_base = 70.0
        self.rsi_exit_lower_base = 30.0
        self.rsi_band_vol_k = 0.08
        self.rsi_slope_exit_enabled = True
        self.rsi_slope_yes_exit = -2.5
        self.rsi_slope_no_exit = 2.5
        self._rsi_tick_history = deque(maxlen=10)
        self._last_rsi_upper = 70.0
        self._last_rsi_lower = 30.0
        self._last_rsi_slope = 0.0

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
        if position_side == "YES":
            return rsi >= self.rsi_hold_yes_floor
        if position_side == "NO":
            return rsi <= self.rsi_hold_no_ceiling
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

    def dynamic_edge_threshold(self, price_history, recent_pnl=0.0, latency_ms=0.0):
        """Return adaptive edge threshold in price units from recent volatility."""
        if not price_history or len(price_history) < 30:
            return self.buy_edge, abs(self.sell_edge)
        arr = np.array(price_history[-50:], dtype=np.float64)
        vol = float(np.std(arr))
        pnl_penalty = 1.15 if recent_pnl < 0 else 1.0
        latency_boost = 1.10 if latency_ms > 250 else 1.0
        edge = max(2.0, min(20.0, vol * 0.6 * pnl_penalty * latency_boost))
        return edge, edge

    def generate_signal(self, fast_price, poly_mid, zscore, lstm_forecast, price_history, recent_pnl=0.0, latency_ms=0.0):
        """Return BUY_YES or BUY_NO or None based on edge/zscore/cooldown."""
        now = time.time()
        if now - self.last_trade_time < self.cooldown:
            return None

        edge, speed, depth, age, trend = self.update_trend(fast_price, poly_mid)
        buy_edge_dyn, sell_edge_dyn = self.dynamic_edge_threshold(
            price_history=price_history,
            recent_pnl=recent_pnl,
            latency_ms=latency_ms,
        )
        if abs(edge) < self.noise_edge:
            return None

        trend_ok_up = trend == "UP" and speed >= -0.2 and depth >= buy_edge_dyn and age >= 0.15
        trend_ok_down = trend == "DOWN" and speed <= 0.2 and depth >= sell_edge_dyn and age >= 0.15

        if edge > buy_edge_dyn and zscore > 0.25 and lstm_forecast >= fast_price and trend_ok_up:
            self.last_trade_time = now
            return "BUY_YES"
        if edge < -sell_edge_dyn and zscore < -0.25 and trend_ok_down:
            self.last_trade_time = now
            return "BUY_NO"
        return None

    def generate_live_signal(self, fast_price, poly_mid, zscore):
        """Return production-style signal without position side-effects."""
        now = time.time()
        if now - self.last_trade_time < self.cooldown:
            return None
        edge, speed, depth, age, trend = self.update_trend(fast_price, poly_mid)
        if abs(edge) < 4.0:
            return None
        if edge > 8.0 and zscore > 0.4 and trend == "UP" and speed >= -0.2 and depth >= 8.0 and age >= 0.15:
            self.last_trade_time = now
            return "BUY_YES"
        if edge < -8.0 and zscore < -0.4 and trend == "DOWN" and speed <= 0.2 and depth >= 8.0 and age >= 0.15:
            self.last_trade_time = now
            return "BUY_NO"
        return None

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
    ):
        if not fast_price or not poly_orderbook['ask']:
            return

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
        imbalance = bid_size / (bid_size + ask_size + 1e-9)
        signal = self.generate_signal(
            fast_price,
            poly_mid,
            zscore,
            lstm_forecast,
            price_history,
            recent_pnl=recent_pnl,
            latency_ms=latency_ms,
        )
        trend = self.get_trend_state()
        buy_edge_dyn, sell_edge_dyn = self.dynamic_edge_threshold(
            price_history,
            recent_pnl=recent_pnl,
            latency_ms=latency_ms,
        )

        yes_ask = float(poly_orderbook["ask"])
        yes_bid = float(poly_orderbook["bid"])
        no_ask = max(0.01, min(0.99, 1.0 - yes_bid))
        no_bid = max(0.01, min(0.99, 1.0 - yes_ask))

        pre_reaction_yes = (fast_price - poly_mid) >= buy_edge_dyn and imbalance < 0.15
        pre_reaction_no = (fast_price - poly_mid) <= -sell_edge_dyn and imbalance > -0.15

        if (
            signal == "BUY_YES"
            and self.pnl.inventory == 0
            and self.can_trade()
            and self.rsi_entry_yes_low < current_rsi < self.rsi_entry_yes_high
            and pre_reaction_yes
            and meta_enabled
        ):
            open_event = await self.execute("BUY_YES", yes_ask)
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
            }
            logging.info(
                "🧭 Entry context: poly_mid=%.4f fast=%.2f edge=%.2f trend=%s imb=%.2f",
                poly_mid,
                fast_price,
                fast_price - poly_mid,
                self.position_trend,
                imbalance,
            )
            return {"event": "OPEN", "side": "YES", "trade": open_event}

        if (
            signal == "BUY_NO"
            and self.pnl.inventory == 0
            and self.can_trade()
            and self.rsi_entry_no_low < current_rsi < self.rsi_entry_no_high
            and pre_reaction_no
            and meta_enabled
        ):
            open_event = await self.execute("BUY_NO", no_ask)
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
            }
            logging.info(
                "🧭 Entry context: side=BUY_NO poly_mid=%.4f fast=%.2f edge=%.2f trend=%s imb=%.2f",
                poly_mid,
                fast_price,
                fast_price - poly_mid,
                self.position_trend,
                imbalance,
            )
            return {"event": "OPEN", "side": "NO", "trade": open_event}

        if self.pnl.inventory > 0:
            now = time.time()
            hold_sec = now - self.entry_time if self.entry_time else 0.0
            poly_move = 0.0
            if self.entry_poly_mid and self.entry_poly_mid > 0:
                poly_move = (poly_mid - self.entry_poly_mid) / self.entry_poly_mid

            if self.pnl.position_side == "NO":
                reaction_confirmed = hold_sec >= self.min_hold_sec and poly_move <= -self.poly_take_profit_move
                protective_stop = hold_sec >= self.min_hold_sec and poly_move >= self.poly_stop_move
                imbalance_flip = imbalance > 0.20 and hold_sec >= self.min_hold_sec
            else:
                reaction_confirmed = hold_sec >= self.min_hold_sec and poly_move >= self.poly_take_profit_move
                protective_stop = hold_sec >= self.min_hold_sec and poly_move <= -self.poly_stop_move
                imbalance_flip = imbalance < -0.20 and hold_sec >= self.min_hold_sec
            timeout_no_reaction = (
                hold_sec >= self.reaction_timeout_sec
                and (
                    abs(fast_price - poly_mid) < self.noise_edge
                    or signal == "BUY_NO"
                    or current_rsi > 80
                )
            )
            trend_lost = (
                hold_sec >= self.min_hold_sec
                and trend["trend"] != self.position_trend
                and trend["age"] > 0.5
            )
            speed_slowdown = abs(trend["speed"]) < self.speed_floor and hold_sec >= self.min_hold_sec
            unrealized = self.pnl.get_unrealized_pnl(poly_orderbook)
            pnl_tp = unrealized >= self.target_profit_usd
            pnl_sl = unrealized <= -self.stop_loss_usd

            side = self.pnl.position_side or "YES"
            if self._rsi_suppresses_soft_exit(side, current_rsi):
                trend_lost = False
                speed_slowdown = False
                imbalance_flip = False

            rsi_overbought_exit = (
                hold_sec >= self.min_hold_sec
                and side == "YES"
                and current_rsi >= upper_b
            )
            rsi_oversold_exit = (
                hold_sec >= self.min_hold_sec
                and side == "NO"
                and current_rsi <= lower_b
            )
            rsi_slope_exit = (
                self.rsi_slope_exit_enabled
                and hold_sec >= self.min_hold_sec
                and (
                    (side == "YES" and self._last_rsi_slope <= self.rsi_slope_yes_exit)
                    or (side == "NO" and self._last_rsi_slope >= self.rsi_slope_no_exit)
                )
            )

            should_close = (
                reaction_confirmed
                or protective_stop
                or timeout_no_reaction
                or rsi_overbought_exit
                or rsi_oversold_exit
                or rsi_slope_exit
                or trend_lost
                or speed_slowdown
                or imbalance_flip
                or pnl_tp
                or pnl_sl
            )
            if should_close:
                reason = "REACTION_TP"
                if protective_stop:
                    reason = "REACTION_STOP"
                elif timeout_no_reaction:
                    reason = "TIMEOUT_EXIT"
                elif rsi_overbought_exit:
                    reason = "RSI_OVERBOUGHT"
                elif rsi_oversold_exit:
                    reason = "RSI_OVERSOLD"
                elif rsi_slope_exit:
                    reason = "RSI_SLOPE"
                elif trend_lost:
                    reason = "TREND_LOST"
                elif speed_slowdown:
                    reason = "SPEED_SLOWDOWN"
                elif imbalance_flip:
                    reason = "IMBALANCE_FLIP"
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
                exit_price = no_bid if self.pnl.position_side == "NO" else yes_bid
                pos_side = self.pnl.position_side or "YES"
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
                }
                self.entry_poly_mid = None
                self.entry_fast_price = None
                self.entry_time = 0.0
                self.position_trend = "FLAT"
                self.entry_context = {}
                return result

    async def execute(self, side, price):
        """Execute simulated trade."""
        return self.pnl.log_trade(side, price, self.trade_amount_usd)