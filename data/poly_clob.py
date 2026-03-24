import asyncio
import json
import logging
import os
import websockets

class PolyOrderBook:
    def __init__(self, symbol="bitcoin"):
        self.symbol = symbol
        self.book = {
            "ask": 0.0,
            "bid": 0.0,
            "mid": 0.0,
            "btc_oracle": 0.0,
            "ask_size_top": 0.0,
            "bid_size_top": 0.0,
            "ts": 0.0,
        }
        self.url = "wss://ws-live-data.polymarket.com"

    async def connect(self):
        while True:
            try:
                async with websockets.connect(self.url) as ws:
                    sub = {
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": "crypto_prices_chainlink",
                                "type": "*",
                                "filters": "",
                            }
                        ],
                    }
                    await ws.send(json.dumps(sub))
                    logging.info(f"✅ Poly RTDS подключен для {self.symbol}")

                    async for msg in ws:
                        if not isinstance(msg, str) or not msg.strip():
                            continue
                        data = json.loads(msg)
                        if data.get("type") == "update":
                            payload = data.get("payload", {})
                            if payload.get("symbol", "").lower() != "btc/usd":
                                continue
                            price = float(payload["value"])
                            self.book["btc_oracle"] = price
                            self.book["mid"] = price
                            if float(self.book.get("ask") or 0.0) <= 0.0 or float(self.book.get("bid") or 0.0) <= 0.0:
                                half_spread = 0.005
                                ym = 0.5
                                self.book["ask"] = min(0.99, ym + half_spread)
                                self.book["bid"] = max(0.01, ym - half_spread)
                            self.book["ts"] = asyncio.get_event_loop().time()
                            # RTDS does not provide depth; keep synthetic top sizes for imbalance logic.
                            if float(self.book.get("ask_size_top") or 0.0) <= 0.0:
                                self.book["ask_size_top"] = 1.0
                            if float(self.book.get("bid_size_top") or 0.0) <= 0.0:
                                self.book["bid_size_top"] = 1.0
            except Exception as e:
                logging.error(f"❌ Poly RTDS Error: {e}")
                delay = float(os.getenv("HFT_WS_RECONNECT_SEC", "0"))
                await asyncio.sleep(delay)