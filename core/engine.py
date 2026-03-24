import numpy as np
import time
import logging
from collections import deque

from ml.indicators import compute_rsi

class HFTEngine:
    """Signal, risk, and execution engine for Polymarket latency strategy."""

    def __init__(self, pnl_tracker, is_test_mode=True):
        self.pnl = pnl_tracker
        self.is_test_mode = is_test_mode
        self.noise_edge = 3.0
        self.buy_edge = 8.0
        self.sell_edge = -8.0
        self.cooldown = 2.0
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
        self.speed_floor = 2.0
        self.edge_window = deque(maxlen=120)
        self.last_edge_sign = 0
        self.trend_dir = "FLAT"
        self.trend_since_ts = 0.0
        self.trend_depth = 0.0

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

    def generate_signal(self, fast_price, poly_mid, zscore, lstm_forecast):
        """Return BUY_YES or BUY_NO or None based on edge/zscore/cooldown."""
        now = time.time()
        if now - self.last_trade_time < self.cooldown:
            return None

        edge, speed, depth, age, trend = self.update_trend(fast_price, poly_mid)
        if abs(edge) < self.noise_edge:
            return None

        trend_ok_up = trend == "UP" and speed > 0 and depth >= self.buy_edge and age >= 0.3
        trend_ok_down = trend == "DOWN" and speed < 0 and depth >= abs(self.sell_edge) and age >= 0.3

        if edge > self.buy_edge and zscore > 0.5 and lstm_forecast >= fast_price and trend_ok_up:
            self.last_trade_time = now
            return "BUY_YES"
        if edge < self.sell_edge and zscore < -0.5 and trend_ok_down:
            self.last_trade_time = now
            return "BUY_NO"
        return None

    def generate_live_signal(self, fast_price, poly_mid, zscore):
        """Return production-style signal without position side-effects."""
        now = time.time()
        if now - self.last_trade_time < self.cooldown:
            return None
        edge, speed, depth, age, trend = self.update_trend(fast_price, poly_mid)
        if abs(edge) < 5.0:
            return None
        if edge > 10.0 and zscore > 0.7 and trend == "UP" and speed > 0 and depth >= 10.0 and age >= 0.3:
            self.last_trade_time = now
            return "BUY_YES"
        if edge < -10.0 and zscore < -0.7 and trend == "DOWN" and speed < 0 and depth >= 10.0 and age >= 0.3:
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

    async def process_tick(self, fast_price, poly_orderbook, price_history, lstm_forecast, zscore=0.0):
        if not fast_price or not poly_orderbook['ask']:
            return

        current_rsi = compute_rsi(np.array(price_history))

        poly_mid = poly_orderbook.get("mid", 0.0)
        bid_size = float(poly_orderbook.get("bid_size_top", 1.0))
        ask_size = float(poly_orderbook.get("ask_size_top", 1.0))
        imbalance = bid_size / (bid_size + ask_size + 1e-9)
        signal = self.generate_signal(fast_price, poly_mid, zscore, lstm_forecast)
        trend = self.get_trend_state()

        yes_ask = float(poly_orderbook["ask"])
        yes_bid = float(poly_orderbook["bid"])
        no_ask = max(0.01, min(0.99, 1.0 - yes_bid))
        no_bid = max(0.01, min(0.99, 1.0 - yes_ask))

        if (
            signal == "BUY_YES"
            and self.pnl.inventory == 0
            and self.can_trade()
            and current_rsi < 85
            and imbalance >= 0.5
        ):
            await self.execute("BUY_YES", yes_ask)
            self.entry_poly_mid = poly_mid
            self.entry_fast_price = fast_price
            self.entry_time = time.time()
            self.position_trend = trend["trend"]
            logging.info(
                "🧭 Entry context: poly_mid=%.4f fast=%.2f edge=%.2f trend=%s imb=%.2f",
                poly_mid,
                fast_price,
                fast_price - poly_mid,
                self.position_trend,
                imbalance,
            )
            return

        if (
            signal == "BUY_NO"
            and self.pnl.inventory == 0
            and self.can_trade()
            and current_rsi > 15
            and imbalance <= 0.5
        ):
            await self.execute("BUY_NO", no_ask)
            self.entry_poly_mid = poly_mid
            self.entry_fast_price = fast_price
            self.entry_time = time.time()
            self.position_trend = trend["trend"]
            logging.info(
                "🧭 Entry context: side=BUY_NO poly_mid=%.4f fast=%.2f edge=%.2f trend=%s imb=%.2f",
                poly_mid,
                fast_price,
                fast_price - poly_mid,
                self.position_trend,
                imbalance,
            )
            return

        if self.pnl.inventory > 0:
            now = time.time()
            hold_sec = now - self.entry_time if self.entry_time else 0.0
            poly_move = 0.0
            if self.entry_poly_mid and self.entry_poly_mid > 0:
                poly_move = (poly_mid - self.entry_poly_mid) / self.entry_poly_mid

            if self.pnl.position_side == "NO":
                reaction_confirmed = hold_sec >= self.min_hold_sec and poly_move <= -self.poly_take_profit_move
                protective_stop = hold_sec >= self.min_hold_sec and poly_move >= self.poly_stop_move
                imbalance_flip = imbalance > 0.55 and hold_sec >= self.min_hold_sec
            else:
                reaction_confirmed = hold_sec >= self.min_hold_sec and poly_move >= self.poly_take_profit_move
                protective_stop = hold_sec >= self.min_hold_sec and poly_move <= -self.poly_stop_move
                imbalance_flip = imbalance < 0.45 and hold_sec >= self.min_hold_sec
            timeout_no_reaction = (
                hold_sec >= self.reaction_timeout_sec
                and (
                    abs(fast_price - poly_mid) < self.noise_edge
                    or signal == "BUY_NO"
                    or current_rsi > 80
                )
            )
            trend_lost = trend["trend"] != self.position_trend and trend["age"] > 0.5
            speed_slowdown = abs(trend["speed"]) < self.speed_floor and hold_sec >= self.min_hold_sec
            unrealized = self.pnl.get_unrealized_pnl(poly_mid)
            pnl_tp = unrealized >= self.target_profit_usd
            pnl_sl = unrealized <= -self.stop_loss_usd

            should_close = (
                reaction_confirmed
                or protective_stop
                or timeout_no_reaction
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
                    "📌 Exit reason=%s hold=%.1fs poly_move=%.4f edge=%.2f pnl=%.2f imb=%.2f",
                    reason,
                    hold_sec,
                    poly_move,
                    fast_price - poly_mid,
                    unrealized,
                    imbalance,
                )
                exit_price = no_bid if self.pnl.position_side == "NO" else yes_bid
                await self.execute("SELL", exit_price)
                self.entry_poly_mid = None
                self.entry_fast_price = None
                self.entry_time = 0.0
                self.position_trend = "FLAT"

    async def execute(self, side, price):
        """Execute simulated trade."""
        self.pnl.log_trade(side, price, self.trade_amount_usd)