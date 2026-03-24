import asyncio
import websockets
import json
import logging

class PolyOrderBook:
    def __init__(self, symbol="bitcoin"):
        self.symbol = symbol
        self.book = {"ask": 0.0, "bid": 0.0, "mid": 0.0}
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
                            self.book['mid'] = price
                            self.book['ask'] = price + 0.05
                            self.book['bid'] = price - 0.05
            except Exception as e:
                logging.error(f"❌ Poly RTDS Error: {e}")
                await asyncio.sleep(2)