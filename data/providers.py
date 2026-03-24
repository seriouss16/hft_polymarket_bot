import asyncio
import json
import logging
import os
import websockets

class FastExchangeProvider:
    def __init__(self, exchange_name, url, symbol, update_callback):
        self.name = exchange_name
        self.url = url
        self.symbol = symbol.lower()
        self.update_callback = update_callback

    async def connect(self):
        uri = self.url
        if self.name == "binance":
            stream_symbol = self.symbol
            if not stream_symbol.endswith("usdt"):
                stream_symbol = f"{stream_symbol}usdt"
            uri = f"wss://stream.binance.com:9443/stream?streams={stream_symbol}@bookTicker"
        
        while True:
            try:
                async with websockets.connect(uri) as ws:
                    logging.info(f"✅ [{self.name}] Соединение установлено: {uri}")
                    
                    if self.name == "coinbase":
                        product = self.symbol.upper()
                        if "-" not in product:
                            product = f"{product}-USD"
                        sub = {"type": "subscribe", "channels": [{"name": "ticker", "product_ids": [product]}]}
                        await ws.send(json.dumps(sub))
                    
                    async for msg in ws:
                        data = json.loads(msg)
                        price = None
                        
                        if self.name == "binance":
                            # В Multiplex Stream данные лежат в ключе 'data'
                            tick = data.get('data', {})
                            if 'a' in tick and 'b' in tick:
                                price = (float(tick['a']) + float(tick['b'])) / 2
                        
                        elif self.name == "coinbase":
                            if data.get('type') == 'ticker':
                                price = float(data['price'])
                        
                        if price:
                            ts = asyncio.get_event_loop().time()
                            self.update_callback(self.name, price, ts)
                            
            except Exception as e:
                logging.error(f"❌ [{self.name}] Ошибка: {e}")
                delay = float(os.getenv("HFT_WS_RECONNECT_SEC", "0.2"))
                await asyncio.sleep(delay)