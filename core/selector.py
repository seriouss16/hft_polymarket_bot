import requests
import logging
import json # ОБЯЗАТЕЛЬНО

class MarketSelector:
    def __init__(self, asset="btc", interval=300):
        self.asset = asset
        self.interval = interval

    def get_current_slot_timestamp(self):
        from datetime import datetime, timezone
        now = int(datetime.now(timezone.utc).timestamp())
        return (now // self.interval) * self.interval

    def format_slug(self, timestamp):
        return f"{self.asset.lower()}-updown-5m-{timestamp}"

    async def fetch_token_id(self, slug):
        url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
        try:
            resp = requests.get(url, timeout=5)
            data = resp.json()
            if data and "clobTokenIds" in data[0]:
                raw = data[0]["clobTokenIds"]
                tids = json.loads(raw) if isinstance(raw, str) else raw
                return tids[0], data[0].get("question", slug)
        except Exception as e:
            logging.error(f"❌ Selector Error: {e}")
        return None, None