"""Polymarket CLOB market-channel WebSocket: live order book cache (WS-first, HTTP fallback in live_engine).

Endpoint: ``wss://ws-subscriptions-clob.polymarket.com/ws/market`` (no auth).
Heartbeats: send text ``PING`` every 10s (see Polymarket CLOB websocket docs).

Events used: ``book`` (full snapshot), ``price_change`` (level updates), optional ``best_bid_ask``.

``book`` (Polymarket docs): ``event_type``, ``asset_id``, ``market``, ``bids`` / ``asks`` as arrays of
``{ "price": ".48", "size": "30" }`` (strings allowed), ``timestamp``, ``hash``. Emitted on subscribe
and when a trade affects the book.

``best_bid_ask`` (requires ``custom_feature_enabled: true``): ``event_type``, ``market``, ``asset_id``,
``best_bid``, ``best_ask``, ``spread``, ``timestamp``. Emitted when best prices change; we merge into
the L2 cache by moving top-of-book size when prices shift, or seed placeholder sizes if no ``book`` yet.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any

import websockets

from core.live_common import _snapshot_from_levels

CLOB_MARKET_WS_URL = os.getenv(
    "CLOB_MARKET_WS_URL",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market",
)


def _parse_num(x: Any) -> float:
    if x is None:
        return 0.0
    s = str(x).strip()
    if not s:
        return 0.0
    return float(s)


def _levels_from_side_rows(rows: Any) -> dict[float, float]:
    """Map price -> size for one side (CLOB ``book`` event: list of ``{price, size}``)."""
    out: dict[float, float] = {}
    if rows is None:
        return out
    if not isinstance(rows, list):
        logging.debug(
            "CLOB market WS: bids/asks must be a list, got %s",
            type(rows).__name__,
        )
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        p = _parse_num(row.get("price"))
        sz = _parse_num(row.get("size"))
        if sz <= 0.0:
            continue
        if p > 0.0:
            out[p] = sz
    return out


class ClobMarketBookCache:
    """Thread-safe in-memory L2 books per ``asset_id`` (CLOB token id) from market WebSocket."""

    def __init__(self) -> None:
        self.enabled = os.getenv("CLOB_MARKET_WS_ENABLED", "1").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        self._max_stale_sec = float(os.getenv("CLOB_BOOK_WS_MAX_STALE_SEC", "12"))
        self._reconnect_sec = float(os.getenv("CLOB_MARKET_WS_RECONNECT_SEC", "2"))
        self._ping_interval = float(os.getenv("CLOB_MARKET_WS_PING_SEC", "10"))
        self._custom_features = os.getenv("CLOB_MARKET_WS_CUSTOM_FEATURES", "1").strip().lower() in (
            "1",
            "true",
            "yes",
        )

        self._lock = threading.RLock()
        self._bids: dict[str, dict[float, float]] = {}
        self._asks: dict[str, dict[float, float]] = {}
        self._last_ts: dict[str, float] = {}
        self._asset_ids: list[str] = []
        self._ws: Any = None
        self._stop = asyncio.Event()

    def set_asset_ids(self, ids: list[str]) -> None:
        """Replace subscribed token ids (call from any thread before async close)."""
        with self._lock:
            self._asset_ids = [x for x in ids if x]
            for k in list(self._bids.keys()):
                if k not in self._asset_ids:
                    self._bids.pop(k, None)
                    self._asks.pop(k, None)
                    self._last_ts.pop(k, None)

    async def set_asset_ids_async(self, ids: list[str]) -> None:
        """Update subscription list and reconnect WebSocket (call from async main loop)."""
        new_ids = [x for x in ids if x]
        with self._lock:
            old = list(self._asset_ids)
        if new_ids == old:
            return
        self.set_asset_ids(new_ids)
        await self.close_ws()

    def snapshot(self, token_id: str, depth: int) -> dict[str, Any] | None:
        """Return the same shape as ``_snapshot_from_levels`` or None if no book yet."""
        if not token_id:
            return None
        with self._lock:
            bids = self._bids.get(token_id)
            asks = self._asks.get(token_id)
            if not bids and not asks:
                return None
            bid_levels = sorted(bids.items(), key=lambda x: x[0], reverse=True) if bids else []
            ask_levels = sorted(asks.items(), key=lambda x: x[0]) if asks else []
        return _snapshot_from_levels(bid_levels, ask_levels, depth)

    def get_snapshot_with_imbalance(self, token_id: str, depth: int = 5) -> dict[str, Any] | None:
        """Return snapshot with top-N volume and imbalance metrics expected by HFTEngine.

        Calculates bid_vol_topn, ask_vol_topn, imbalance, and pressure fields.
        Returns None if no book data available.
        """
        snap = self.snapshot(token_id, depth)
        if snap is None:
            return None
        # Calculate volumes from bid/ask levels
        bid_vol = sum(snap.get('bids', {}).values())
        ask_vol = sum(snap.get('asks', {}).values())
        total = bid_vol + ask_vol
        snap['bid_vol_topn'] = bid_vol
        snap['ask_vol_topn'] = ask_vol
        snap['imbalance'] = (bid_vol - ask_vol) / total if total > 0 else 0.0
        snap['pressure'] = bid_vol / total if total > 0 else 0.5
        return snap

    def _apply_snapshot(self, snap: dict[str, Any], token_id: str) -> None:
        """Apply a snapshot dict to the cache for a given token_id.

        Used by LiveExecutionEngine to update cache with fresh HTTP/SDK data
        when WebSocket is unavailable.
        """
        if not token_id or not snap:
            return
        bids_raw = snap.get('bids', {})
        asks_raw = snap.get('asks', {})
        bids = {float(p): float(s) for p, s in bids_raw.items()} if bids_raw else {}
        asks = {float(p): float(s) for p, s in asks_raw.items()} if asks_raw else {}
        with self._lock:
            self._bids[token_id] = bids
            self._asks[token_id] = asks
            self._touch(token_id)

    def is_fresh(self, token_id: str) -> bool:
        with self._lock:
            ts = self._last_ts.get(token_id)
        if ts is None:
            return False
        return (time.time() - ts) <= self._max_stale_sec

    def has_valid_pair(self, up_id: str, down_id: str | None) -> bool:
        if not up_id:
            return False
        if not self.is_fresh(up_id):
            return False
        if down_id:
            return self.is_fresh(down_id)
        return True

    def _touch(self, asset_id: str) -> None:
        self._last_ts[asset_id] = time.time()

    def _apply_book(self, msg: dict[str, Any]) -> None:
        """Replace L2 for ``asset_id`` from a ``book`` event (full snapshot)."""
        aid = str(msg.get("asset_id") or "")
        if not aid:
            return
        bids = _levels_from_side_rows(msg.get("bids"))
        asks = _levels_from_side_rows(msg.get("asks"))
        with self._lock:
            self._bids[aid] = bids
            self._asks[aid] = asks
            self._touch(aid)

    def _apply_price_change(self, msg: dict[str, Any]) -> None:
        for ch in msg.get("price_changes") or []:
            if not isinstance(ch, dict):
                continue
            aid = str(ch.get("asset_id") or "")
            if not aid:
                continue
            price = _parse_num(ch.get("price"))
            size = _parse_num(ch.get("size"))
            side = str(ch.get("side") or "").upper()
            with self._lock:
                bids = self._bids.setdefault(aid, {})
                asks = self._asks.setdefault(aid, {})
                if side == "BUY":
                    book = bids
                elif side == "SELL":
                    book = asks
                else:
                    continue
                if size <= 0.0:
                    book.pop(price, None)
                else:
                    book[price] = size
                self._touch(aid)

    def _apply_best_bid_ask(self, msg: dict[str, Any]) -> None:
        """Apply ``best_bid_ask`` (custom feature): refresh top-of-book prices for ``asset_id``.

        Schema: ``market``, ``asset_id``, ``best_bid``, ``best_ask``, ``spread``, ``timestamp``.
        Sizes are not included; if L2 already exists we move the previous top level's size to the new
        best price; otherwise we seed placeholder size ``1.0`` until a full ``book`` arrives.
        """
        aid = str(msg.get("asset_id") or "")
        if not aid:
            return
        bb = _parse_num(msg.get("best_bid"))
        ba = _parse_num(msg.get("best_ask"))
        if bb <= 0.0 and ba <= 0.0:
            return
        with self._lock:
            bids = self._bids.setdefault(aid, {})
            asks = self._asks.setdefault(aid, {})
            if bb > 0.0:
                if not bids:
                    bids[bb] = 1.0
                else:
                    old = max(bids.keys())
                    if abs(old - bb) > 1e-12:
                        sz = float(bids.pop(old, 0.0))
                        if sz <= 0.0:
                            sz = 1.0
                        bids[bb] = bids.get(bb, 0.0) + sz
            if ba > 0.0:
                if not asks:
                    asks[ba] = 1.0
                else:
                    old = min(asks.keys())
                    if abs(old - ba) > 1e-12:
                        sz = float(asks.pop(old, 0.0))
                        if sz <= 0.0:
                            sz = 1.0
                        asks[ba] = asks.get(ba, 0.0) + sz
            self._touch(aid)

    def _handle_message_dict(self, msg: dict[str, Any]) -> None:
        """Dispatch one decoded JSON object (Polymarket may batch multiple events in a list)."""
        try:
            et = str(msg.get("event_type") or msg.get("type") or "").lower()
            if et == "book":
                self._apply_book(msg)
            elif et == "price_change":
                self._apply_price_change(msg)
            elif et == "best_bid_ask":
                self._apply_best_bid_ask(msg)
        except Exception as exc:
            logging.debug("CLOB market WS: skip message: %s", exc)

    def _handle_raw(self, raw: str) -> None:
        if not raw or raw == "PONG":
            return
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return
        # Server may send a JSON array of events instead of a single object.
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    self._handle_message_dict(item)
            return
        if isinstance(parsed, dict):
            self._handle_message_dict(parsed)

    async def _send_subscribe(self, ws: Any, asset_ids: list[str]) -> None:
        payload = {
            "assets_ids": asset_ids,
            "type": "market",
            "custom_feature_enabled": self._custom_features,
        }
        await ws.send(json.dumps(payload))
        logging.info(
            "CLOB market WS subscribed: %d asset(s), custom_feature=%s",
            len(asset_ids),
            self._custom_features,
        )

    async def _ping_loop(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                await ws.send("PING")
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def close_ws(self) -> None:
        """Force reconnect with new asset list (async)."""
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def run_forever(self) -> None:
        """Background task: maintain one market WS connection and update books."""
        if not self.enabled:
            logging.info("CLOB market WS disabled (CLOB_MARKET_WS_ENABLED=0).")
            return
        try:
            while not self._stop.is_set():
                ids = list(self._asset_ids)
                if not ids:
                    await asyncio.sleep(0.25)
                    continue
                try:
                    async with websockets.connect(
                        CLOB_MARKET_WS_URL,
                        ping_interval=None,
                        close_timeout=5,
                        max_size=10 * 1024 * 1024,
                    ) as ws:
                        self._ws = ws
                        await self._send_subscribe(ws, ids)
                        ping_task = asyncio.create_task(self._ping_loop(ws))
                        try:
                            async for message in ws:
                                if self._stop.is_set():
                                    break
                                if isinstance(message, bytes):
                                    message = message.decode("utf-8", errors="replace")
                                self._handle_raw(message)
                        finally:
                            ping_task.cancel()
                            try:
                                await ping_task
                            except asyncio.CancelledError:
                                pass
                            self._ws = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logging.warning(
                        "CLOB market WS connection error: %s — reconnect in %.1fs",
                        exc,
                        self._reconnect_sec,
                    )
                    await asyncio.sleep(self._reconnect_sec)
        except asyncio.CancelledError:
            raise

    def stop(self) -> None:
        self._stop.set()


def sync_poly_book_from_cache(
    poly_book: dict[str, Any],
    cache: ClobMarketBookCache,
    token_up_id: str,
    token_down_id: str | None,
    *,
    depth: int = 5,
    loop_ts: float | None = None,
) -> bool:
    """Merge fresh WS snapshots into ``poly_book`` (same keys as HTTP pull path).

    Returns True when UP top-of-book is valid and DOWN is valid or absent.
    """
    ob_up = cache.snapshot(token_up_id, depth)
    if ob_up is None or not cache.is_fresh(token_up_id):
        return False
    up_bid = float(ob_up.get("best_bid", 0.0))
    up_ask = float(ob_up.get("best_ask", 0.0))
    up_valid = 0.0 < up_bid < up_ask <= 1.0
    if up_valid:
        poly_book["bid"] = up_bid
        poly_book["ask"] = up_ask
        poly_book["bid_size_top"] = float(ob_up.get("bid_size_top", 1.0))
        poly_book["ask_size_top"] = float(ob_up.get("ask_size_top", 1.0))
    else:
        poly_book.pop("bid", None)
        poly_book.pop("ask", None)

    down_valid = True
    if token_down_id:
        ob_down = cache.snapshot(token_down_id, depth)
        if ob_down is None or not cache.is_fresh(token_down_id):
            poly_book.pop("down_bid", None)
            poly_book.pop("down_ask", None)
            return False
        down_bid = float(ob_down.get("best_bid", 0.0))
        down_ask = float(ob_down.get("best_ask", 0.0))
        down_valid = 0.0 < down_bid < down_ask <= 1.0
        if down_valid:
            poly_book["down_bid"] = down_bid
            poly_book["down_ask"] = down_ask
            poly_book["down_bid_size_top"] = float(ob_down.get("bid_size_top", 0.0))
            poly_book["down_ask_size_top"] = float(ob_down.get("ask_size_top", 0.0))
        else:
            poly_book.pop("down_bid", None)
            poly_book.pop("down_ask", None)

    if loop_ts is not None:
        poly_book["ts"] = loop_ts
    ok = up_valid and (not token_down_id or down_valid)
    return bool(ok)
