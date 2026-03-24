import numpy as np
import time

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

    def can_trade(self):
        """Return True when risk limits allow new trade."""
        usd_exposure = abs(self.pnl.inventory * self.pnl.entry_price) if self.pnl.inventory > 0 else 0.0
        return usd_exposure < self.max_position

    def generate_signal(self, fast_price, poly_mid, zscore, lstm_forecast):
        """Return BUY_YES or BUY_NO or None based on edge/zscore/cooldown."""
        now = time.time()
        if now - self.last_trade_time < self.cooldown:
            return None

        edge = fast_price - poly_mid
        if abs(edge) < self.noise_edge:
            return None

        if edge > self.buy_edge and zscore > 0.5 and lstm_forecast >= fast_price:
            self.last_trade_time = now
            return "BUY_YES"
        if edge < self.sell_edge and zscore < -0.5:
            self.last_trade_time = now
            return "BUY_NO"
        return None

    def generate_live_signal(self, fast_price, poly_mid, zscore):
        """Return production-style signal without position side-effects."""
        now = time.time()
        if now - self.last_trade_time < self.cooldown:
            return None
        edge = fast_price - poly_mid
        if abs(edge) < 5.0:
            return None
        if edge > 10.0 and zscore > 0.7:
            self.last_trade_time = now
            return "BUY_YES"
        if edge < -10.0 and zscore < -0.7:
            self.last_trade_time = now
            return "BUY_NO"
        return None

    async def process_tick(self, fast_price, poly_orderbook, price_history, lstm_forecast, zscore=0.0):
        if not fast_price or not poly_orderbook['ask']:
            return

        current_rsi = compute_rsi(np.array(price_history))

        poly_mid = poly_orderbook.get("mid", 0.0)
        signal = self.generate_signal(fast_price, poly_mid, zscore, lstm_forecast)

        if signal == "BUY_YES" and self.pnl.inventory == 0 and self.can_trade() and current_rsi < 85:
            await self.execute("BUY", poly_orderbook['ask'])
            return

        if self.pnl.inventory > 0:
            stop_loss = self.pnl.entry_price * 0.998
            should_close = current_rsi > 75 or fast_price < stop_loss or signal == "BUY_NO"
            if should_close:
                await self.execute("SELL", poly_orderbook['bid'])

    async def execute(self, side, price):
        """Execute simulated trade."""
        self.pnl.log_trade(side, price, self.trade_amount_usd)