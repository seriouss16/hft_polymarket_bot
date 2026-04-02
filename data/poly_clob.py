"""Polymarket RTDS (Real-Time Data Socket) WebSocket for Chainlink crypto oracle prices.

Not the CLOB API — this is ``wss://ws-live-data.polymarket.com`` per
https://docs.polymarket.com/market-data/websocket/rtds

Subscribe: ``crypto_prices_chainlink``, ``type: "*"``, optional ``filters`` (JSON string for
e.g. ``{"symbol":"btc/usd"}``). Messages: ``topic``, ``type``, ``timestamp``, ``payload`` with
``symbol``, ``value``, ``timestamp``.

RTDS docs require **application ``PING`` every 5 seconds** (separate from WebSocket protocol pings).

Async HTTP Client: ClobAsyncHTTPClient provides non-blocking order book fetches
for the main loop to avoid blocking on synchronous HTTP calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

import websockets

try:
    import aiohttp
    _AIOHTTP_AVAILABLE = True
except ImportError:
    _AIOHTTP_AVAILABLE = False

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
        except Exception as exc:
            logging.debug("Poly RTDS ping loop ended: %s", exc)

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
                            ask = float(self.book.get("ask") or 0.0)
                            bid = float(self.book.get("bid") or 0.0)
                            if ask <= 0.0 or bid <= 0.0:
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


# ---------------------------------------------------------------------------
# Async HTTP client for CLOB order book (non-blocking, replaces sync requests)
# ---------------------------------------------------------------------------

_CLOB_BOOK_HTTP_URL = os.getenv("CLOB_BOOK_HTTP", "https://clob.polymarket.com/book")
_CLOB_BOOK_HTTP_TIMEOUT = float(os.getenv("LIVE_CLOB_BOOK_HTTP_TIMEOUT", "1.5"))


def _levels_from_book_rows(rows: Any) -> list[tuple[float, float]]:
    """Parse CLOB bids or asks JSON rows into (price, size) tuples."""
    out: list[tuple[float, float]] = []
    for row in rows or []:
        if isinstance(row, dict):
            out.append((float(row.get("price", 0.0)), float(row.get("size", 0.0))))
        else:
            out.append((float(getattr(row, "price", 0.0)), float(getattr(row, "size", 0.0))))
    return out


def _snapshot_from_levels(
    bid_levels: list[tuple[float, float]],
    ask_levels: list[tuple[float, float]],
    depth: int,
) -> dict:
    """Pick best bid (max price), best ask (min price), and top-of-book volumes."""
    bids = sorted(bid_levels, key=lambda x: x[0], reverse=True)
    asks = sorted(ask_levels, key=lambda x: x[0])
    best_bid = float(bids[0][0]) if bids else 0.0
    best_ask = float(asks[0][0]) if asks else 1.0
    bid_size_top = float(bids[0][1]) if bids else 0.0
    ask_size_top = float(asks[0][1]) if asks else 0.0
    bid_vol_topn = float(sum(s for _, s in bids[:depth]))
    ask_vol_topn = float(sum(s for _, s in asks[:depth]))
    den = bid_vol_topn + ask_vol_topn + 1e-9
    imbalance = (bid_vol_topn - ask_vol_topn) / den
    pressure = bid_size_top - ask_size_top
    return {
        "best_bid": best_bid,
        "best_ask": best_ask,
        "bid_size_top": bid_size_top,
        "ask_size_top": ask_size_top,
        "imbalance": imbalance,
        "bid_vol_topn": bid_vol_topn,
        "ask_vol_topn": ask_vol_topn,
        "pressure": pressure,
    }


class ClobAsyncHTTPClient:
    """Async HTTP client for CLOB order book fetches (non-blocking).

    This replaces the synchronous `requests` calls with aiohttp to avoid
    blocking the event loop during order book fetches in the main loop.
    """

    def __init__(self, timeout: float = _CLOB_BOOK_HTTP_TIMEOUT) -> None:
        self._timeout = aiohttp.ClientTimeout(total=timeout) if _AIOHTTP_AVAILABLE else None
        self._session: Optional[aiohttp.ClientSession] = None
        self._base_url = _CLOB_BOOK_HTTP_URL

    async def _ensure_session(self) -> None:
        """Ensure aiohttp session exists (lazy initialization)."""
        if self._session is None and _AIOHTTP_AVAILABLE:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def close(self) -> None:
        """Close the HTTP session."""
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch_orderbook(
        self, token_id: str, depth: int = 5
    ) -> Optional[dict]:
        """Fetch order book snapshot asynchronously.

        Returns dict with best_bid, best_ask, bid_size_top, ask_size_top, etc.
        Returns None on failure.
        """
        if not _AIOHTTP_AVAILABLE:
            logging.warning("aiohttp not available; async order book fetch disabled")
            return None

        await self._ensure_session()
        if self._session is None:
            return None

        empty = {
            "best_bid": 0.0,
            "best_ask": 1.0,
            "bid_size_top": 0.0,
            "ask_size_top": 0.0,
            "imbalance": 0.0,
            "bid_vol_topn": 0.0,
            "ask_vol_topn": 0.0,
            "pressure": 0.0,
        }

        try:
            async with self._session.get(
                self._base_url, params={"token_id": token_id}
            ) as resp:
                if resp.status != 200:
                    logging.debug("CLOB HTTP order book failed token=%s…: %d", token_id[:28], resp.status)
                    return empty
                data = await resp.json()
                bid_levels = _levels_from_book_rows(data.get("bids"))
                ask_levels = _levels_from_book_rows(data.get("asks"))
                return _snapshot_from_levels(bid_levels, ask_levels, depth)
        except asyncio.TimeoutError:
            logging.debug("CLOB HTTP order book timeout token=%s…", token_id[:28])
            return empty
        except Exception as exc:
            logging.debug("CLOB HTTP order book failed token=%s…: %s", token_id[:28], exc)
            return empty

    async def fetch_orderbook_pair(
        self,
        token_up_id: str,
        token_down_id: Optional[str],
        depth: int = 5,
    ) -> tuple[dict, dict]:
        """Fetch order books for UP and DOWN tokens concurrently.

        Returns (up_snapshot, down_snapshot). If token_down_id is None,
        returns (up_snapshot, empty_dict).
        """
        empty = {
            "best_bid": 0.0,
            "best_ask": 1.0,
            "bid_size_top": 0.0,
            "ask_size_top": 0.0,
            "imbalance": 0.0,
            "bid_vol_topn": 0.0,
            "ask_vol_topn": 0.0,
            "pressure": 0.0,
        }

        if not _AIOHTTP_AVAILABLE:
            return empty, empty

        if token_down_id:
            up_snap, down_snap = await asyncio.gather(
                self.fetch_orderbook(token_up_id, depth),
                self.fetch_orderbook(token_down_id, depth),
            )
            return up_snap or empty, down_snap or empty
        else:
            up_snap = await self.fetch_orderbook(token_up_id, depth)
            return up_snap or empty, empty
