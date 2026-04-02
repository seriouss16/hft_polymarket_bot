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
import math
import os
import random
import threading
import time
from typing import Any

import websockets

from core.live_common import _snapshot_from_levels

CLOB_MARKET_WS_URL = os.getenv(
    "CLOB_MARKET_WS_URL",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market",
)
CLOB_MARKET_WS_BACKUP_URL = os.getenv("CLOB_MARKET_WS_BACKUP_URL", "")
# Max seconds to wait for TCP + WebSocket handshake (avoids hanging forever on bad routes).
_CLOB_MARKET_WS_OPEN_TIMEOUT = float(os.getenv("CLOB_MARKET_WS_OPEN_TIMEOUT_SEC", "30"))


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
        # Staleness thresholds
        self._max_stale_sec = float(os.getenv("CLOB_MARKET_WS_MAX_STALE_SEC", "25"))
        self._stale_warn_sec = float(os.getenv("CLOB_WS_STALE_WARN_SEC", "15"))
        self._stale_skip_sec = float(os.getenv("CLOB_WS_STALE_SKIP_SEC", "25"))
        
        # Reconnection backoff parameters
        self._reconnect_base_sec = float(os.getenv("CLOB_WS_RECONNECT_BASE_SEC", "1"))
        self._reconnect_max_sec = float(os.getenv("CLOB_WS_RECONNECT_MAX_SEC", "30"))
        self._reconnect_jitter_ms = float(os.getenv("CLOB_WS_RECONNECT_JITTER_MS", "500"))
        self._reconnect_attempt = 0
        self._reconnect_start_time: float | None = None
        self._reconnecting = False
        self._backup_connected = False
        self._backup_ws: Any = None
        
        # Health monitoring
        self._health_log_interval = float(os.getenv("CLOB_WS_HEALTH_LOG_INTERVAL_SEC", "60"))
        self._last_health_log = time.time()
        self._connection_start_time: float | None = None
        self._total_reconnects = 0
        self._message_count = 0
        self._message_rate_window: list[float] = []  # timestamps for rolling rate
        self._max_rate_samples = 1000
        
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
        
        # --- HFT Optimization: Cached snapshot with dirty flag ---
        self._cache_enabled = os.getenv("HFT_CACHE_BOOK_SNAPSHOT", "1").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        self._top_n = int(os.getenv("HFT_BOOK_TOP_N", "5"))
        self._incremental_imbalance = os.getenv("HFT_INCREMENTAL_IMBALANCE", "1").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        self._cached_snapshot: dict[str, dict[str, Any] | None] = {}
        self._snapshot_dirty: dict[str, bool] = {}
        self._cached_imbalance: dict[str, float] = {}
        self._cached_top_bids: dict[str, list[tuple[float, float]]] = {}
        self._cached_top_asks: dict[str, list[tuple[float, float]]] = {}
        self._total_bid_volume: dict[str, float] = {}
        self._total_ask_volume: dict[str, float] = {}

    def set_asset_ids(self, ids: list[str]) -> None:
        """Replace subscribed token ids (call from any thread before async close)."""
        with self._lock:
            self._asset_ids = [x for x in ids if x]
            for k in list(self._bids.keys()):
                if k not in self._asset_ids:
                    self._bids.pop(k, None)
                    self._asks.pop(k, None)
                    self._last_ts.pop(k, None)
                    # Clear cache entries for removed assets
                    if self._cache_enabled:
                        self._snapshot_dirty.pop(k, None)
                        self._cached_snapshot.pop(k, None)
                        self._cached_top_bids.pop(k, None)
                        self._cached_top_asks.pop(k, None)
                        self._cached_imbalance.pop(k, None)
                        self._total_bid_volume.pop(k, None)
                        self._total_ask_volume.pop(k, None)

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
        
        # Check if we can use cached snapshot
        if (
            self._cache_enabled
            and depth == self._top_n
            and not self._snapshot_dirty.get(token_id, True)
        ):
            with self._lock:
                cached = self._cached_snapshot.get(token_id)
                if cached is not None:
                    return cached
        
        with self._lock:
            bids = self._bids.get(token_id)
            asks = self._asks.get(token_id)
            if not bids and not asks:
                return None
            
            # Use heapq.nlargest for top-N extraction (optimized)
            if self._cache_enabled and depth == self._top_n:
                import heapq
                # Get top N bids (highest prices)
                bid_items = heapq.nlargest(depth, bids.items(), key=lambda x: x[0])
                # Get top N asks (lowest prices)
                ask_items = heapq.nsmallest(depth, asks.items(), key=lambda x: x[0])
                bid_levels = bid_items
                ask_levels = ask_items
            else:
                bid_levels = sorted(bids.items(), key=lambda x: x[0], reverse=True)
                ask_levels = sorted(asks.items(), key=lambda x: x[0])
        
        snap = _snapshot_from_levels(bid_levels, ask_levels, depth)
        
        # Cache the snapshot if enabled
        if self._cache_enabled and depth == self._top_n:
            with self._lock:
                self._cached_snapshot[token_id] = snap
                self._cached_top_bids[token_id] = bid_levels
                self._cached_top_asks[token_id] = ask_levels
                # Calculate and cache incremental totals
                if self._incremental_imbalance:
                    bid_vol = sum(s for _, s in bid_levels)
                    ask_vol = sum(s for _, s in ask_levels)
                    self._total_bid_volume[token_id] = bid_vol
                    self._total_ask_volume[token_id] = ask_vol
                    # Pre-calculate imbalance
                    total = bid_vol + ask_vol
                    imbalance = (bid_vol - ask_vol) / total if total > 0 else 0.0
                    self._cached_imbalance[token_id] = imbalance
                self._mark_snapshot_clean(token_id)
        
        return snap

    def get_snapshot_with_imbalance(self, token_id: str, depth: int = 5) -> dict[str, Any] | None:
        """Return snapshot with top-N volume and imbalance metrics expected by HFTEngine.

        Uses cached snapshot and pre-calculated imbalance when available for performance.
        Calculates bid_vol_topn, ask_vol_topn, imbalance, and pressure fields.
        Returns None if no book data available.
        """
        snap = self.snapshot(token_id, depth)
        if snap is None:
            return None
        
        # Use cached imbalance and volumes if available and fresh
        if (
            self._cache_enabled
            and depth == self._top_n
            and not self._snapshot_dirty.get(token_id, True)
            and token_id in self._cached_imbalance
        ):
            snap['bid_vol_topn'] = self._total_bid_volume.get(token_id, 0.0)
            snap['ask_vol_topn'] = self._total_ask_volume.get(token_id, 0.0)
            snap['imbalance'] = self._cached_imbalance[token_id]
            total = snap['bid_vol_topn'] + snap['ask_vol_topn']
            snap['pressure'] = snap['bid_vol_topn'] / total if total > 0 else 0.5
        else:
            # Calculate volumes from bid/ask levels (fallback)
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
            # Invalidate cached snapshot for this token
            if self._cache_enabled:
                self._snapshot_dirty[token_id] = True
                self._cached_snapshot.pop(token_id, None)
                self._cached_top_bids.pop(token_id, None)
                self._cached_top_asks.pop(token_id, None)
                self._cached_imbalance.pop(token_id, None)
                self._total_bid_volume.pop(token_id, None)
                self._total_ask_volume.pop(token_id, None)

    def invalidate_cache(self, token_id: str) -> None:
        """Explicitly invalidate cached snapshot for a token (e.g., after external update)."""
        if self._cache_enabled:
            with self._lock:
                self._snapshot_dirty[token_id] = True
                self._cached_snapshot.pop(token_id, None)
                self._cached_top_bids.pop(token_id, None)
                self._cached_top_asks.pop(token_id, None)
                self._cached_imbalance.pop(token_id, None)
                self._total_bid_volume.pop(token_id, None)
                self._total_ask_volume.pop(token_id, None)

    def is_fresh(self, token_id: str) -> bool:
        """Check if token data is fresh, accounting for reconnection grace period."""
        with self._lock:
            ts = self._last_ts.get(token_id)
        if ts is None:
            return False
        age = time.time() - ts
        # During reconnection, extend threshold by 2x to avoid skip gates
        effective_threshold = self._max_stale_sec * (2.0 if self._reconnecting else 1.0)
        return age <= effective_threshold
    
    def get_health_metrics(self) -> dict[str, Any]:
        """Return connection health metrics."""
        now = time.time()
        uptime = (now - self._connection_start_time) if self._connection_start_time else 0.0
        
        # Calculate rolling message rate (messages/sec over recent window)
        if len(self._message_rate_window) > 1:
            time_span = self._message_rate_window[-1] - self._message_rate_window[0]
            msg_rate = len(self._message_rate_window) / max(time_span, 0.001)
        else:
            msg_rate = 0.0
        
        # Last message age: inf when no WS/book timestamps yet (not "fresh" at 0s).
        with self._lock:
            if self._last_ts:
                latest_ts = max(self._last_ts.values())
                last_msg_age = now - latest_ts
            else:
                last_msg_age = math.inf
        
        last_age_out: float = (
            round(last_msg_age, 2) if math.isfinite(last_msg_age) else last_msg_age
        )
        return {
            "uptime_sec": round(uptime, 2),
            "reconnect_count": self._total_reconnects,
            "messages_per_sec": round(msg_rate, 2),
            "last_message_age_sec": last_age_out,
            "is_reconnecting": self._reconnecting,
            "backup_active": self._backup_connected,
        }
    
    def _log_health_metrics(self) -> None:
        """Log health metrics periodically."""
        now = time.time()
        if now - self._last_health_log < self._health_log_interval:
            return
        
        metrics = self.get_health_metrics()
        lma = metrics["last_message_age_sec"]
        lma_s = f"{lma:.2f}" if isinstance(lma, float) and math.isfinite(lma) else "inf"
        logging.info(
            "[WS_HEALTH] uptime=%.1fs reconnects=%d msg_rate=%.2f/s last_msg_age=%ss reconnecting=%s backup=%s",
            metrics["uptime_sec"],
            metrics["reconnect_count"],
            metrics["messages_per_sec"],
            lma_s,
            metrics["is_reconnecting"],
            metrics["backup_active"],
        )
        self._last_health_log = now

    def has_valid_pair(self, up_id: str, down_id: str | None) -> bool:
        if not up_id:
            return False
        if not self.is_fresh(up_id):
            return False
        if down_id:
            return self.is_fresh(down_id)
        return True

    def _touch(self, asset_id: str) -> None:
        now = time.time()
        self._last_ts[asset_id] = now
        # Track message rate
        self._message_rate_window.append(now)
        # Keep window bounded
        if len(self._message_rate_window) > self._max_rate_samples:
            self._message_rate_window.pop(0)
        self._message_count += 1
        # Mark snapshot as dirty when book updates
        if self._cache_enabled:
            self._snapshot_dirty[asset_id] = True

    def _mark_snapshot_clean(self, asset_id: str) -> None:
        """Clear dirty flag after snapshot has been computed."""
        if self._cache_enabled and asset_id in self._snapshot_dirty:
            self._snapshot_dirty[asset_id] = False

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
            # Invalidate cached snapshot for this asset
            if self._cache_enabled:
                self._snapshot_dirty[aid] = True
                self._cached_snapshot.pop(aid, None)
                self._cached_top_bids.pop(aid, None)
                self._cached_top_asks.pop(aid, None)
                self._cached_imbalance.pop(aid, None)
                self._total_bid_volume.pop(aid, None)
                self._total_ask_volume.pop(aid, None)

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
                # Invalidate cached snapshot for this asset
                if self._cache_enabled:
                    self._snapshot_dirty[aid] = True
                    self._cached_snapshot.pop(aid, None)
                    self._cached_top_bids.pop(aid, None)
                    self._cached_top_asks.pop(aid, None)
                    self._cached_imbalance.pop(aid, None)
                    self._total_bid_volume.pop(aid, None)
                    self._total_ask_volume.pop(aid, None)

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
            # Invalidate cached snapshot for this asset
            if self._cache_enabled:
                self._snapshot_dirty[aid] = True
                self._cached_snapshot.pop(aid, None)
                self._cached_top_bids.pop(aid, None)
                self._cached_top_asks.pop(aid, None)
                self._cached_imbalance.pop(aid, None)
                self._total_bid_volume.pop(aid, None)
                self._total_ask_volume.pop(aid, None)

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
        except Exception as exc:
            logging.debug("CLOB market WS ping loop ended: %s", exc)

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
        
        primary_url = CLOB_MARKET_WS_URL
        backup_url = CLOB_MARKET_WS_BACKUP_URL if CLOB_MARKET_WS_BACKUP_URL else None
        
        try:
            while not self._stop.is_set():
                ids = list(self._asset_ids)
                if not ids:
                    await asyncio.sleep(0.25)
                    continue
                
                # Determine which URL to use
                url = primary_url if not self._backup_connected else (backup_url or primary_url)
                
                try:
                    async with websockets.connect(
                        url,
                        open_timeout=_CLOB_MARKET_WS_OPEN_TIMEOUT,
                        ping_interval=None,
                        close_timeout=5,
                        max_size=10 * 1024 * 1024,
                    ) as ws:
                        self._ws = ws
                        if self._backup_connected and backup_url:
                            logging.info("CLOB market WS connected to backup endpoint: %s", backup_url)
                            # Reset flag so next disconnect attempts primary first
                            self._backup_connected = False
                        else:
                            logging.info("CLOB market WS connected: %s", url)
                        # Log connection success
                        if self._backup_connected and backup_url:
                            logging.info("CLOB market WS reverted to primary endpoint")
                            self._backup_connected = False
                        else:
                            logging.info("CLOB market WS connected: %s", url)
                        
                        await self._send_subscribe(ws, ids)
                        ping_task = asyncio.create_task(self._ping_loop(ws))
                        try:
                            async for message in ws:
                                if self._stop.is_set():
                                    break
                                if isinstance(message, bytes):
                                    message = message.decode("utf-8", errors="replace")
                                self._handle_raw(message)
                                # Periodic health logging
                                self._log_health_metrics()
                        finally:
                            ping_task.cancel()
                            try:
                                await ping_task
                            except asyncio.CancelledError:
                                pass
                            self._ws = None
                            self._connection_start_time = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # Calculate exponential backoff with jitter
                    self._reconnect_attempt += 1
                    if self._reconnect_start_time is None:
                        self._reconnect_start_time = time.time()
                    
                    base = self._reconnect_base_sec
                    max_delay = self._reconnect_max_sec
                    jitter_ms = self._reconnect_jitter_ms
                    
                    # Exponential backoff: base * 2^attempt, capped at max
                    delay = min(base * (2 ** (self._reconnect_attempt - 1)), max_delay)
                    # Add jitter: random 0-jitter_ms
                    jitter = (random.random() if jitter_ms > 0 else 0.0) * (jitter_ms / 1000.0)
                    delay += jitter
                    
                    self._reconnecting = True
                    
                    # Check if we should try backup connection
                    if backup_url and not self._backup_connected and self._reconnect_attempt >= 3:
                        logging.warning(
                            "CLOB market WS primary connection failed after %d attempts — switching to backup endpoint",
                            self._reconnect_attempt,
                        )
                        self._backup_connected = True
                        # Reset attempt count for backup connection
                        self._reconnect_attempt = 0
                        self._reconnect_start_time = time.time()
                        await asyncio.sleep(0.1)  # Quick retry with backup
                    else:
                        logging.warning(
                            "CLOB market WS connection error: %s — reconnect in %.2fs (attempt=%d)",
                            exc,
                            delay,
                            self._reconnect_attempt,
                        )
                        await asyncio.sleep(delay)
                    
                    self._total_reconnects += 1
        except asyncio.CancelledError:
            raise

    def stop(self) -> None:
        self._stop.set()


def _valid_spread(bid: float, ask: float) -> bool:
    return 0.0 < bid < ask <= 1.0


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
    All-or-nothing: if the merge would fail (stale snapshot, invalid spread), ``poly_book``
    is left unchanged so callers never see a half-updated book.
    """
    ob_up = cache.snapshot(token_up_id, depth)
    if ob_up is None or not cache.is_fresh(token_up_id):
        return False
    up_bid = float(ob_up.get("best_bid", 0.0))
    up_ask = float(ob_up.get("best_ask", 0.0))
    if not _valid_spread(up_bid, up_ask):
        return False

    ob_down = None
    if token_down_id:
        ob_down = cache.snapshot(token_down_id, depth)
        if ob_down is None or not cache.is_fresh(token_down_id):
            return False
        down_bid = float(ob_down.get("best_bid", 0.0))
        down_ask = float(ob_down.get("best_ask", 0.0))
        if not _valid_spread(down_bid, down_ask):
            return False

    poly_book["bid"] = up_bid
    poly_book["ask"] = up_ask
    poly_book["bid_size_top"] = float(ob_up.get("bid_size_top", 1.0))
    poly_book["ask_size_top"] = float(ob_up.get("ask_size_top", 1.0))
    if token_down_id and ob_down is not None:
        poly_book["down_bid"] = float(ob_down.get("best_bid", 0.0))
        poly_book["down_ask"] = float(ob_down.get("best_ask", 0.0))
        poly_book["down_bid_size_top"] = float(ob_down.get("bid_size_top", 0.0))
        poly_book["down_ask_size_top"] = float(ob_down.get("ask_size_top", 0.0))

    if loop_ts is not None:
        poly_book["ts"] = loop_ts
    return True
