import asyncio
import logging
from collections import deque

class FastPriceAggregator:
    def __init__(self, max_age_seconds=2.0):
        # Храним цену и время последнего обновления
        self.data = {
            "binance": {"price": 0.0, "timestamp": 0.0},
            "coinbase": {"price": 0.0, "timestamp": 0.0}
        }
        self.max_age = max_age_seconds
        self.prices = {
            "binance": deque(maxlen=200), # История для LSTM/RSI
            "coinbase": deque(maxlen=200)
        }

    def update(self, exchange, price):
        """Обновление данных из провайдеров."""
        current_time = asyncio.get_event_loop().time()
        self.data[exchange] = {
            "price": price,
            "timestamp": current_time
        }
        self.prices[exchange].append(price)

    # В data/aggregator.py
    def get_weighted_price(self):
        # Priory Coinbase as requested to reduce drift vs Polymarket.
        c = self.data.get("coinbase")
        if c and c["price"] > 0:
            return c["price"]
        b = self.data.get("binance")
        if b and b["price"] > 0:
            return b["price"]
        return None

    def is_ready(self):
        """Проверка, накоплено ли достаточно данных для работы (например, для LSTM)."""
        return len(self.prices.get("coinbase", [])) >= 100