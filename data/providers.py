"""Websocket clients for Coinbase and Binance fast price feeds."""

import asyncio
import json
import logging
import os
from typing import Any

import websockets


class FastExchangeProvider:
    """Subscribe to one exchange websocket and push mid prices into a callback."""

    def __init__(self, exchange_name, url, symbol, update_callback):
        self.name = exchange_name
        self.url = url
        self.symbol = symbol.lower()
        self.update_callback = update_callback

    async def connect(self):
        """Reconnect loop: subscribe and stream best bid/ask mids into ``update_callback``."""
        uri = self.url
        if self.name == "binance":
            stream_symbol = self.symbol
            if not stream_symbol.endswith("usdt"):
                stream_symbol = f"{stream_symbol}usdt"
            uri = f"wss://stream.binance.com:9443/stream?streams={stream_symbol}@bookTicker"

        while True:
            try:
                async with websockets.connect(uri, ping_interval=10, ping_timeout=5) as ws:
                    logging.info(f"✅ [{self.name}] Connected: {uri}")

                    if self.name == "coinbase":
                        product = self.symbol.upper()
                        if "-" not in product:
                            product = f"{product}-USD"
                        sub = {"type": "subscribe", "channels": [{"name": "ticker", "product_ids": [product]}]}
                        await ws.send(json.dumps(sub))

                    async for msg in ws:
                        try:
                            data = json.loads(msg)
                        except (TypeError, json.JSONDecodeError):
                            logging.debug("[%s] Skip non-JSON websocket frame.", self.name)
                            continue
                        price = None
                        bid_px = None
                        ask_px = None
                        exchange_ts = None

                        if self.name == "binance":
                            # Combined stream wraps payload in ``data``; raw ``/ws/`` sends payload at top level.
                            tick = data.get("data", data) if isinstance(data, dict) else {}
                            if isinstance(tick, dict) and "a" in tick and "b" in tick:
                                bid_px = float(tick["b"])
                                ask_px = float(tick["a"])
                                price = (bid_px + ask_px) / 2.0
                                if "E" in tick:
                                    exchange_ts = float(tick["E"]) / 1000.0

                        elif self.name == "coinbase":
                            if data.get("type") == "ticker":
                                price = float(data["price"])
                                if "best_bid" in data and "best_ask" in data:
                                    bid_px = float(data["best_bid"])
                                    ask_px = float(data["best_ask"])
                                if "time" in data:
                                    try:
                                        from datetime import datetime
                                        dt = datetime.fromisoformat(data["time"].replace("Z", "+00:00"))
                                        exchange_ts = dt.timestamp()
                                    except Exception:
                                        pass

                        if price:
                            loop_ts = asyncio.get_running_loop().time()
                            if bid_px is not None and ask_px is not None:
                                self.update_callback(
                                    self.name,
                                    price,
                                    loop_ts,
                                    bid_px,
                                    ask_px,
                                    exchange_ts=exchange_ts,
                                )
                            else:
                                self.update_callback(self.name, price, loop_ts, exchange_ts=exchange_ts)

            except asyncio.CancelledError:
                logging.info("🛑 [%s] WebSocket task cancelled; stopping provider.", self.name)
                raise
            except Exception as e:
                logging.error(f"❌ [{self.name}] Error: {e}")
                delay = float(os.getenv("HFT_WS_RECONNECT_SEC") or "2")
                await asyncio.sleep(delay)
