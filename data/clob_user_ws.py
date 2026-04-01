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
import json
import logging
import os
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


@dataclass
class OrderEvent:
    """Represents an order event from WebSocket."""
    order_id: str
    event_type: OrderEventType
    status: str
    filled_size: float
    original_size: float
    timestamp: float
    ws_latency_ms: float = 0.0


@dataclass
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
    event_history: list[OrderEvent] = field(default_factory=list)
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
    - HTTP fallback tracking
    - Latency metrics collection
    - Comprehensive logging
    """

    def __init__(self, creds_getter: Callable[[], Any]) -> None:
        self._creds_getter = creds_getter
        self.enabled = os.getenv("CLOB_USER_WS_ENABLED", "1").strip().lower() in (
            "1",
            "true",
            "yes",
        )
        self._max_stale_sec = float(os.getenv("CLOB_USER_WS_MAX_STALE_SEC", "12"))
        self._reconnect_sec = float(os.getenv("CLOB_USER_WS_RECONNECT_SEC", "2"))
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
                if time.time() - float(row.get("ts", 0.0)) <= self._max_stale_sec:
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
        with self._lock:
            self._orders[oid] = {
                "status": status,
                "filled": filled,
                "ts": time.time(),
            }
            
            # Update state machine
            state_info = self._init_order_state(oid, original_size)
            state_info.filled_size = filled
            state_info.last_updated = time.time()
            state_info.last_ws_event_ts = time.time()
            state_info.ws_events_received += 1
            
            # Track latency
            if ws_latency_ms > 0:
                self._ws_latency_samples.append(ws_latency_ms)
                if len(self._ws_latency_samples) > self._max_latency_samples:
                    self._ws_latency_samples.pop(0)
            
            self._ws_events_total += 1
            
            # Determine state from status
            status_lower = status.lower()
            if status_lower in ("matched", "filled"):
                new_state = OrderState.FILLED
            elif status_lower in ("canceled", "cancelled"):
                new_state = OrderState.CANCELLED
            elif status_lower == "partially_matched":
                new_state = OrderState.PARTIAL
            elif status_lower == "live":
                new_state = OrderState.PENDING
            elif status_lower == "failed":
                new_state = OrderState.FAILED
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
        if not raw or raw.strip().upper() == "PONG":
            return
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, dict):
                    self._handle_message_dict(item)
            return
        if isinstance(parsed, dict):
            self._handle_message_dict(parsed)

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
        while not self._stop.is_set():
            creds = self._creds_getter()
            if _auth_dict(creds) is None:
                await asyncio.sleep(1.0)
                continue
            try:
                async with websockets.connect(
                    CLOB_USER_WS_URL,
                    open_timeout=_CLOB_USER_WS_OPEN_TIMEOUT,
                    ping_interval=None,
                    ping_timeout=15,
                    close_timeout=5,
                    max_size=10 * 1024 * 1024,
                ) as ws:
                    self._ws = ws
                    await self._send_subscribe(ws)
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
                    "CLOB user WS error: %s — reconnect in %.1fs",
                    exc,
                    self._reconnect_sec,
                )
                await asyncio.sleep(self._reconnect_sec)
        
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
            }

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
