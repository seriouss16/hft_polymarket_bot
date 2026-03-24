import numpy as np

from ml.indicators import compute_rsi, compute_ma

class HFTEngine:
    def __init__(self, pnl_tracker, is_test_mode=True):
        self.pnl = pnl_tracker
        self.is_test_mode = is_test_mode
        # Снижаем порог до 0.01% или даже 0.005% для тестов
        self.threshold = 0.0001  # 0.01% разницы
        self.max_rsi = 85

    async def process_tick(self, fast_price, poly_orderbook, price_history, lstm_forecast):
        if not fast_price or not poly_orderbook['ask']:
            return

        current_rsi = compute_rsi(np.array(price_history))
        
        # ЛОГИКА ВХОДА:
        # 1. Быстрая цена выше Ask Polymarket
        # 2. RSI не перекуплен
        # 3. Прогноз LSTM выше текущей цены
        if fast_price > poly_orderbook['ask'] * (1 + self.threshold):
            if current_rsi < self.max_rsi and lstm_forecast > poly_orderbook['ask']:
                if self.pnl.inventory == 0:
                    await self.execute("BUY", poly_orderbook['ask'])

        # ЛОГИКА ВЫХОДА:
        # 1. RSI > 75 (перекупленность)
        # 2. ИЛИ Цена упала ниже входа на 0.2% (Stop Loss)
        elif self.pnl.inventory > 0:
            stop_loss = self.pnl.entry_price * 0.998
            if current_rsi > 75 or fast_price < stop_loss:
                await self.execute("SELL", poly_orderbook['bid'])

    async def execute(self, side, price):
        # Логика из предыдущего шага (Simulator vs Real)
        self.pnl.log_trade(side, price, 100.0)