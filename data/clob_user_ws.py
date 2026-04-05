"""Polymarket CLOB **user** WebSocket — authenticated order and trade updates.

Endpoint: ``wss://ws-subscriptions-clob.polymarket.com/ws/user`` (see
https://docs.polymarket.com/market-data/websocket/user-channel).

Subscribes with L2 API creds (`apiKey`, `secret`, `passphrase`) and optional ``markets``
(condition IDs). Text ``PING`` every ~10 s. Parses ``order`` and ``trade`` events into a
thread-safe cache so ``LiveExecutionEngine._get_order_fill`` can avoid HTTP polling when fresh.

**Placement** and **cancel** remain REST-only (Polymarket does not expose these over user WS).
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import random
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

import websockets

CLOB_USER_WS_URL = os.getenv(
    "CLOB_USER_WS_URL",
    "wss://ws-subscriptions-clob.polymarket.com/ws/user",
)
_CLOB_USER_WS_OPEN_TIMEOUT = float(os.getenv("CLOB_USER_WS_OPEN_TIMEOUT_SEC", "30"))


class OrderState(Enum):
    """Order state machine states for event-driven tracking."""
    PENDING = "pending"  # Order placed, waiting for fill
    PARTIAL = "partial"  # Partially filled
    FILLED = "filled"    # Fully filled
    CANCELLED = "cancelled"  # Order cancelled
    FAILED = "failed"    # Order failed
    STALE = "stale"      # Order stale, awaiting reprice/exit


class OrderEventType(Enum):
    """Types of order events from WebSocket."""
    PLACEMENT = "placement"
    UPDATE = "update"
    CANCELLATION = "cancellation"
    TRADE = "trade"
    STATUS_CHANGE = "status_change"


@dataclass(slots=True)
class OrderStateInfo:
    """Complete order state information with event history."""
    order_id: str
    state: OrderState
    status: str
    filled_size: float
    original_size: float
    placed_at: float = 0.0
    last_updated: float = 0.0
    event_count: int = 0
    ws_events_received: int = 0
    http_fallback_count: int = 0
    last_ws_event_ts: float = 0.0
    last_http_poll_ts: float = 0.0


def _norm_oid(oid: str) -> str:
    s = str(oid).strip().lower()
    if s.startswith("0x"):
        return s
    return s


def _parse_size_fields(msg: dict[str, Any]) -> tuple[float, float]:
    """Return (size_matched, original_size) with optional 1e6 micro scaling."""
    sm = _float(msg.get("size_matched"))
    orig = _float(msg.get("original_size"))
    if orig > 1000:  # micro
        sm = sm / 1_000_000.0
        orig = orig / 1_000_000.0
    return sm, orig


def _float(x: Any) -> float:
    if x is None:
        return 0.0
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return 0.0


def _extract_server_sequence(msg: dict[str, Any]) -> int | None:
    """Return Polymarket (or future) server stream sequence if present in the payload.

    Public docs do not guarantee a field; when absent, gap detection is skipped.
    """
    for key in ("seq", "sequence", "sequence_number", "sequenceNumber", "msg_seq"):
        v = msg.get(key)
        if v is None:
            continue
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if n >= 0:
            return n
    return None


def _auth_dict(creds: Any) -> dict[str, str] | None:
    if creds is None:
        return None
    key = getattr(creds, "api_key", None) or getattr(creds, "apiKey", None)
    sec = getattr(creds, "api_secret", None) or getattr(creds, "secret", None)
    phrase = getattr(creds, "api_passphrase", None) or getattr(creds, "passphrase", None)
    if not key or not sec or not phrase:
        return None
    return {
        "apiKey": str(key),
        "secret": str(sec),
        "passphrase": str(phrase),
    }


class ClobUserOrderCache:
    """Thread-safe order id → {status, filled} from user-channel WebSocket.

    Supports event-driven order tracking via callbacks for immediate notification
    of order events (PLACEMENT, UPDATE, CANCELLATION, TRADE).
    
    Implements a complete order state machine with:
    - Event-driven state transitions
    - Server sequence gap detection when the feed includes a sequence field
    - Reconnection event buffering
    - Latency metrics collection
    - Comprehensive logging
    - Order lifecycle tracking
    """

    def __init__(self, creds_getter: Callable[[], Any]) -> None:
        self._creds_getter = creds_getter
        self.enabled = os.getenv("CLOB_USER_WS_ENABLED", "1").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        # Staleness thresholds
        self._max_stale_sec = float(os.getenv("CLOB_USER_WS_MAX_STALE_SEC", "25"))
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
        
        self._ping_interval = float(os.getenv("CLOB_USER_WS_PING_SEC", "10"))
        self._lock = threading.RLock()
        self._orders: dict[str, dict[str, Any]] = {}
        self._state_machine: dict[str, OrderStateInfo] = {}
        self._markets: list[str] = []
        self._ws: Any = None
        self._stop = asyncio.Event()
        # Callback for event-driven order tracking: callback(order_id, status, filled)
        self._callback: Callable[[str, str, float], None] | None = None
        
        # Event-driven waiting: order_id -> list of asyncio.Event to set on update
        self._order_events: dict[str, list[asyncio.Event]] = {}
        
        # Server-side stream sequence (gap detection when payloads include a seq field)
        self._last_sequence_seen: int = 0
        self._sequence_gaps_detected: int = 0
        self._client_annotation_seq: int = 0  # optional _seq stamp; not used for gap logic
        
        # Reconnection event buffering
        self._reconnect_buffer: collections.deque[dict[str, Any]] = collections.deque(maxlen=1000)
        self._is_reconnecting: bool = False
        self._buffer_during_reconnect: bool = False
        
        # Order lifecycle tracking: order_id -> creation_time
        self._pending_orders: dict[str, float] = {}
        
        # Metrics tracking
        self._ws_events_total: int = 0
        self._http_fallbacks_total: int = 0
        self._ws_latency_samples: list[float] = []
        self._max_latency_samples = 1000
        self._events_by_type: dict[OrderEventType, int] = defaultdict(int)
        self._state_transitions: dict[tuple[OrderState, OrderState], int] = defaultdict(int)
        self._last_metrics_log = time.time()
        self._metrics_log_interval = 60.0  # Log metrics every 60 seconds
        self._max_order_entries = max(1, int(os.getenv("CLOB_USER_WS_MAX_ORDER_ENTRIES", "5000")))

    def _trim_order_caches_locked(self) -> None:
        """Evict oldest-order rows when ``_orders`` grows past ``_max_order_entries``."""
        n = len(self._orders)
        if n <= self._max_order_entries:
            return
        excess = n - self._max_order_entries
        sorted_ids = sorted(
            self._orders.keys(),
            key=lambda k: float(self._orders[k].get("ts", 0.0)),
        )
        for oid in sorted_ids[:excess]:
            self._orders.pop(oid, None)
            self._state_machine.pop(oid, None)
            self._order_events.pop(oid, None)

    def set_markets(self, markets: list[str]) -> None:
        """Replace condition ids (hex) for subscription filter."""
        with self._lock:
            self._markets = [str(m).strip() for m in markets if m and str(m).strip()]

    async def set_markets_async(self, markets: list[str]) -> None:
        new_m = [str(m).strip() for m in markets if m and str(m).strip()]
        with self._lock:
            old = list(self._markets)
        if new_m == old:
            return
        self.set_markets(new_m)
        await self.close_ws()

    def get_order_fill(self, order_id: str) -> tuple[str, float] | None:
        """Return ``(status_lower, filled_size)`` if cache has fresh data, else None.

        Pure WebSocket event-driven — no HTTP fallback.
        Returns None if no fresh cache available, caller should continue waiting.
        """
        if not order_id:
            return None
        oid = _norm_oid(order_id)
        with self._lock:
            # Check if we have fresh cached data first
            row = self._orders.get(oid)
            if row is None:
                for k, v in self._orders.items():
                    if k == oid or k.endswith(oid) or oid.endswith(k):
                        row = v
                        break
            if row is not None:
                age = time.time() - float(row.get("ts", 0.0))
                # During reconnection, extend threshold by 2x to avoid skip gates
                effective_threshold = self._max_stale_sec * (2.0 if self._reconnecting else 1.0)
                if age <= effective_threshold:
                    # Fresh cache available, return it
                    return str(row.get("status", "unknown")), float(row.get("filled", 0.0))
            
            # No fresh cache — return None, caller should continue waiting for WS event
            return None

    def set_order_callback(self, callback: Callable[[str, str, float], None]) -> None:
        """Register callback for event-driven order notifications.

        Callback signature: callback(order_id, status, filled)
        Called immediately after cache update on order/trade events.
        """
        self._callback = callback

    def _init_order_state(self, oid: str, original_size: float = 0.0) -> OrderStateInfo:
        """Initialize or get existing order state info."""
        if oid not in self._state_machine:
            self._state_machine[oid] = OrderStateInfo(
                order_id=oid,
                state=OrderState.PENDING,
                status="pending",
                filled_size=0.0,
                original_size=original_size,
                placed_at=time.time(),
                last_updated=time.time(),
            )
        return self._state_machine[oid]

    def _transition_state(self, state_info: OrderStateInfo, new_state: OrderState,
                          status: str, filled: float) -> bool:
        """Transition order to new state, return True if state changed."""
        old_state = state_info.state
        if old_state == new_state:
            return False
        
        state_info.state = new_state
        state_info.status = status
        state_info.filled_size = filled
        state_info.last_updated = time.time()
        state_info.event_count += 1
        
        # Track state transition
        self._state_transitions[(old_state, new_state)] += 1
        
        # Log state transition with details
        logging.info(
            "[WS] Order state transition: id=%s %s → %s status=%s filled=%.4f/%.4f "
            "(events_received=%d http_fallbacks=%d)",
            state_info.order_id[:20],
            old_state.value,
            new_state.value,
            status,
            filled,
            state_info.original_size if state_info.original_size > 0 else filled,
            state_info.ws_events_received,
            state_info.http_fallback_count,
        )
        
        return True

    def _touch(self, oid: str, status: str, filled: float, original_size: float = 0.0,
                ws_latency_ms: float = 0.0) -> None:
        """Merge order state (best-effort) and invoke callback if registered."""
        now = time.time()
        with self._lock:
            self._orders[oid] = {
                "status": status,
                "filled": filled,
                "ts": now,
            }
            
            # Update state machine
            state_info = self._init_order_state(oid, original_size)
            state_info.filled_size = filled
            state_info.last_updated = now
            state_info.last_ws_event_ts = now
            state_info.ws_events_received += 1
            
            # Track latency
            if ws_latency_ms > 0:
                self._ws_latency_samples.append(ws_latency_ms)
                if len(self._ws_latency_samples) > self._max_latency_samples:
                    self._ws_latency_samples.pop(0)
            
            self._ws_events_total += 1
            
            # Track message rate
            self._message_rate_window.append(now)
            if len(self._message_rate_window) > self._max_rate_samples:
                self._message_rate_window.pop(0)
            self._message_count += 1
            
            # Determine state from status
            status_lower = status.lower()
            if status_lower in ("matched", "filled"):
                new_state = OrderState.FILLED
                # Order filled - complete pending tracking
                self._pending_orders.pop(oid, None)
            elif status_lower in ("canceled", "cancelled"):
                new_state = OrderState.CANCELLED
                # Order cancelled - complete pending tracking
                self._pending_orders.pop(oid, None)
            elif status_lower == "partially_matched":
                new_state = OrderState.PARTIAL
            elif status_lower == "live":
                new_state = OrderState.PENDING
                # Order is live - register as pending if not already
                if oid not in self._pending_orders:
                    self._pending_orders[oid] = time.time()
            elif status_lower == "failed":
                new_state = OrderState.FAILED
                # Order failed - complete pending tracking
                self._pending_orders.pop(oid, None)
            else:
                new_state = OrderState.PENDING
            
            self._transition_state(state_info, new_state, status_lower, filled)
            
            # Comprehensive logging for WS events
            log_level = logging.INFO if new_state in (OrderState.FILLED, OrderState.CANCELLED) else logging.DEBUG
            logging.log(
                log_level,
                "[WS] Event received: id=%s status=%s filled=%.4f/%s state=%s "
                "latency=%.2fms events_received=%d",
                oid[:20],
                status,
                filled,
                f"{original_size:.4f}" if original_size > 0 else "N/A",
                new_state.value,
                ws_latency_ms if ws_latency_ms > 0 else 0.0,
                state_info.ws_events_received,
            )
            self._trim_order_caches_locked()

        # Invoke callback outside the lock to avoid blocking other operations
        if self._callback is not None:
            try:
                self._callback(oid, status_lower, filled)
            except Exception as exc:
                logging.debug("Order callback error: %s", exc)
        
        # Notify any waiters that an order update has been received
        self._notify_order_update(oid)

    def _apply_order_msg(self, msg: dict[str, Any]) -> None:
        inner = str(msg.get("type") or "").upper()
        et = str(msg.get("event_type") or "").lower()
        if et != "order" and inner not in ("PLACEMENT", "UPDATE", "CANCELLATION"):
            return
        oid_raw = msg.get("id") or msg.get("order_id") or msg.get("orderID")
        if not oid_raw:
            return
        oid = _norm_oid(str(oid_raw))
        sm, orig = _parse_size_fields(msg)
        
        # Calculate WS latency (time between event and processing)
        event_ts = msg.get("ts") or msg.get("timestamp") or time.time()
        ws_latency_ms = (time.time() - event_ts) * 1000 if isinstance(event_ts, (int, float)) else 0.0
        
        if inner == "CANCELLATION":
            self._events_by_type[OrderEventType.CANCELLATION] += 1
            self._touch(oid, "canceled", sm, orig, ws_latency_ms)
            return
        if inner == "PLACEMENT":
            self._events_by_type[OrderEventType.PLACEMENT] += 1
            self._touch(oid, "live", sm, orig, ws_latency_ms)
            return
        if inner == "UPDATE":
            self._events_by_type[OrderEventType.UPDATE] += 1
            if orig > 0 and sm + 1e-9 >= orig:
                self._touch(oid, "matched", sm, orig, ws_latency_ms)
            elif sm > 1e-9:
                self._touch(oid, "partially_matched", sm, orig, ws_latency_ms)
            else:
                self._touch(oid, "live", sm, orig, ws_latency_ms)
            return
        self._touch(oid, "live", sm, orig, ws_latency_ms)

    def _apply_trade_msg(self, msg: dict[str, Any]) -> None:
        et = str(msg.get("event_type") or "").lower()
        if not et:
            ou = str(msg.get("type") or "").upper()
            if ou == "TRADE":
                et = "trade"
        if et != "trade":
            return
        st = str(msg.get("status") or "").upper()
        sz = _float(msg.get("size"))
        if sz > 1000:
            sz /= 1_000_000.0
        
        # Calculate WS latency
        event_ts = msg.get("ts") or msg.get("timestamp") or time.time()
        ws_latency_ms = (time.time() - event_ts) * 1000 if isinstance(event_ts, (int, float)) else 0.0
        
        taker = msg.get("taker_order_id") or msg.get("takerOrderId")
        if taker:
            oid = _norm_oid(str(taker))
            self._events_by_type[OrderEventType.TRADE] += 1
            if st == "MATCHED":
                self._touch(oid, "matched", sz, 0.0, ws_latency_ms)
            elif st in ("CONFIRMED",):
                self._touch(oid, "matched", sz, 0.0, ws_latency_ms)
            elif st in ("FAILED",):
                self._touch(oid, "failed", 0.0, 0.0, ws_latency_ms)
        for mo in msg.get("maker_orders") or []:
            if not isinstance(mo, dict):
                continue
            mid = mo.get("order_id") or mo.get("orderId")
            if not mid:
                continue
            oid = _norm_oid(str(mid))
            mamt = _float(mo.get("matched_amount"))
            if mamt > 1000:
                mamt /= 1_000_000.0
            if st == "MATCHED":
                self._touch(oid, "matched", mamt, 0.0, ws_latency_ms)

    def _handle_message_dict(self, msg: dict[str, Any]) -> None:
        try:
            et = str(msg.get("event_type") or "").lower()
            if not et:
                ou = str(msg.get("type") or "").upper()
                if ou == "TRADE":
                    et = "trade"
                elif ou in ("PLACEMENT", "UPDATE", "CANCELLATION"):
                    et = "order"
            if et == "order":
                self._apply_order_msg(msg)
            elif et == "trade":
                self._apply_trade_msg(msg)
        except Exception as exc:
            logging.debug("CLOB user WS: skip message: %s", exc)

    def _handle_raw(self, raw: str) -> None:
        """Alias for :meth:`handle_ws_message_with_sequence` (tests and legacy callers)."""
        self.handle_ws_message_with_sequence(raw)

    async def _send_subscribe(self, ws: Any) -> None:
        creds = self._creds_getter()
        auth = _auth_dict(creds)
        if not auth:
            logging.warning("CLOB user WS: no API credentials (derive from private key in live).")
            return
        payload: dict[str, Any] = {
            "auth": auth,
            "type": "user",
        }
        if self._markets:
            payload["markets"] = self._markets
        await ws.send(json.dumps(payload))
        logging.info(
            "CLOB user WS subscribed (markets=%s)",
            len(self._markets) if self._markets else "all",
        )

    async def _ping_loop(self, ws: Any) -> None:
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                await ws.send("PING")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.debug("CLOB user WS ping loop ended: %s", exc)

    async def close_ws(self) -> None:
        ws = self._ws
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass

    async def run_forever(self) -> None:
        if not self.enabled:
            logging.info("CLOB user WS disabled (CLOB_USER_WS_ENABLED=0).")
            return
        
        primary_url = CLOB_USER_WS_URL
        # Note: backup URL not typically used for user WS (auth required), but support it if configured
        backup_url = os.getenv("CLOB_USER_WS_BACKUP_URL", "")
        
        try:
            while not self._stop.is_set():
                creds = self._creds_getter()
                if _auth_dict(creds) is None:
                    await asyncio.sleep(1.0)
                    continue
                
                # Determine which URL to use
                url = primary_url if not self._backup_connected else (backup_url or primary_url)
                
                try:
                    async with websockets.connect(
                        url,
                        open_timeout=_CLOB_USER_WS_OPEN_TIMEOUT,
                        ping_interval=None,
                        ping_timeout=15,
                        close_timeout=5,
                        max_size=10 * 1024 * 1024,
                    ) as ws:
                        self._ws = ws
                        self._connection_start_time = time.time()
                        self._reconnect_attempt = 0
                        self._reconnecting = False
                        self._reconnect_start_time = None
                        
                        # Log connection success
                        if self._backup_connected and backup_url:
                            logging.info("CLOB user WS reverted to primary endpoint")
                            self._backup_connected = False
                        else:
                            logging.info("CLOB user WS connected: %s", url)
                        
                        await self._send_subscribe(ws)
                        self.stop_reconnect_buffering()
                        ping_task = asyncio.create_task(self._ping_loop(ws))
                        try:
                            async for message in ws:
                                if self._stop.is_set():
                                    break
                                if isinstance(message, bytes):
                                    message = message.decode("utf-8", errors="replace")
                                self.handle_ws_message_with_sequence(message)
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
                            "CLOB user WS primary connection failed after %d attempts — switching to backup endpoint",
                            self._reconnect_attempt,
                        )
                        self._backup_connected = True
                        # Reset attempt count for backup connection
                        self._reconnect_attempt = 0
                        self._reconnect_start_time = time.time()
                        await asyncio.sleep(0.1)  # Quick retry with backup
                    else:
                        logging.warning(
                            "CLOB user WS error: %s — reconnect in %.2fs (attempt=%d)",
                            exc,
                            delay,
                            self._reconnect_attempt,
                        )
                        # Buffer until subscribe completes on the next connection
                        self.start_reconnect_buffering()
                        await asyncio.sleep(delay)
                    
                    self._total_reconnects += 1
        except asyncio.CancelledError:
            raise
        finally:
            # Log final metrics
            self._log_metrics("shutdown")

    def stop(self) -> None:
        self._stop.set()

    async def wait_for_order_update(self, order_id: str, timeout: float | None = None) -> bool:
        """Wait for an order update event via WebSocket.
        
        Returns True if an event was received within timeout, False otherwise.
        This is used by LiveExecutionEngine for event-driven order tracking.
        """
        if timeout is None:
            timeout = 30.0
        
        oid = _norm_oid(order_id)
        event = asyncio.Event()
        
        with self._lock:
            if oid not in self._order_events:
                self._order_events[oid] = []
            self._order_events[oid].append(event)
        
        try:
            # Wait for the event to be set by _notify_order_update
            await asyncio.wait_for(event.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            logging.debug("[WS] Order update wait timeout: id=%s timeout=%.1fs", oid[:20], timeout)
            return False
        finally:
            # Clean up event from list
            with self._lock:
                if oid in self._order_events and event in self._order_events[oid]:
                    self._order_events[oid].remove(event)
                    if not self._order_events[oid]:
                        self._order_events.pop(oid, None)
    
    def _notify_order_update(self, order_id: str) -> None:
        """Notify all waiters that an order update has been received."""
        oid = _norm_oid(order_id)
        with self._lock:
            events = self._order_events.get(oid, []).copy()
        
        # Set all waiting events
        for event in events:
            try:
                event.set()
            except Exception:
                pass
    
    def get_order_state(self, order_id: str) -> OrderStateInfo | None:
        """Get complete order state information from state machine."""
        with self._lock:
            oid = _norm_oid(order_id)
            return self._state_machine.get(oid)

    def get_all_order_states(self) -> dict[str, OrderStateInfo]:
        """Get all order states."""
        with self._lock:
            return dict(self._state_machine)

    def get_metrics(self) -> dict[str, Any]:
        """Get WebSocket metrics."""
        with self._lock:
            ws_latency = self._ws_latency_samples
            avg_latency = sum(ws_latency) / len(ws_latency) if ws_latency else 0.0
            min_latency = min(ws_latency) if ws_latency else 0.0
            max_latency = max(ws_latency) if ws_latency else 0.0
            
            # Calculate rolling message rate
            if len(self._message_rate_window) > 1:
                time_span = self._message_rate_window[-1] - self._message_rate_window[0]
                msg_rate = len(self._message_rate_window) / max(time_span, 0.001)
            else:
                msg_rate = 0.0
            
            # Last message age
            if self._orders:
                latest_ts = max(float(row.get("ts", 0.0)) for row in self._orders.values())
                last_msg_age = time.time() - latest_ts
            else:
                last_msg_age = 0.0
            
            return {
                "ws_events_total": self._ws_events_total,
                "http_fallbacks_total": self._http_fallbacks_total,
                "ws_latency_avg_ms": round(avg_latency, 2),
                "ws_latency_min_ms": round(min_latency, 2),
                "ws_latency_max_ms": round(max_latency, 2),
                "ws_latency_samples": len(ws_latency),
                "events_by_type": {k.value: v for k, v in self._events_by_type.items()},
                "state_transitions": {
                    f"{k[0].value}→{k[1].value}": v
                    for k, v in self._state_transitions.items()
                },
                "active_orders": len(self._state_machine),
                "sequence_number": self._last_sequence_seen,
                "sequence_gaps_detected": self._sequence_gaps_detected,
                "pending_orders": len(self._pending_orders),
                "reconnect_buffer_size": len(self._reconnect_buffer),
                # Health metrics
                "uptime_sec": round((time.time() - self._connection_start_time) if self._connection_start_time else 0.0, 2),
                "reconnect_count": self._total_reconnects,
                "messages_per_sec": round(msg_rate, 2),
                "last_message_age_sec": round(last_msg_age, 2),
                "is_reconnecting": self._reconnecting,
            }
    
    def get_health_metrics(self) -> dict[str, Any]:
        """Return connection health metrics (subset of get_metrics)."""
        with self._lock:
            # Calculate rolling message rate
            if len(self._message_rate_window) > 1:
                time_span = self._message_rate_window[-1] - self._message_rate_window[0]
                msg_rate = len(self._message_rate_window) / max(time_span, 0.001)
            else:
                msg_rate = 0.0
            
            # Last message age
            if self._orders:
                latest_ts = max(float(row.get("ts", 0.0)) for row in self._orders.values())
                last_msg_age = time.time() - latest_ts
            else:
                last_msg_age = 0.0
            
            return {
                "uptime_sec": round((time.time() - self._connection_start_time) if self._connection_start_time else 0.0, 2),
                "reconnect_count": self._total_reconnects,
                "messages_per_sec": round(msg_rate, 2),
                "last_message_age_sec": round(last_msg_age, 2),
                "is_reconnecting": self._reconnecting,
                "backup_active": self._backup_connected,
            }
    
    def _log_health_metrics(self) -> None:
        """Log health metrics periodically."""
        now = time.time()
        if now - self._last_health_log < self._health_log_interval:
            return
        
        metrics = self.get_health_metrics()
        logging.info(
            "[WS_HEALTH] uptime=%.1fs reconnects=%d msg_rate=%.2f/s last_msg_age=%.2fs reconnecting=%s backup=%s",
            metrics["uptime_sec"],
            metrics["reconnect_count"],
            metrics["messages_per_sec"],
            metrics["last_message_age_sec"],
            metrics["is_reconnecting"],
            metrics["backup_active"],
        )
        self._last_health_log = now

    def _log_metrics(self, reason: str = "periodic") -> None:
        """Log WebSocket metrics."""
        metrics = self.get_metrics()
        logging.info(
            "[WS_METRICS] %s: events=%d http_fallbacks=%d "
            "avg_latency=%.2fms min=%.2fms max=%.2fms "
            "active_orders=%d events_by_type=%s",
            reason,
            metrics["ws_events_total"],
            metrics["http_fallbacks_total"],
            metrics["ws_latency_avg_ms"],
            metrics["ws_latency_min_ms"],
            metrics["ws_latency_max_ms"],
            metrics["active_orders"],
            metrics["events_by_type"],
        )

    def _next_client_annotation_seq(self) -> int:
        """Monotonic id for optional ``_seq`` on decoded payloads (debug only)."""
        with self._lock:
            self._client_annotation_seq += 1
            return self._client_annotation_seq

    def _track_server_sequences_in_payload(self, parsed: Any) -> None:
        """Run gap detection using server-provided sequence fields on each event dict."""
        if isinstance(parsed, dict):
            seq = _extract_server_sequence(parsed)
            if seq is not None:
                self._check_sequence_gap(seq)
            return
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    seq = _extract_server_sequence(item)
                    if seq is not None:
                        self._check_sequence_gap(seq)

    def _check_sequence_gap(self, seq: int) -> bool:
        """Compare *server* stream sequence to the last seen value; return True if a gap was detected."""
        with self._lock:
            gap_detected = (
                self._last_sequence_seen > 0 and seq > self._last_sequence_seen + 1
            )
            if gap_detected:
                gap = seq - self._last_sequence_seen - 1
                self._sequence_gaps_detected += gap
                logging.warning(
                    "[WS] Sequence gap detected: expected=%d got=%d gap=%d total_gaps=%d",
                    self._last_sequence_seen + 1, seq, gap, self._sequence_gaps_detected,
                )
            self._last_sequence_seen = seq
            return gap_detected

    def _buffer_event(self, msg: dict[str, Any]) -> None:
        """Buffer an event during reconnection."""
        with self._lock:
            if len(self._reconnect_buffer) >= self._reconnect_buffer.maxlen:
                logging.warning(
                    "[WS] Reconnect buffer full (%d), dropping oldest event",
                    self._reconnect_buffer.maxlen,
                )
            self._reconnect_buffer.append(msg)

    def _replay_buffered_events(self) -> int:
        """Replay buffered events after reconnection. Returns count of replayed events."""
        replayed = 0
        with self._lock:
            buffered = list(self._reconnect_buffer)
            self._reconnect_buffer.clear()
        
        for msg in buffered:
            try:
                if isinstance(msg, dict):
                    srv = _extract_server_sequence(msg)
                    if srv is not None:
                        self._check_sequence_gap(srv)
                    self._handle_message_dict(msg)
                    replayed += 1
            except Exception as exc:
                logging.debug("[WS] Error replaying buffered event: %s", exc)
        
        if replayed > 0:
            logging.info("[WS] Replayed %d buffered events after reconnection", replayed)
        return replayed

    def start_reconnect_buffering(self) -> None:
        """Begin buffering: call when the socket is down, before reconnect delay/connect."""
        with self._lock:
            self._buffer_during_reconnect = True
            self._is_reconnecting = True
        logging.info("[WS] Started reconnection event buffering")

    def stop_reconnect_buffering(self) -> int:
        """End buffering, replay queued dict events. Call after (re)subscribe on the new socket."""
        with self._lock:
            self._buffer_during_reconnect = False
            self._is_reconnecting = False
        return self._replay_buffered_events()

    def register_pending_order(self, order_id: str) -> None:
        """Register a new pending order for lifecycle tracking."""
        oid = _norm_oid(order_id)
        with self._lock:
            self._pending_orders[oid] = time.time()
        logging.debug("[WS] Registered pending order: id=%s", oid[:20])

    def complete_pending_order(self, order_id: str) -> None:
        """Mark a pending order as completed (filled/cancelled/failed)."""
        oid = _norm_oid(order_id)
        with self._lock:
            self._pending_orders.pop(oid, None)
        logging.debug("[WS] Completed pending order: id=%s", oid[:20])

    def get_pending_order_age(self, order_id: str) -> float | None:
        """Get the age of a pending order in seconds, or None if not pending."""
        oid = _norm_oid(order_id)
        with self._lock:
            created_at = self._pending_orders.get(oid)
            if created_at is None:
                return None
            return time.time() - created_at

    def get_stale_pending_orders(self, max_age_sec: float = 30.0) -> list[tuple[str, float]]:
        """Get list of (order_id, age_sec) for orders exceeding max_age_sec."""
        now = time.time()
        stale = []
        with self._lock:
            for oid, created_at in list(self._pending_orders.items()):
                age = now - created_at
                if age > max_age_sec:
                    stale.append((oid, age))
        return stale

    async def wait_for_fill_with_timeout(
        self, order_id: str, timeout: float = 3.0
    ) -> tuple[str, float] | None:
        """Wait for a specific order to be filled with configurable timeout.
        
        Uses asyncio.wait_for() for efficient event-driven waiting.
        Returns (status, filled_size) tuple when fill arrives, None on timeout.
        """
        oid = _norm_oid(order_id)
        
        # Check cache first
        cached = self.get_order_fill(oid)
        if cached is not None:
            status, filled = cached
            if status in ("matched", "filled", "partially_matched"):
                return cached
        
        # Wait for event-driven update
        event_received = await self.wait_for_order_update(oid, timeout=timeout)
        if event_received:
            return self.get_order_fill(oid)
        
        return None

    def handle_ws_message_with_sequence(self, raw: str) -> None:
        """Single entry point for each user-channel WebSocket text frame (used by ``run_forever``).

        Skips heartbeats, applies reconnect buffering (per-event dict), server sequence gap
        tracking, optional client ``_seq`` stamp, then dispatches to ``_handle_message_dict``.
        """
        if not raw or raw.strip().upper() == "PONG":
            return
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return
        
        # Buffer during reconnection (one deque entry per event dict; replay expects dicts).
        with self._lock:
            buffering = self._buffer_during_reconnect
        if buffering:
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        self._buffer_event(item)
                return
            if isinstance(parsed, dict):
                self._buffer_event(parsed)
            return

        self._track_server_sequences_in_payload(parsed)

        ann = self._next_client_annotation_seq()
        if isinstance(parsed, dict):
            parsed["_seq"] = ann
        elif isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    item["_seq"] = ann

        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    self._handle_message_dict(item)
        elif isinstance(parsed, dict):
            self._handle_message_dict(parsed)
