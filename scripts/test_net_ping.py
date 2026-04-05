import asyncio
import json
import logging
import shutil
import statistics
import subprocess
import time
from collections import defaultdict
from logging.handlers import RotatingFileHandler

import httpx
import websockets
from py_clob_client.client import ClobClient


def normalize_clob_token_ids(raw) -> list[str]:
    """Same logic as ``core.selector.normalize_clob_token_ids`` — inlined so this script runs when copied without ``hft_bot`` on PYTHONPATH."""
    if raw is None:
        return []
    parsed = raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
        except json.JSONDecodeError:
            return [s]
    if isinstance(parsed, (list, tuple)):
        return [str(x) for x in parsed if x is not None]
    if isinstance(parsed, bool):
        return []
    if isinstance(parsed, int):
        return [str(parsed)]
    if isinstance(parsed, float):
        if not parsed.is_integer():
            return []
        return [str(int(parsed))]
    if isinstance(parsed, str):
        return [parsed]
    return []


# =============================
# LOGGING SETUP
# =============================

logger = logging.getLogger("polymarket_latency_full")
logger.setLevel(logging.INFO)

file_handler = RotatingFileHandler("latency_full.log", maxBytes=20_000_000, backupCount=5)
console_handler = logging.StreamHandler()

formatter = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(console_handler)

# =============================
# CONFIG
# =============================

CLOB_HOST = "https://clob.polymarket.com"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"
POLYMARKET_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

REQUEST_INTERVAL = 0.8
TEST_DURATION = 120
MARKETS_LIMIT = 200
ORDERBOOK_VALIDATION_LIMIT = 300
ORDERBOOK_TARGET_TOKENS = 80
WS_BATCH_SIZE = 40

# =============================
# GLOBALS
# =============================

REQUEST_STATS = defaultdict(list)
FULL_REQUEST_LOG = []

# =============================
# FETCH MARKETS
# =============================


def fetch_markets_sync():
    """Load CLOB token ids from Gamma ``clobTokenIds`` (matches ``/book`` API).

    ``get_simplified_markets()`` token ids often do not match CLOB orderbooks (404 spam).
    """
    logger.info("Fetching markets via Gamma API (clobTokenIds)...")
    try:
        r = httpx.get(
            GAMMA_MARKETS_URL,
            params={
                "limit": MARKETS_LIMIT,
                "active": "true",
                "closed": "false",
            },
            timeout=30.0,
        )
        r.raise_for_status()
        markets = r.json()
        if not isinstance(markets, list):
            logger.error("Gamma markets unexpected shape")
            return []
        token_ids: list[str] = []
        for m in markets:
            if not isinstance(m, dict):
                continue
            for tid in normalize_clob_token_ids(m.get("clobTokenIds")):
                token_ids.append(tid)
        token_ids = list(dict.fromkeys(token_ids))
        logger.info(f"Loaded {len(token_ids)} candidate token_ids (Gamma clobTokenIds)")
        return token_ids
    except Exception as e:
        logger.error(f"Error fetching markets: {e}")
        return []


def validate_token_ids_sync(token_ids: list[str]) -> list[str]:
    """Keep token_ids that currently have an orderbook to avoid 404 spam."""
    if not token_ids:
        return []
    client = ClobClient(CLOB_HOST)
    valid: list[str] = []
    not_found = 0
    checked = 0
    for tid in token_ids:
        if checked >= ORDERBOOK_VALIDATION_LIMIT:
            break
        checked += 1
        try:
            client.get_order_book(tid)
            valid.append(tid)
            if len(valid) >= ORDERBOOK_TARGET_TOKENS:
                break
        except Exception as exc:
            text = str(exc)
            if "status_code=404" in text or "No orderbook exists" in text:
                not_found += 1
                continue
            logger.debug(f"Validation skip {tid[:8]}...: {exc}")
    logger.info(
        "Orderbook validation: checked=%d valid=%d no_book_404=%d",
        checked,
        len(valid),
        not_found,
    )
    return valid


# =============================
# REST LATENCY
# =============================


async def measure_clob_latency(client: ClobClient, token_id: str):
    start = time.perf_counter()
    try:
        await asyncio.to_thread(client.get_order_book, token_id)
        latency = (time.perf_counter() - start) * 1000
        logger.info(f"CLOB REST | {token_id[:8]}... | {latency:.2f} ms")
        return latency, 200
    except Exception as e:
        text = str(e)
        if "status_code=404" in text or "No orderbook exists" in text:
            logger.debug(f"CLOB no-orderbook 404 for {token_id[:8]}...")
            return None, 404
        logger.warning(f"CLOB request failed for {token_id[:8]}...: {e}")
        return None, None


# =============================
# WS LATENCY
# =============================


async def polymarket_ws_listener(ws_latencies, token_ids_batch):
    if not token_ids_batch:
        return
    async with websockets.connect(POLYMARKET_WS, ping_interval=None) as ws:
        sub_msg = {
            "type": "market",
            "assets_ids": token_ids_batch,
            "custom_feature_enabled": True,
        }
        await ws.send(json.dumps(sub_msg))
        logger.info(f"WS subscribed to {len(token_ids_batch)} assets")

        async def heartbeat():
            while True:
                await asyncio.sleep(10)
                try:
                    await ws.send("PING")
                except Exception:
                    break

        hb_task = asyncio.create_task(heartbeat())

        try:
            while True:
                start = time.perf_counter()
                try:
                    await asyncio.wait_for(ws.recv(), timeout=8)
                    end = time.perf_counter()
                    latency = (end - start) * 1000
                    ws_latencies.append(latency)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    logger.error(f"WS error: {e}")
                    break
        finally:
            hb_task.cancel()


# =============================
# ANALYSIS HELPER
# =============================


def print_request_analysis():
    logger.info("=== DETAILED REQUEST / RESPONSE ANALYSIS ===")
    total_calls = sum(len(latencies) for latencies in REQUEST_STATS.values())

    for endpoint, latencies in sorted(REQUEST_STATS.items()):
        if not latencies:
            continue
        avg = statistics.mean(latencies)
        min_lat = min(latencies)
        max_lat = max(latencies)
        jitter = statistics.stdev(latencies) if len(latencies) > 1 else 0
        count = len(latencies)
        logger.info(
            f"{endpoint:45} | count={count:3} | AVG={avg:6.2f} ms | "
            f"MIN={min_lat:6.2f} | MAX={max_lat:6.2f} | JITTER={jitter:6.2f}"
        )

    logger.info(f"Total REST calls processed: {total_calls}")
    logger.info(f"Full request log saved to latency_full.log ({len(FULL_REQUEST_LOG)} entries)")


# =============================
# TRACEROUTE
# =============================


def traceroute(host="clob.polymarket.com"):
    traceroute_bin = shutil.which("traceroute")
    if traceroute_bin:
        cmd = [traceroute_bin, "-n", "-q", "3", host]
        tool_name = "traceroute"
    else:
        tracepath_bin = shutil.which("tracepath")
        if not tracepath_bin:
            logger.warning("Neither traceroute nor tracepath is installed; skipping route diagnostics.")
            return
        cmd = [tracepath_bin, host]
        tool_name = "tracepath"

    logger.info("Running %s to CLOB...", tool_name)
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        logger.info("\n" + result.stdout)
    except Exception as e:
        logger.error(f"Traceroute error: {e}")


# =============================
# MAIN TEST
# =============================


async def run_test():
    token_ids = fetch_markets_sync()
    if not token_ids:
        logger.error("No markets loaded")
        return

    valid_tokens = await asyncio.to_thread(validate_token_ids_sync, token_ids)
    if not valid_tokens:
        logger.error("No token_ids with active orderbook after validation")
        return

    client = ClobClient(CLOB_HOST)
    rest_latencies = []
    ws_latencies = []
    status_counts: defaultdict[int | None, int] = defaultdict(int)

    ws_tasks = []
    for i in range(0, len(valid_tokens), WS_BATCH_SIZE):
        batch = valid_tokens[i : i + WS_BATCH_SIZE]
        ws_tasks.append(asyncio.create_task(polymarket_ws_listener(ws_latencies, batch)))

    start_time = time.time()
    i = 0
    logger.info("=== Starting FULL MAPPING + LATENCY TEST ===")

    try:
        while time.time() - start_time < TEST_DURATION:
            token_id = valid_tokens[i % len(valid_tokens)]
            latency, status = await measure_clob_latency(client, token_id)
            status_counts[status] += 1
            if latency is not None:
                rest_latencies.append(latency)
            await asyncio.sleep(REQUEST_INTERVAL)
            i += 1
    except KeyboardInterrupt:
        logger.info("Test interrupted by user")
    finally:
        for task in ws_tasks:
            task.cancel()
        await asyncio.sleep(0.5)

    logger.info("=== FINAL LATENCY RESULTS ===")
    if rest_latencies:
        logger.info(
            f"CLOB REST AVG={statistics.mean(rest_latencies):.2f} ms | "
            f"MIN={min(rest_latencies):.2f} | MAX={max(rest_latencies):.2f} | "
            f"JITTER={statistics.stdev(rest_latencies) if len(rest_latencies) > 1 else 0:.2f}"
        )

    if ws_latencies:
        logger.info(
            f"POLYMARKET WS AVG={statistics.mean(ws_latencies):.2f} ms | "
            f"MIN={min(ws_latencies):.2f} | MAX={max(ws_latencies):.2f} | "
            f"JITTER={statistics.stdev(ws_latencies) if len(ws_latencies) > 1 else 0:.2f}"
        )

    logger.info(
        "REST status counters: 200=%d 404(no-book)=%d errors=%d",
        status_counts.get(200, 0),
        status_counts.get(404, 0),
        status_counts.get(None, 0),
    )

    print_request_analysis()
    traceroute()


if __name__ == "__main__":
    asyncio.run(run_test())
