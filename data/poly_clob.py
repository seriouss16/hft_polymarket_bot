"""Polymarket RTDS (Real-Time Data Socket) WebSocket for Chainlink crypto oracle prices.

Not the CLOB API — this is ``wss://ws-live-data.polymarket.com`` per
https://docs.polymarket.com/market-data/websocket/rtds

Subscribe: ``crypto_prices_chainlink``, ``type: "*"``, optional ``filters`` (JSON string for
e.g. ``{"symbol":"btc/usd"}``). Messages: ``topic``, ``type``, ``timestamp``, ``payload`` with
``symbol``, ``value``, ``timestamp``.

RTDS docs require **application ``PING`` every 5 seconds** (separate from WebSocket protocol pings).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

import websockets

# Default RTDS keepalive interval per Polymarket RTDS documentation.
_DEFAULT_RTDS_PING_SEC = 5.0


def _reconnect_delay_sec() -> float:
    raw = os.getenv("HFT_WS_RECONNECT_SEC")
    if raw is None or not str(raw).strip():
        return 2.0
    return float(raw)


def _chainlink_filters() -> str:
    """Return ``filters`` string for RTDS subscription (empty = all Chainlink symbols)."""
    raw = os.getenv("POLY_RTDS_CHAINLINK_FILTERS", "").strip()
    if raw:
        return raw
    # Optional: subscribe only to BTC/USD to cut traffic (JSON per RTDS docs).
    if os.getenv("POLY_RTDS_BTC_ONLY", "0").strip() in ("1", "true", "yes"):
        return '{"symbol":"btc/usd"}'
    return ""


class PolyOrderBook:
    """Hold oracle-driven book fields updated from RTDS ``wss://ws-live-data.polymarket.com``."""

    def __init__(self, symbol: str = "bitcoin") -> None:
        """Initialize empty book state for the given asset symbol."""
        self.symbol = symbol
        self.book: dict[str, Any] = {
            "ask": 0.0,
            "bid": 0.0,
            "mid": 0.0,
            "btc_oracle": 0.0,
            "ask_size_top": 0.0,
            "bid_size_top": 0.0,
            "ts": 0.0,
        }
        self.url = os.getenv("POLY_RTDS_URL", "wss://ws-live-data.polymarket.com")
        self._ping_sec = float(os.getenv("POLY_RTDS_PING_SEC", str(_DEFAULT_RTDS_PING_SEC)))

    async def _rtds_ping_loop(self, ws: Any) -> None:
        """Send text ``PING`` per RTDS docs (every ~5 s by default)."""
        try:
            while True:
                await asyncio.sleep(self._ping_sec)
                await ws.send("PING")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def connect(self) -> None:
        """Subscribe to RTDS Chainlink stream and update ``self.book`` until disconnect."""
        filters = _chainlink_filters()
        while True:
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=None,
                    ping_timeout=15,
                    close_timeout=5,
                ) as ws:
                    sub = {
                        "action": "subscribe",
                        "subscriptions": [
                            {
                                "topic": "crypto_prices_chainlink",
                                "type": "*",
                                "filters": filters,
                            }
                        ],
                    }
                    await ws.send(json.dumps(sub))
                    logging.info(
                        "✅ Poly RTDS connected (%s) filters=%r ping=%.1fs",
                        self.symbol,
                        filters or "(all chainlink symbols)",
                        self._ping_sec,
                    )
                    ping_task = asyncio.create_task(self._rtds_ping_loop(ws))
                    try:
                        async for msg in ws:
                            if not isinstance(msg, str) or not msg.strip():
                                continue
                            if msg.strip().upper() == "PONG":
                                continue
                            try:
                                data = json.loads(msg)
                            except json.JSONDecodeError:
                                logging.debug("Poly RTDS: skip non-JSON frame.")
                                continue
                            topic = data.get("topic")
                            if topic and topic != "crypto_prices_chainlink":
                                continue
                            if data.get("type") != "update":
                                continue
                            payload = data.get("payload") or {}
                            if not isinstance(payload, dict):
                                continue
                            if str(payload.get("symbol", "")).lower() != "btc/usd":
                                continue
                            price = float(payload["value"])
                            self.book["btc_oracle"] = price
                            self.book["mid"] = price
                            if float(self.book.get("ask") or 0.0) <= 0.0 or float(
                                self.book.get("bid") or 0.0
                            ) <= 0.0:
                                half_spread = 0.005
                                ym = 0.5
                                self.book["ask"] = min(0.99, ym + half_spread)
                                self.book["bid"] = max(0.01, ym - half_spread)
                            self.book["ts"] = asyncio.get_running_loop().time()
                            if float(self.book.get("ask_size_top") or 0.0) <= 0.0:
                                self.book["ask_size_top"] = 1.0
                            if float(self.book.get("bid_size_top") or 0.0) <= 0.0:
                                self.book["bid_size_top"] = 1.0
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass
            except asyncio.CancelledError:
                logging.info("🛑 Poly RTDS task cancelled; stopping feed.")
                raise
            except Exception as e:
                logging.error("❌ Poly RTDS Error: %s", e)
                await asyncio.sleep(_reconnect_delay_sec())
