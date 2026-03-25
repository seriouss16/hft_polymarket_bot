"""Rolling price buffers and hybrid fast price for the HFT loop."""

import asyncio
import itertools
from collections import deque
from typing import Any

import numpy as np


class FastPriceAggregator:
    """Aggregate Coinbase/Binance feeds into smart fast price and z-score."""

    def __init__(self, max_age_seconds=2.0):
        self.data = {
            "binance": {"price": 0.0, "timestamp": 0.0, "bid": None, "ask": None},
            "coinbase": {"price": 0.0, "timestamp": 0.0, "bid": None, "ask": None},
        }
        self.max_age = max_age_seconds
        self.prices = {
            "binance": deque(maxlen=200),
            "coinbase": deque(maxlen=200),
        }
        self.history = deque(maxlen=500)
        self.zscore_window = 96

    @staticmethod
    def tail_last_n(seq, n: int) -> list[float]:
        """Return the last n samples from a deque or list without copying the full sequence."""
        if not seq or n <= 0:
            return []
        if len(seq) <= n:
            return list(seq)
        skip = len(seq) - n
        return list(itertools.islice(seq, skip, None))

    def update(self, exchange, price, ts=None, bid=None, ask=None):
        """Apply one tick from a websocket feed into local buffers."""
        current_time = (
            ts if ts is not None else asyncio.get_running_loop().time()
        )
        self.data[exchange] = {
            "price": price,
            "timestamp": current_time,
            "bid": bid,
            "ask": ask,
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

    def get_coinbase_price(self):
        """Return last Coinbase ticker price or None if unset."""
        c = self.data.get("coinbase")
        if not c:
            return None
        p = float(c.get("price") or 0.0)
        return p if p > 0.0 else None

    def get_binance_price(self):
        """Return last Binance book mid or None if unset."""
        b = self.data.get("binance")
        if not b:
            return None
        p = float(b.get("price") or 0.0)
        return p if p > 0.0 else None

    def get_binance_bbo(self):
        """Return last Binance best bid and ask from bookTicker, or None if missing."""
        b = self.data.get("binance")
        if not b:
            return None
        bid = b.get("bid")
        ask = b.get("ask")
        if bid is None or ask is None:
            return None
        fb = float(bid)
        fa = float(ask)
        if fb <= 0.0 or fa <= 0.0:
            return None
        return fb, fa

    def add_history(self, price):
        """Append a fast-price sample for z-score calculations."""
        if price is None:
            return
        self.history.append(float(price))

    def get_zscore(self):
        """Return rolling z-score using the last ``zscore_window`` fast-price samples."""
        if len(self.history) < 50:
            return 0.0
        window = self.tail_last_n(self.history, self.zscore_window)
        arr = np.asarray(window, dtype=np.float64)
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
        """Return True when enough primary ticks exist for LSTM-length indicators."""
        return len(self.get_primary_history()) >= 100

    def feed_timing(self, poly_ts: float, now_loop: float | None = None) -> dict[str, Any]:
        """Return receive-time ages and cross-feed skew using one monotonic clock.

        Timestamps on feeds are ``asyncio`` loop time when each message was handled locally
        (not exchange wall time). ``staleness_ms`` is max age of the slowest leg; use it for
        gates. ``skew_ms`` is (coinbase_recv - poly_recv) in ms: who was updated last; sign is
        not NTP skew by itself. Raw ages clamp to 0 ms when negative (future timestamps vs
        ``now_loop``), which usually means mixed clocks if it happens often.

        Omit ``now_loop`` so ages use the time at this call; passing a stale ``now_loop`` from
        the start of a long iteration understates age or clamps one leg to 0 ms.
        """
        if now_loop is None:
            now_loop = asyncio.get_running_loop().time()
        _MISSING = 1e9
        c_ts = float(self.data.get("coinbase", {}).get("timestamp", 0.0))
        b_ts = float(self.data.get("binance", {}).get("timestamp", 0.0))
        p_ts = float(poly_ts or 0.0)
        coinbase_age_ms = max(0.0, (now_loop - c_ts) * 1000.0) if c_ts > 0.0 else _MISSING
        binance_age_ms = max(0.0, (now_loop - b_ts) * 1000.0) if b_ts > 0.0 else _MISSING
        poly_age_ms = max(0.0, (now_loop - p_ts) * 1000.0) if p_ts > 0.0 else _MISSING
        skew_ms = (c_ts - p_ts) * 1000.0 if c_ts > 0.0 and p_ts > 0.0 else 0.0
        ages: list[float] = []
        if c_ts > 0.0:
            ages.append(coinbase_age_ms)
        if p_ts > 0.0:
            ages.append(poly_age_ms)
        if b_ts > 0.0:
            ages.append(binance_age_ms)
        staleness_ms = max(ages) if ages else _MISSING
        return {
            "now_loop": now_loop,
            "coinbase_age_ms": coinbase_age_ms,
            "binance_age_ms": binance_age_ms,
            "poly_age_ms": poly_age_ms,
            "skew_ms": skew_ms,
            "staleness_ms": staleness_ms,
        }

    def get_latency_ms(self, poly_ts: float) -> float:
        """Return max receive-age across feeds in ms (staleness); API name kept for callers."""
        return float(self.feed_timing(poly_ts)["staleness_ms"])