import asyncio
import logging
from collections import deque
import numpy as np

class FastPriceAggregator:
    """Aggregate Coinbase/Binance feeds into smart fast price and z-score."""

    def __init__(self, max_age_seconds=2.0):
        self.data = {
            "binance": {"price": 0.0, "timestamp": 0.0},
            "coinbase": {"price": 0.0, "timestamp": 0.0}
        }
        self.max_age = max_age_seconds
        self.prices = {
            "binance": deque(maxlen=200), # История для LSTM/RSI
            "coinbase": deque(maxlen=200)
        }
        self.history = deque(maxlen=500)

    def update(self, exchange, price):
        """Обновление данных из провайдеров."""
        current_time = asyncio.get_event_loop().time()
        self.data[exchange] = {
            "price": price,
            "timestamp": current_time
        }
        self.prices[exchange].append(price)

    def get_price(self):
        """Return smart hybrid price: Coinbase anchor + capped Binance lead."""
        c = self.data.get("coinbase")
        b = self.data.get("binance")
        if not c or c["price"] <= 0:
            if b and b["price"] > 0:
                return b["price"]
            return None

        c_price = c["price"]
        if not b or b["price"] <= 0:
            return c_price

        drift = b["price"] - c_price
        if abs(drift) > 5.0:
            return c_price + drift * 0.4
        return c_price

    def get_weighted_price(self):
        """Backward-compatible alias for smart price."""
        return self.get_price()

    def add_history(self, price):
        """Append a fast-price sample for z-score calculations."""
        if price is None:
            return
        self.history.append(float(price))

    def get_zscore(self):
        """Return rolling z-score of fast price."""
        if len(self.history) < 50:
            return 0.0
        arr = np.array(self.history, dtype=np.float64)
        std = float(arr.std()) + 1e-9
        return float((arr[-1] - arr.mean()) / std)

    def get_primary_history(self):
        """Return primary series for indicators/LSTM with Coinbase priority."""
        c = self.prices.get("coinbase", deque())
        if len(c) > 0:
            return c
        b = self.prices.get("binance", deque())
        if len(b) > 0:
            return b
        return deque()

    def is_ready(self):
        """Проверка, накоплено ли достаточно данных для работы (например, для LSTM)."""
        return len(self.get_primary_history()) >= 100