import asyncio
import logging
import os
import time

from core.live_common import OrderStatus, TrackedOrder
from core.live_engine import LiveExecutionEngine

# Setup logging to capture the warning
logging.basicConfig(level=logging.INFO)


class MockCache:
    def __init__(self, fresh=True):
        self._fresh = fresh
        self.enabled = True

    def is_fresh(self, *args):
        return self._fresh


async def verify():
    # Set required environment variables for LiveExecutionEngine
    os.environ["HFT_LIVE_SKIP_STATS_LOG_SEC"] = "60"
    os.environ["HFT_MAX_ENTRY_ASK"] = "0.99"
    os.environ["LIVE_CLOB_BOOK_HTTP_TIMEOUT"] = "5"
    os.environ["LIVE_ORDER_FILL_POLL_SEC"] = "1"
    os.environ["LIVE_ORDER_STALE_SEC"] = "5"
    os.environ["LIVE_ORDER_MAX_REPRICE"] = "3"
    os.environ["LIVE_ORDER_EMERGENCY_TICKS"] = "5"
    os.environ["LIVE_REPRICE_POST_CANCEL_SLEEP_SEC"] = "0.1"
    os.environ["LIVE_REPRICE_POST_CANCEL_FILL_POLLS"] = "1"
    os.environ["LIVE_REPRICE_POST_CANCEL_POLL_SEC"] = "0.1"
    os.environ["LIVE_ORDERBOOK_STALE_SEC_DAY"] = "10"
    os.environ["LIVE_ORDERBOOK_STALE_SEC_NIGHT"] = "10"
    os.environ["POLY_SIGNATURE_TYPE"] = "1"
    os.environ["CLOB_BOOK_HTTP"] = "http://localhost"

    # Force LIVE_STALE_BLOCK_ACTIONS=1
    os.environ["LIVE_STALE_BLOCK_ACTIONS"] = "1"

    engine = LiveExecutionEngine(None, None, test_mode=True)
    market_cache = MockCache(fresh=False)  # Stale
    engine.set_market_book_cache(market_cache)

    print("--- Testing BUY blocking ---")
    res = await engine.execute("BUY_UP", "token_123", order_size=10.0)
    if res == (0.0, 0.0):
        print("SUCCESS: BUY blocked due to stale data")
    else:
        print(f"FAILURE: BUY allowed despite stale data: {res}")

    print("\n--- Testing POLL blocking ---")
    tracked = TrackedOrder("ord_1", "token_123", "BUY", 0.5, 10.0)
    # Manually make it stale
    tracked.placed_at = time.time() - 100

    # Mock _wait_for_order_fill to return "live" so it continues to the freshness check
    async def mock_wait(*args, **kwargs):
        return "live", 0.0

    engine._wait_for_order_fill = mock_wait

    # Run poll_order in a way that it hits the freshness check
    try:
        # It should hit the 'continue' in the loop and stay there
        await asyncio.wait_for(engine._poll_order(tracked), timeout=2.0)
    except asyncio.TimeoutError:
        print("POLL loop timed out as expected (it should be retrying freshness check)")


if __name__ == "__main__":
    asyncio.run(verify())
