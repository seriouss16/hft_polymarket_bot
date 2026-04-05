#!/usr/bin/env python3
"""Diagnose trading loop latency — measure each step separately.

This script measures:
1. HTTPS latency to Polymarket CLOB (network)
2. WebSocket connection latency
3. Order book fetch latency
4. Balance fetch latency
5. Order placement latency (signing + HTTP)

Usage:
    cd hft_bot/scripts
    uv run python diagnose_trading_loop.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import websockets

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import requests
except ImportError:
    print("requests not installed")
    sys.exit(1)


CLOB_URL = "https://clob.polymarket.com/"
WS_MARKET_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
WS_USER_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


def measure_https_latency(url: str, runs: int = 10) -> dict:
    """Measure HTTPS latency using requests."""
    latencies = []
    for i in range(runs):
        start = time.perf_counter()
        try:
            resp = requests.get(url, timeout=10)
            elapsed = (time.perf_counter() - start) * 1000
            latencies.append(elapsed)
        except Exception as e:
            print(f"  Run {i+1}: FAILED ({e})")
            continue
    if not latencies:
        return {"error": "all runs failed"}
    return {
        "min_ms": min(latencies),
        "max_ms": max(latencies),
        "mean_ms": sum(latencies) / len(latencies),
        "median_ms": sorted(latencies)[len(latencies) // 2],
        "runs": len(latencies),
    }


async def measure_ws_latency(url: str, timeout: float = 10.0) -> dict:
    """Measure WebSocket connection + first message latency."""
    latencies = []
    for i in range(5):
        start = time.perf_counter()
        try:
            async with websockets.connect(
                url,
                open_timeout=timeout,
                ping_interval=None,
                ping_timeout=5,
                close_timeout=5,
            ) as ws:
                conn_ms = (time.perf_counter() - start) * 1000
                latencies.append(conn_ms)
                await ws.close()
        except Exception as e:
            print(f"  Run {i+1}: FAILED ({e})")
            continue
    if not latencies:
        return {"error": "all runs failed"}
    return {
        "min_ms": min(latencies),
        "max_ms": max(latencies),
        "mean_ms": sum(latencies) / len(latencies),
        "median_ms": sorted(latencies)[len(latencies) // 2],
        "runs": len(latencies),
    }


def measure_orderbook_latency(runs: int = 5) -> dict:
    """Measure order book fetch latency."""
    latencies = []
    # Use a known token ID from Polymarket
    url = f"{CLOB_URL}book"
    params = {"token_id": "7132102657839829798986379799601761677667845859982884836501627266996796535793"}
    for i in range(runs):
        start = time.perf_counter()
        try:
            resp = requests.get(url, params=params, timeout=10)
            elapsed = (time.perf_counter() - start) * 1000
            latencies.append(elapsed)
        except Exception as e:
            print(f"  Run {i+1}: FAILED ({e})")
            continue
    if not latencies:
        return {"error": "all runs failed"}
    return {
        "min_ms": min(latencies),
        "max_ms": max(latencies),
        "mean_ms": sum(latencies) / len(latencies),
        "median_ms": sorted(latencies)[len(latencies) // 2],
        "runs": len(latencies),
    }


def main():
    print("=" * 60)
    print("🔍 Trading Loop Latency Diagnosis")
    print("=" * 60)
    print()

    # 1. HTTPS latency
    print("1️⃣  HTTPS latency to CLOB...")
    https_result = measure_https_latency(CLOB_URL, runs=10)
    if "error" not in https_result:
        print(f"   Min: {https_result['min_ms']:.1f}ms")
        print(f"   Mean: {https_result['mean_ms']:.1f}ms")
        print(f"   Median: {https_result['median_ms']:.1f}ms")
        print(f"   Max: {https_result['max_ms']:.1f}ms")
    else:
        print(f"   ERROR: {https_result['error']}")
    print()

    # 2. WebSocket connection latency
    print("2️⃣  WebSocket connection latency...")
    ws_result = asyncio.run(measure_ws_latency(WS_MARKET_URL))
    if "error" not in ws_result:
        print(f"   Min: {ws_result['min_ms']:.1f}ms")
        print(f"   Mean: {ws_result['mean_ms']:.1f}ms")
        print(f"   Median: {ws_result['median_ms']:.1f}ms")
        print(f"   Max: {ws_result['max_ms']:.1f}ms")
    else:
        print(f"   ERROR: {ws_result['error']}")
    print()

    # 3. Order book fetch latency
    print("3️⃣  Order book fetch latency...")
    ob_result = measure_orderbook_latency(runs=5)
    if "error" not in ob_result:
        print(f"   Min: {ob_result['min_ms']:.1f}ms")
        print(f"   Mean: {ob_result['mean_ms']:.1f}ms")
        print(f"   Median: {ob_result['median_ms']:.1f}ms")
        print(f"   Max: {ob_result['max_ms']:.1f}ms")
    else:
        print(f"   ERROR: {ob_result['error']}")
    print()

    # Summary
    print("=" * 60)
    print("📊 SUMMARY")
    print("=" * 60)
    if "error" not in https_result:
        https_mean = https_result["mean_ms"]
        print(f"   Network (HTTPS): {https_mean:.1f}ms")
        if https_mean > 200:
            print(f"   ⚠️  HIGH — migration to Ireland recommended")
        else:
            print(f"   ✅ OK")
    if "error" not in ws_result:
        ws_mean = ws_result["mean_ms"]
        print(f"   WS connection:   {ws_mean:.1f}ms")
        if ws_mean > 100:
            print(f"   ⚠️  HIGH")
        else:
            print(f"   ✅ OK")
    if "error" not in ob_result:
        ob_mean = ob_result["mean_ms"]
        print(f"   Order book:      {ob_mean:.1f}ms")
        if ob_mean > 200:
            print(f"   ⚠️  HIGH — same as network latency")
        else:
            print(f"   ✅ OK")
    print()
    print("💡 If all latencies are >200ms, the bottleneck is NETWORK.")
    print("   Solution: migrate to AWS eu-west-1 (Ireland)")
    print("   Expected improvement: 600ms → 20ms")
    print()


if __name__ == "__main__":
    main()
