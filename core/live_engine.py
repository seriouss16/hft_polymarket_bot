"""Live execution and risk controls for Polymarket CLOB.

Phase 2 WebSocket Migration: Fully event-driven order tracking.
- Removes HTTP polling for order status
- Implements complete order state machine using WebSocket events
- Adds comprehensive logging for WS/HTTP fallback events
- Adds latency metrics to track WS vs HTTP performance
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from core.live_common import (_CLOB_BOOK_HTTP_TIMEOUT, _ORDER_FILL_POLL_SEC,
                              _ORDER_MAX_REPRICE, _ORDER_STALE_SEC,
                              _REPRICE_POST_CANCEL_FILL_POLLS,
                              _REPRICE_POST_CANCEL_POLL_SEC,
                              _REPRICE_POST_CANCEL_SLEEP_SEC, BUY,
                              CLOB_BOOK_HTTP, SELL_SIDE, ClobClient,
                              LiveRiskManager, OrderArgs, OrderStatus,
                              OrderType, RestResponseEvent, TimerEvent,
                              TrackedOrder, WsOrderEvent,
                              _collateral_usd_from_balance_allowance_response,
                              _levels_from_book_rows,
                              _paper_aligned_buy_price_allows,
                              _parse_csv_floats, _parse_usdc_verify_delays,
                              _snapshot_from_levels, is_fresh_for_trading,
                              live_buy_reprice_tick, live_emergency_buy_bump,
                              live_emergency_cross_bump,
                              live_sell_reprice_tick)
from utils.env_config import req_float, req_int, req_str
from utils.resilience import CircuitBreaker, CircuitBreakerError, safe_task


class OrderFSM:
    """Finite State Machine for managing a single order's lifecycle."""

    def __init__(self, tracked: TrackedOrder, engine: LiveExecutionEngine) -> None:
        self.tracked = tracked
        self.engine = engine
        self._done_event = asyncio.Event()

    async def transition(self, event: WsOrderEvent | RestResponseEvent | TimerEvent) -> None:
        """Handle state transitions based on incoming events."""
        if self.tracked.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.FAILED,
            OrderStatus.STALE,
        ):
            return

        if isinstance(event, WsOrderEvent):
            await self._handle_ws_order(event)
        elif isinstance(event, RestResponseEvent):
            await self._handle_rest_response(event)
        elif isinstance(event, TimerEvent):
            await self._handle_timer(event)

        if self.tracked.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.FAILED,
            OrderStatus.STALE,
        ):
            self._done_event.set()

    async def _handle_ws_order(self, event: WsOrderEvent) -> None:
        status = event.status.lower().replace("order_status_", "")
        if status in ("matched", "filled"):
            self.tracked.status = OrderStatus.FILLED
            self.tracked.filled_size = min(self.tracked.size, event.filled)
            if not self.tracked.fill_ts:
                self.tracked.fill_ts = event.timestamp
            if not self.tracked.exit_ts:
                self.tracked.exit_ts = event.timestamp
        elif status in ("canceled", "cancelled"):
            self.tracked.status = OrderStatus.CANCELLED
            self.tracked.filled_size = event.filled
        elif status == "partially_matched":
            self.tracked.status = OrderStatus.PARTIAL
            self.tracked.filled_size = min(self.tracked.size, event.filled)
            self.tracked.placed_at = time.time()  # Reset stale timer
        elif status == "live":
            if not self.tracked.ack_ts:
                self.tracked.ack_ts = event.timestamp
            if self.tracked.status == OrderStatus.PLACING:
                self.tracked.status = OrderStatus.PENDING

    async def _handle_rest_response(self, event: RestResponseEvent) -> None:
        if not event.success:
            self.tracked.status = OrderStatus.FAILED
            return
        if event.order_id:
            self.tracked.order_id = event.order_id
        if event.status in ("matched", "filled"):
            self.tracked.status = OrderStatus.FILLED
            self.tracked.filled_size = self.tracked.size
            if not self.tracked.fill_ts:
                self.tracked.fill_ts = event.timestamp
            if not self.tracked.exit_ts:
                self.tracked.exit_ts = event.timestamp
        elif event.status == "live":
            if not self.tracked.ack_ts:
                self.tracked.ack_ts = event.timestamp
            if self.tracked.status == OrderStatus.PLACING:
                self.tracked.status = OrderStatus.PENDING

    async def _handle_timer(self, event: TimerEvent) -> None:
        if self.tracked.is_stale:
            await self._reprice_or_emergency()

    async def _reprice_or_emergency(self) -> None:
        """Execute reprice logic or emergency exit when stale."""
        tracked = self.tracked
        poly_min = req_float("POLY_CLOB_MIN_SHARES")

        # BUY partial fill below exchange minimum: cancel and FAK-sell dust (cannot reprice).
        if tracked.side == BUY and tracked.status == OrderStatus.PARTIAL:
            if 0 < tracked.filled_size < poly_min:
                self.engine._cancel_order(tracked.order_id)
                await self.engine._fak_sell(tracked.token_id, tracked.filled_size)
                tracked.filled_size = 0.0
                tracked.status = OrderStatus.CANCELLED
                return

        # SELL remainder below minimum GTC size: FAK the rest.
        if tracked.side == SELL_SIDE and tracked.status == OrderStatus.PARTIAL:
            rem = tracked.remaining
            if 0 < rem < poly_min:
                fak_filled = await self.engine._fak_sell(tracked.token_id, rem)
                tracked.filled_size += fak_filled
                tracked.status = OrderStatus.FILLED
                return

        if tracked.reprice_count >= _ORDER_MAX_REPRICE:
            logging.warning(
                "⚠️ Order %s stale after %d reprices — emergency exit.", tracked.order_id, tracked.reprice_count
            )
            self.engine._cancel_order(tracked.order_id)
            tracked.status = OrderStatus.STALE
            await self.engine._emergency_exit_order(tracked)
            return

        best_bid, best_ask = await asyncio.to_thread(self.engine.get_best_prices, tracked.token_id)

        if tracked.side == BUY:
            new_price = max(0.01, min(0.99, best_ask + 0.001))
            slip_raw = os.getenv("LIVE_MAX_BUY_REPRICE_SLIPPAGE", "").strip()
            entry_ask = tracked.entry_best_ask
            if slip_raw and entry_ask is not None and entry_ask > 0:
                max_slip = float(slip_raw)
                if new_price - entry_ask > max_slip + 1e-9:
                    logging.warning(
                        "BUY reprice aborted (slippage): new=%.4f entry_best_ask=%.4f max_slip=%.4f",
                        new_price,
                        entry_ask,
                        max_slip,
                    )
                    self.engine._cancel_order(tracked.order_id)
                    tracked.filled_size = 0.0
                    tracked.status = OrderStatus.CANCELLED
                    self.engine._last_buy_skip_reason = "slippage_abort"
                    return
        else:
            new_price = max(0.01, min(0.99, best_bid - 0.001))

        if abs(new_price - tracked.price) < 0.001:
            tracked.reprice_count += 1
            tracked.placed_at = time.time()
            return

        # Cancel old and place new
        old_id = tracked.order_id
        self.engine._cancel_order(old_id)
        await self.engine._recover_fill_after_cancel(tracked, old_id)

        if tracked.remaining <= 0:
            tracked.status = OrderStatus.FILLED
            return

        tracked.reprice_count += 1
        new_id, immediate = await self.engine._place_limit_raw(
            tracked.token_id, tracked.side, new_price, tracked.remaining
        )

        if new_id:
            # Update FSM mapping in engine
            self.engine._fsms.pop(old_id, None)
            tracked.order_id = new_id
            tracked.price = new_price
            tracked.placed_at = time.time()
            tracked.status = OrderStatus.FILLED if immediate else OrderStatus.PENDING
            self.engine._fsms[new_id] = self
            if not immediate:
                self.engine._event_queue.put_nowait(RestResponseEvent(order_id=new_id, success=True, status="live"))
        else:
            tracked.status = OrderStatus.FAILED

    async def wait(self) -> None:
        """Wait for the FSM to reach a terminal state."""
        await self._done_event.wait()


class LiveExecutionEngine:
    """Place safe limit orders against Polymarket CLOB with full order lifecycle management.

    Order lifecycle:
      1. execute() / close_position() places a GTC limit and tracks it as PENDING.
      2. _poll_order() polls fill status every LIVE_ORDER_FILL_POLL_SEC (see ``config/runtime.env``).
      3. If unfilled after LIVE_ORDER_STALE_SEC the order is repriced up to
         LIVE_ORDER_MAX_REPRICE times toward best market price.  For BUY, if
         LIVE_MAX_BUY_REPRICE_SLIPPAGE is set and the new limit would exceed that
         adverse move vs the original best_ask, the entry is cancelled instead.
      4. If still unfilled after all reprice attempts, emergency_exit() is called
         which cancels the stale order and places an aggressive market-crossing limit.
      5. emergency_exit() can also be triggered externally when the engine decides
         the position must close regardless of conditions.

    ``_last_buy_skip_reason`` is set on intentional BUY abort (e.g. slippage guard,
    ``usdc_debit_mismatch`` when collateral did not decrease after a reported fill)
    so the bot can avoid a live-skip cooldown; see ``LIVE_SKIP_COOLDOWN_ON_SLIPPAGE_ABORT``.

    On EXIT, ``wait_for_exit_readiness`` / ``probe_chain_shares_for_close`` reconcile
    ledger lag and partial fills before treating a position as phantom; see
    ``LIVE_CLOSE_WAIT_PENDING_SEC`` and ``LIVE_CLOSE_CHAIN_PROBE_DELAYS_SEC``.
    """

    def __init__(
        self,
        private_key: str | None,
        funder: str | None,
        test_mode: bool = True,
        min_order_size: float = 10.0,
        max_spread: float = 0.03,
        allowance_cache: Any | None = None,
    ) -> None:
        """Initialise execution engine and optionally connect to Polymarket CLOB.

        Phase 2 WebSocket Migration: Event-driven order tracking with HTTP fallback.
        """
        self.test_mode = test_mode
        self.min_order_size = min_order_size
        self.max_spread = max_spread
        self.max_entry_ask = req_float("HFT_MAX_ENTRY_ASK")
        self.stale_block_actions = os.getenv("LIVE_STALE_BLOCK_ACTIONS", "1") == "1"
        self.skip_stats_log_sec = req_float("HFT_LIVE_SKIP_STATS_LOG_SEC")
        self._last_skip_stats_log_ts = time.time()
        self._entry_stats: dict[str, int] = {
            "attempts": 0,
            "executed": 0,
            "skip_ask_cap": 0,
            "skip_spread": 0,
            "skip_signal": 0,
            "emergency_exits": 0,
            "reprice_total": 0,
        }
        # WebSocket/HTTP metrics tracking
        self._ws_metrics: dict[str, int] = {
            "ws_events_received": 0,
            "http_fallbacks": 0,
            "ws_latency_samples": 0,
            "ws_latency_total_ms": 0.0,
            "ws_latency_min_ms": float("inf"),
            "ws_latency_max_ms": 0.0,
        }
        self._http_metrics: dict[str, int] = {
            "http_polls_total": 0,
            "http_fallbacks_total": 0,
            "http_errors": 0,
        }
        self._active_orders: dict[str, TrackedOrder] = {}
        # Last confirmed BUY fills, keyed by token_id.  Persists after the order
        # leaves _active_orders so that close_position can still find the shares.
        # Cleared explicitly by clear_filled_buy() after a SELL completes.
        self._confirmed_buys: dict[str, float] = {}
        self._last_buy_skip_reason: str | None = None
        self.client = None
        # Removed unused requests.Session() — all HTTP goes through py_clob_client
        self._market_book_cache: object | None = None
        self._user_order_cache: object | None = None
        self._api_creds: object | None = None
        self._ws_metrics_last_log = time.time()
        self._ws_metrics_log_interval = 60.0  # Log metrics every 60 seconds
        self._allowance_cache = allowance_cache

        # Event Queue and Worker
        self._event_queue: asyncio.Queue = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._fsms: dict[str, OrderFSM] = {}

        # Circuit Breaker for Polymarket API
        self.circuit_breaker = CircuitBreaker(
            name="PolymarketAPI",
            error_threshold=(
                req_int("HFT_CIRCUIT_BREAKER_THRESHOLD") if os.getenv("HFT_CIRCUIT_BREAKER_THRESHOLD") else 5
            ),
            recovery_timeout=(
                req_float("HFT_CIRCUIT_BREAKER_RECOVERY_SEC") if os.getenv("HFT_CIRCUIT_BREAKER_RECOVERY_SEC") else 60.0
            ),
        )

        if ClobClient is None:
            if not self.test_mode:
                raise RuntimeError("py_clob_client is not installed.")
            return

        sig_type = req_int("POLY_SIGNATURE_TYPE")
        self.client = ClobClient(
            "https://clob.polymarket.com",
            key=private_key or "",
            chain_id=137,
            signature_type=sig_type,
            funder=funder or "",
        )
        if not self.test_mode:
            if not private_key or not funder:
                raise ValueError("LIVE_MODE=1 requires PRIVATE_KEY and FUNDER env vars.")
            # Always derive credentials from private key — they are canonical and always valid.
            # Explicit API keys in env may be stale (rotated/re-generated on Polymarket).
            derived = self.client.create_or_derive_api_creds()
            self.client.set_api_creds(derived)
            self._api_creds = derived
            logging.info(
                "[LIVE] ClobClient credentials derived from private key (key=...%s).",
                derived.api_key[-4:] if derived.api_key else "????",
            )

    @staticmethod
    def _validate_order_params(side: str, price: float, size: float) -> tuple[bool, str]:
        """Validate order parameters before sending to CLOB.

        Returns (is_valid, error_message).
        """
        if not math.isfinite(price):
            return False, f"price is not finite: {price}"
        if not math.isfinite(size):
            return False, f"size is not finite: {size}"
        if price <= 0.0:
            return False, f"price must be > 0, got {price}"
        if size <= 0.0:
            return False, f"size must be > 0, got {size}"
        if price > 1.0:
            return False, f"price must be <= 1.0 for Polymarket, got {price}"
        if side not in ("BUY", "SELL"):
            return False, f"invalid side: {side}"
        return True, ""

    def can_enter_position(self, token_id: str, side: str) -> bool:
        """Check if a new position can be entered for the given token/side.

        Anti-doubling safety gate: prevents entering a position if:
        - An active order already exists for the same token and side (BUY only for entries)
        - A confirmed position already exists for the token (from previous fill)

        Returns True if safe to enter, False otherwise.
        """
        # Normalize side for comparison (execute() only uses BUY for entries)
        check_side = side.upper()
        if check_side not in ("BUY", "BUY_UP", "BUY_DOWN"):
            logging.debug("[SAFETY] can_enter_position: non-entry side %s — allow", side)
            return True

        # Check for existing active BUY orders for this token
        for order in self._active_orders.values():
            if order.token_id == token_id and order.side == BUY:
                if order.status in (OrderStatus.PENDING, OrderStatus.PARTIAL):
                    logging.warning(
                        "[SAFETY] Anti-doubling: active BUY order %s exists for token %s (status=%s) — blocking new entry",
                        order.order_id[:12],
                        token_id[:12],
                        order.status,
                    )
                    return False

        # Check for confirmed position (from previous fill)
        if token_id in self._confirmed_buys:
            existing_shares = self._confirmed_buys[token_id]
            if existing_shares >= req_float("POLY_CLOB_MIN_SHARES"):
                logging.warning(
                    "[SAFETY] Anti-doubling: confirmed position exists for token %s (shares=%.4f) — blocking new entry",
                    token_id[:12],
                    existing_shares,
                )
                return False

        return True

    async def initialize(self) -> None:
        """Start the event worker task."""
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._event_worker())
            logging.info("[LIVE] Event worker started.")

    @safe_task(task_name="live_event_worker")
    async def _event_worker(self) -> None:
        """Main event loop for processing order and market events."""
        while True:
            try:
                event = await self._event_queue.get()
                if event is None:  # Shutdown signal
                    break

                # Dispatch event to relevant FSMs
                if isinstance(event, (WsOrderEvent, RestResponseEvent)):
                    order_id = event.order_id
                    if order_id and order_id in self._fsms:
                        await self._fsms[order_id].transition(event)
                elif isinstance(event, TimerEvent):
                    # Periodic check for all active FSMs
                    for fsm in list(self._fsms.values()):
                        await fsm.transition(event)

                self._event_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logging.error("[LIVE] Event worker error: %s", exc, exc_info=True)

    def get_api_creds(self) -> object | None:
        """L2 API credentials object for user-channel WebSocket (live only)."""
        return self._api_creds

    def set_user_order_cache(self, cache: object | None) -> None:
        """Optional user-channel WS cache (``data.clob_user_ws.ClobUserOrderCache``)."""
        self._user_order_cache = cache
        # Initialize callback for event-driven order tracking
        if cache is not None:
            self._init_user_order_cache()

    def set_market_book_cache(self, cache: object | None) -> None:
        """Optional CLOB market WebSocket cache (``data.clob_market_ws.ClobMarketBookCache``)."""
        self._market_book_cache = cache

    def set_allowance_cache(self, cache: Any | None) -> None:
        """Optional allowance cache (``data.balance_cache.ConditionalAllowanceCache``)."""
        self._allowance_cache = cache

    async def _ensure_allowance_cached(self, token_id: str) -> None:
        """Ensure conditional allowance using cache-first approach.

        If cache has a valid (non-expired) entry, skip the API call entirely.
        If cache is expired or missing, call the API and update cache.
        This eliminates blocking API calls from the critical path when
        the background refresh task has pre-warmed the cache.
        """
        if self.test_mode or self.client is None:
            return
        if self._allowance_cache is not None:
            cached = self._allowance_cache.get_cached_allowance(token_id)
            if cached is not None:
                # Cache hit (fresh or stale) — skip API call
                logging.debug(
                    "[ALLOWANCE] Cache hit for token=%s (allowance=%.0f)",
                    token_id[:20],
                    cached,
                )
                return
            # Cache miss or expired — call API and update cache
            logging.debug(
                "[ALLOWANCE] Cache miss for token=%s — calling API",
                token_id[:20],
            )
        # Call the API
        await asyncio.to_thread(self.ensure_conditional_allowance, token_id)
        # Update cache with a sentinel value (we don't know the exact allowance,
        # but we know it was refreshed successfully)
        if self._allowance_cache is not None:
            self._allowance_cache.set_allowance(token_id, 1.0)

    def ensure_allowances(self) -> None:
        """Refresh USDC (COLLATERAL) spending allowance for the CLOB at startup.

        Only COLLATERAL allowance is set globally; CONDITIONAL (CTF share) allowance
        requires a specific token_id and is refreshed per-trade via
        ``ensure_conditional_allowance(token_id)``.

        Safe to call repeatedly — the on-chain approval is idempotent.
        """
        if self.test_mode or self.client is None:
            return
        try:
            from py_clob_client.clob_types import (AssetType,
                                                   BalanceAllowanceParams)

            sig_type = req_int("POLY_SIGNATURE_TYPE")
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=sig_type,
            )
            resp = self.client.update_balance_allowance(params=params)
            logging.info("[LIVE] COLLATERAL allowance refreshed: %s", resp)
        except Exception as exc:
            logging.error(
                "[LIVE] ensure_allowances failed: %s — BUY orders may be rejected.",
                exc,
            )

    def ensure_conditional_allowance(self, token_id: str) -> None:
        """Refresh CTF conditional token allowance for a specific token_id.

        Must be called after a successful BUY fill so the CLOB accepts the
        subsequent SELL order.  The CONDITIONAL allowance is per-token and
        requires the exact token_id to be included in the params.
        """
        if self.test_mode or self.client is None:
            return
        try:
            from py_clob_client.clob_types import (AssetType,
                                                   BalanceAllowanceParams)

            sig_type = req_int("POLY_SIGNATURE_TYPE")
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=sig_type,
            )
            resp = self.client.update_balance_allowance(params=params)
            logging.info(
                "[LIVE] CONDITIONAL allowance refreshed: token=%s resp=%s",
                token_id[:20],
                resp,
            )
        except Exception as exc:
            logging.error(
                "[LIVE] ensure_conditional_allowance failed for token=%s: %s " "— SELL may be rejected.",
                token_id[:20],
                exc,
            )

    def fetch_conditional_balance(self, token_id: str) -> float | None:
        """Return on-chain conditional token balance for the given token_id.

        Polymarket deducts a protocol fee in CTF shares at the time of fill, so
        the actual spendable shares can be slightly less than the CLOB fill report.
        Querying ``get_balance_allowance`` with ``AssetType.CONDITIONAL`` + token_id
        returns the true wallet balance in micro-shares (divide by 1_000_000).

        Returns None when the call fails or in test_mode.
        """
        if self.test_mode or self.client is None:
            return None

        async def _fetch():
            from py_clob_client.clob_types import (AssetType,
                                                   BalanceAllowanceParams)

            sig_type = req_int("POLY_SIGNATURE_TYPE")
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=sig_type,
            )
            # Use to_thread because SDK calls are blocking
            resp = await asyncio.to_thread(self.client.get_balance_allowance, params=params)
            raw = resp.get("balance") if isinstance(resp, dict) else getattr(resp, "balance", None)
            if raw is None:
                return None
            bal = float(raw) / 1_000_000.0
            logging.debug(
                "[LIVE] Conditional balance: token=%s raw=%s → %.6f shares",
                token_id[:20],
                raw,
                bal,
            )
            return bal

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # If we are in an async context, we must use a Future or wait.
                # But fetch_conditional_balance is sync. This is a design conflict.
                # For now, we use run_coroutine_threadsafe if in a different thread,
                # or we just accept that sync calls to this from the main loop are bad.
                # However, most calls are from to_thread(fetch_conditional_balance).
                import threading

                if threading.current_thread() is threading.main_thread():
                    # This is dangerous if called from the main thread's async loop.
                    # But the engine's sync methods shouldn't be called from the main loop directly.
                    return loop.run_until_complete(self.circuit_breaker.call(_fetch))
                else:
                    # Called from a worker thread (via to_thread)
                    future = asyncio.run_coroutine_threadsafe(self.circuit_breaker.call(_fetch), loop)
                    return future.result()
            else:
                return loop.run_until_complete(self.circuit_breaker.call(_fetch))
        except CircuitBreakerError:
            logging.warning("[CIRCUIT] fetch_conditional_balance skipped: Circuit is OPEN")
            return None
        except Exception as exc:
            logging.warning(
                "[LIVE] fetch_conditional_balance failed token=%s: %s",
                token_id[:20],
                exc,
            )
            return None

    def fetch_usdc_balance(self) -> float | None:
        """Return available USDC balance on the Polymarket CLOB account.

        Returns None when the client is unavailable or the call fails.
        In test_mode returns None (no real account to check).
        """
        if self.test_mode or self.client is None:
            return None

        async def _fetch():
            from py_clob_client.clob_types import (AssetType,
                                                   BalanceAllowanceParams)

            sig_type = req_int("POLY_SIGNATURE_TYPE")
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=sig_type,
            )
            resp = await asyncio.to_thread(self.client.get_balance_allowance, params=params)
            return _collateral_usd_from_balance_allowance_response(resp)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import threading

                if threading.current_thread() is threading.main_thread():
                    return loop.run_until_complete(self.circuit_breaker.call(_fetch))
                else:
                    future = asyncio.run_coroutine_threadsafe(self.circuit_breaker.call(_fetch), loop)
                    return future.result()
            else:
                return loop.run_until_complete(self.circuit_breaker.call(_fetch))
        except CircuitBreakerError:
            logging.warning("[CIRCUIT] fetch_usdc_balance skipped: Circuit is OPEN")
            return None
        except Exception as exc:
            logging.warning("fetch_usdc_balance failed: %s", exc)
            return None

    async def _verify_usdc_debit_after_buy(
        self,
        usdc_before: float | None,
        expected_spend_usd: float,
    ) -> bool:
        """Return True when CLOB collateral USDC dropped by ~expected spend after a BUY.

        Detects phantom fills / desync: CLOB reports a match but collateral did not
        move, so callers must not debit session PnL. Disabled in test_mode or when
        ``LIVE_USDC_DEBIT_VERIFY=0``. When ``usdc_before`` is None, verification is
        skipped (warn once). When all post-fetches fail, allows the fill (inconclusive).
        """
        if self.test_mode:
            return True
        if os.getenv("LIVE_USDC_DEBIT_VERIFY", "1") == "0":
            return True
        if expected_spend_usd <= 1e-9:
            return True
        if usdc_before is None:
            logging.warning(
                "[LIVE] USDC debit verify skipped — no pre-order balance snapshot.",
            )
            return True
        tol_abs = float(os.getenv("LIVE_USDC_DEBIT_VERIFY_ABS_USD", "0.12"))
        tol_rel = float(os.getenv("LIVE_USDC_DEBIT_VERIFY_REL", "0.025"))
        tol = max(tol_abs, tol_rel * expected_spend_usd)
        min_drop = max(0.0, expected_spend_usd - tol)
        delays = _parse_usdc_verify_delays()
        any_after = False
        last_after: float | None = None
        for i, delay in enumerate(delays):
            if delay > 0:
                await asyncio.sleep(delay)
            after = await asyncio.to_thread(self.fetch_usdc_balance)
            last_after = after
            if after is None:
                logging.warning(
                    "[LIVE] USDC debit verify attempt %d/%d: balance fetch failed",
                    i + 1,
                    len(delays),
                )
                continue
            any_after = True
            delta = usdc_before - after
            if delta < -1e-6:
                logging.error(
                    "[LIVE] USDC debit verify: balance rose during BUY " "(before=%.4f after=%.4f) — refusing fill.",
                    usdc_before,
                    after,
                )
                return False
            if delta + 1e-9 >= min_drop:
                logging.info(
                    "[LIVE] USDC debit verify OK: before=%.4f after=%.4f Δ=%.4f " "(expected≈%.4f tol±%.4f)",
                    usdc_before,
                    after,
                    delta,
                    expected_spend_usd,
                    tol,
                )
                return True
            logging.debug(
                "[LIVE] USDC debit verify attempt %d: Δ=%.4f < min %.4f — retry",
                i + 1,
                delta,
                min_drop,
            )
        if not any_after:
            logging.warning(
                "[LIVE] USDC debit verify inconclusive (all post-order fetches failed) " "— allowing fill.",
            )
            return True
        delta_final = usdc_before - last_after if last_after is not None else None
        logging.error(
            "[LIVE] USDC debit verify FAILED: pre=%.4f post=%.4f Δ=%s "
            "expected_spend≈%.4f (need Δ≥%.4f) — refusing BUY as filled "
            "(phantom CLOB / API lag). Check Portfolio; reconcile if shares exist.",
            usdc_before,
            last_after,
            f"{delta_final:.4f}" if delta_final is not None else "n/a",
            expected_spend_usd,
            min_drop,
        )
        return False

    def _affordable_buy_shares(self, price: float, desired_shares: float) -> float:
        """Return a BUY size at ``price`` that fits reported USDC collateral.

        Applies a small safety margin so the CLOB notional stays below balance.
        Rounds down to two decimals like ``execute()``.  Returns ``desired_shares``
        unchanged when balance is unknown (test mode or API failure).
        """
        if self.test_mode or price <= 0.0 or desired_shares <= 0.0:
            return desired_shares
        bal = self.fetch_usdc_balance()
        if bal is None or bal <= 0.0:
            return desired_shares
        safety = req_float("LIVE_BUY_COLLATERAL_SAFETY")
        max_notional = bal * safety
        max_shares = max_notional / price
        capped = min(desired_shares, max_shares)
        capped = float(int(capped * 100.0) / 100.0)
        return max(0.0, capped)

    def get_best_prices(self, token_id: str) -> tuple[float, float]:
        """Return best bid and best ask from CLOB order book."""
        snap = self.get_orderbook_snapshot(token_id, depth=1)
        return float(snap["best_bid"]), float(snap["best_ask"])

    def _orderbook_snapshot_http(self, token_id: str, depth: int, *, log_errors: bool = True) -> dict:
        """Fetch and summarize the order book from the public CLOB HTTP endpoint."""
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
            resp = self._http.get(
                CLOB_BOOK_HTTP,
                params={"token_id": token_id},
                timeout=_CLOB_BOOK_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            bid_levels = _levels_from_book_rows(data.get("bids"))
            ask_levels = _levels_from_book_rows(data.get("asks"))
            return _snapshot_from_levels(bid_levels, ask_levels, depth)
        except Exception as exc:
            log_fn = logging.warning if log_errors else logging.debug
            log_fn(
                "HTTP CLOB book failed token=%s…: %s",
                token_id[:28] if token_id else "",
                exc,
            )
            return empty

    def get_orderbook_snapshot(self, token_id: str, depth: int = 5) -> dict:
        """Return top-N orderbook metrics for imbalance and pressure.

        WS-first architecture: uses market WebSocket cache as primary source.
        HTTP/SDK fallback only when WS cache is unavailable, stale, or failed.
        Returns cached data even if slightly stale to avoid blocking on HTTP.
        """
        cache = self._market_book_cache
        ws_enabled = getattr(cache, "enabled", True) if cache else False
        ws_primary = os.getenv("CLOB_MARKET_WS_PRIMARY", "1").strip().lower() in ("1", "true", "yes")

        # 1. Try WS cache first (always)
        if cache is not None and ws_enabled:
            try:
                snap = cache.snapshot(token_id, depth)
                if snap is not None:
                    if cache.is_fresh(token_id):
                        return snap
                    else:
                        # Cache stale but we have data — return it to avoid blocking
                        # unless WS_PRIMARY=0, then try HTTP
                        if ws_primary:
                            logging.debug(
                                "[STALE_ORDERBOOK] [WS] Book cache stale for %s (age>%.1fs), returning cached data",
                                token_id[:8],
                                cache._max_stale_sec,
                            )
                            return snap
            except Exception as exc:
                logging.debug("Market book cache read failed: %s", exc)

        # 2. HTTP fallback only if no WS or WS failed AND we need fresh data
        if self.client is None:
            logging.warning("[WS_RECONNECT] WS cache unavailable, falling back to HTTP")
            return self._orderbook_snapshot_http(token_id, depth, log_errors=True)

        # Try SDK once, then HTTP
        try:
            book = self.client.get_order_book(token_id)
            bid_levels = _levels_from_book_rows(book.bids)
            ask_levels = _levels_from_book_rows(book.asks)
            fresh_snap = _snapshot_from_levels(bid_levels, ask_levels, depth)
            # Update WS cache with fresh data if available
            if cache is not None:
                try:
                    cache._apply_snapshot(fresh_snap, token_id)
                except Exception as e:
                    logging.debug("Failed to update WS cache with HTTP data: %s", e)
            return fresh_snap
        except Exception as exc:
            logging.warning("CLOB SDK order book failed: %s", exc)
            # Return last cached even if stale (better than empty)
            if cache is not None:
                try:
                    snap = cache.snapshot(token_id, depth)
                    if snap is not None:
                        return snap
                except Exception:
                    pass
            return self._orderbook_snapshot_http(token_id, depth, log_errors=False)

    def _get_order_fill(self, order_id: str) -> tuple[str, float]:
        """Return (status_str, filled_size) for an active order from CLOB.

        Uses the authenticated client.get_order() call which requires L2 credentials.
        The previous approach of GET /order/{id} always returned 404 — that endpoint
        does not exist on the Polymarket CLOB; the correct API is GET /order/{id}
        via the SDK which internally uses authenticated /orders?id= calls.
        Returns ("unknown", 0.0) when the client is unavailable or in test mode.
        """
        if self.test_mode or self.client is None:
            return "unknown", 0.0
        cache = self._user_order_cache
        if cache is not None and getattr(cache, "enabled", True):
            try:
                cached = cache.get_order_fill(order_id)
                if cached is not None:
                    return cached
            except Exception as exc:
                logging.debug("User order WS cache read failed order=%s: %s", order_id, exc)
        try:
            data = self.client.get_order(order_id)
            if not data:
                logging.warning("Order fill poll: empty response for order=%s.", order_id)
                return "unknown", 0.0
            status = str(data.get("status", "unknown")).upper()
            # Polymarket statuses: LIVE, MATCHED, CANCELED, ORDER_STATUS_MATCHED, etc.
            # Normalise to lowercase tokens our _poll_order understands.
            status_lower = status.lower().replace("order_status_", "")
            filled_raw = float(data.get("size_matched", 0.0) or 0.0)
            # size_matched is in raw shares (integer), original_size too — both unitless.
            # Divide by 1_000_000 only if original_size is very large (fixed-point format).
            original_raw = float(data.get("original_size", 1.0) or 1.0)
            if original_raw > 1000:
                # Fixed-point 6-decimal format used by some Polymarket API responses.
                filled_raw /= 1_000_000.0
            return status_lower, filled_raw
        except Exception as exc:
            logging.warning("Order fill poll failed order=%s: %s", order_id, exc)
            return "unknown", 0.0

    def _init_user_order_cache(self) -> None:
        """Initialize user order cache with callback for event-driven tracking."""
        if self._user_order_cache is not None:
            self._user_order_cache.set_order_callback(self._on_user_order_event)

    def _on_user_order_event(self, order_id: str, status: str, filled: float) -> None:
        """Callback from user WS cache when order/trade event arrives.

        Phase 3: Enqueue event for FSM processing.
        """
        self._ws_metrics["ws_events_received"] += 1
        event = WsOrderEvent(order_id=order_id, status=status, filled=filled)
        self._event_queue.put_nowait(event)

    def _track_ws_latency(self, latency_ms: float) -> None:
        """Track WebSocket latency metrics."""
        self._ws_metrics["ws_latency_samples"] += 1
        self._ws_metrics["ws_latency_total_ms"] += latency_ms
        if latency_ms < self._ws_metrics["ws_latency_min_ms"]:
            self._ws_metrics["ws_latency_min_ms"] = latency_ms
        if latency_ms > self._ws_metrics["ws_latency_max_ms"]:
            self._ws_metrics["ws_latency_max_ms"] = latency_ms

    def _get_ws_metrics(self) -> dict[str, Any]:
        """Get WebSocket metrics summary."""
        metrics = dict(self._ws_metrics)
        if metrics["ws_latency_samples"] > 0:
            metrics["ws_latency_avg_ms"] = round(metrics["ws_latency_total_ms"] / metrics["ws_latency_samples"], 2)
        else:
            metrics["ws_latency_avg_ms"] = 0.0
            metrics["ws_latency_min_ms"] = 0.0
        return metrics

    def _log_ws_metrics(self, reason: str = "periodic") -> None:
        """Log WebSocket metrics."""
        metrics = self._get_ws_metrics()
        ws_events = metrics["ws_events_received"]
        http_fallbacks = self._http_metrics["http_fallbacks_total"]
        ws_latency = metrics["ws_latency_avg_ms"]

        ws_rate = ws_events / (time.time() - self._ws_metrics_last_log) if ws_events > 0 else 0

        logging.info(
            "[WS_METRICS] %s: ws_events=%d http_fallbacks=%d "
            "ws_latency_avg=%.2fms min=%.2fms max=%.2fms "
            "ws_rate=%.2f/s",
            reason,
            ws_events,
            http_fallbacks,
            ws_latency,
            metrics["ws_latency_min_ms"],
            metrics["ws_latency_max_ms"],
            ws_rate,
        )
        self._ws_metrics_last_log = time.time()

    @staticmethod
    def _associate_trade_ids_from_order(data: dict) -> list[str]:
        """Parse ``associate_trades`` from a CLOB order dict into trade id strings."""
        raw = data.get("associate_trades") or data.get("associateTrades")
        if raw is None:
            return []
        if isinstance(raw, list):
            return [str(x).strip() for x in raw if str(x).strip()]
        if isinstance(raw, str):
            s = raw.strip()
            if not s:
                return []
            if s.startswith("["):
                try:
                    parsed = json.loads(s.replace("'", '"'))
                    if isinstance(parsed, list):
                        return [str(x).strip() for x in parsed if str(x).strip()]
                except json.JSONDecodeError:
                    pass
            return [s]
        return []

    def _vwap_from_trade_ids(self, trade_ids: list[str]) -> float | None:
        """Return volume-weighted average price (0–1) for Polymarket trade ids, or None."""
        if not trade_ids or self.test_mode or self.client is None:
            return None
        try:
            from py_clob_client.clob_types import TradeParams
        except ImportError:
            return None
        num = 0.0
        den = 0.0
        for tid in trade_ids:
            if not tid:
                continue
            try:
                rows = self.client.get_trades(TradeParams(id=str(tid)))
            except Exception as exc:
                logging.debug("get_trades id=%s: %s", tid[:16], exc)
                continue
            if not rows:
                continue
            for tr in rows:
                if not isinstance(tr, dict):
                    continue
                sz_raw = float(tr.get("size", 0.0) or 0.0)
                px = float(tr.get("price", 0.0) or 0.0)
                if sz_raw > 1000:
                    sz_raw /= 1_000_000.0
                if sz_raw > 0 and 0.01 <= px <= 0.99:
                    num += sz_raw * px
                    den += sz_raw
        if den <= 1e-12:
            return None
        return num / den

    def _sell_fill_avg_price(self, tracked: TrackedOrder, total_filled: float) -> float:
        """Execution VWAP for a filled SELL; ``tracked.price`` is the limit, not VWAP.

        Using the limit price overstates proceeds when the book lifts shares below
        the limit (common for GTC sells placed at bid+offset), which can flip a
        real loss into an apparent win in ``PnLTracker.live_close``.
        """
        if self.test_mode or self.client is None or total_filled <= 0:
            return tracked.price
        try:
            data = self.client.get_order(tracked.order_id)
        except Exception as exc:
            logging.debug("get_order for SELL VWAP: %s", exc)
            return tracked.price
        if not isinstance(data, dict):
            return tracked.price
        for key in (
            "avg_fill_price",
            "avg_price",
            "average_price",
            "execution_price",
            "fill_price",
            "average_match_price",
        ):
            raw = data.get(key)
            if raw is None or raw == "":
                continue
            try:
                px = float(raw)
                if 0.01 <= px <= 0.99:
                    logging.debug(
                        "[LIVE] SELL VWAP from order.%s=%.4f (limit was %.4f)",
                        key,
                        px,
                        tracked.price,
                    )
                    return px
            except (TypeError, ValueError):
                continue
        tids = self._associate_trade_ids_from_order(data)
        vwap = self._vwap_from_trade_ids(tids)
        if vwap is not None and 0.01 <= vwap <= 0.99:
            logging.info(
                "[LIVE] SELL VWAP from associate_trades (%d ids)=%.4f (limit was %.4f)",
                len(tids),
                vwap,
                tracked.price,
            )
            return vwap
        sm = float(data.get("size_matched", 0.0) or 0.0)
        o_sz = float(data.get("original_size", 0.0) or 0.0)
        if o_sz > 1000:
            sm /= 1_000_000.0
        for proceeds_key in (
            "quote_filled",
            "filled_amount_usd",
            "maker_amount_usd",
            "taker_amount_usd",
            "collateral",
        ):
            pv = data.get(proceeds_key)
            if pv is None or pv == "":
                continue
            try:
                usd = float(pv)
                if usd > 0 and sm > 1e-9:
                    px = usd / sm
                    if 0.01 <= px <= 0.99:
                        logging.info(
                            "[LIVE] SELL VWAP from %s / size_matched=%.4f",
                            proceeds_key,
                            px,
                        )
                        return px
            except (TypeError, ValueError):
                continue
        logging.warning(
            "⚠️ [LIVE] SELL VWAP fallback to limit price %.4f (order=%s). "
            "If UI shows a different avg, associate_trades may be empty — check CLOB.",
            tracked.price,
            tracked.order_id[:20],
        )
        return tracked.price

    def _cancel_order(self, order_id: str) -> bool:
        """Cancel an open order; return True on success."""
        if self.test_mode or self.client is None:
            logging.info("[SIM] Cancel order %s.", order_id)
            return True

        async def _cancel():
            await asyncio.to_thread(self.client.cancel, order_id)
            logging.info("[LIVE] Cancelled order %s.", order_id)
            return True

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import threading

                if threading.current_thread() is threading.main_thread():
                    return loop.run_until_complete(self.circuit_breaker.call(_cancel))
                else:
                    future = asyncio.run_coroutine_threadsafe(self.circuit_breaker.call(_cancel), loop)
                    return future.result()
            else:
                return loop.run_until_complete(self.circuit_breaker.call(_cancel))
        except CircuitBreakerError:
            logging.warning("[CIRCUIT] _cancel_order skipped: Circuit is OPEN")
            return False
        except Exception as exc:
            logging.warning("Cancel failed order=%s: %s", order_id, exc)
            return False

    async def cancel_all_orders(self) -> None:
        """Cancel all active orders — used by kill-switch for emergency shutdown.

        Iterates through ``_active_orders`` and attempts to cancel each one.
        Does not clear ``_confirmed_buys`` (position tracking) — that is handled
        separately by the shutdown logic if needed.
        """
        if not self._active_orders:
            logging.info("[LIVE] cancel_all_orders: no active orders to cancel")
            return

        logging.warning(
            "[LIVE] cancel_all_orders: cancelling %d active order(s)",
            len(self._active_orders),
        )
        # Cancel each order; collect order_ids for logging
        order_ids = list(self._active_orders.keys())
        for order_id in order_ids:
            try:
                self._cancel_order(order_id)
            except Exception as exc:
                logging.error("cancel_all_orders: failed to cancel order %s: %s", order_id[:12], exc)

        # Note: We do NOT clear _active_orders here because the FSM worker may still
        # process cancellation confirmations. The orders will be removed naturally
        # as callbacks fire or during subsequent cleanup.

    async def _recover_fill_after_cancel(
        self,
        tracked: TrackedOrder,
        cancelled_order_id: str,
        *,
        skip_initial_sleep: bool = False,
    ) -> bool:
        """Poll the cancelled order id for a fill that raced with cancel or API lag.

        After ``cancel``, the CLOB may still report the old order as matched or
        partially matched. Placing a replacement at full ``remaining`` size then
        fails with insufficient balance. This method waits briefly, polls
        ``get_order`` until a terminal fill amount is visible, updates
        ``tracked``, and returns True when no replacement limit order is needed
        for the original ``tracked.size`` (fully filled or remainder closed via
        FAK for sub-minimum SELL dust).

        Returns False when a new limit order should be placed for
        ``tracked.remaining`` (or when nothing matched after polling).
        """
        poly_min = req_float("POLY_CLOB_MIN_SHARES")
        if self.test_mode:
            return False
        if not skip_initial_sleep:
            await asyncio.sleep(_REPRICE_POST_CANCEL_SLEEP_SEC)

        async def _poll_once() -> tuple[str, float]:
            return await asyncio.to_thread(self._get_order_fill, cancelled_order_id)

        def _apply_matched_size(clob_filled: float) -> bool:
            """Update tracked from CLOB size_matched; return True if fully done."""
            if clob_filled <= tracked.filled_size + 1e-9:
                return False
            tracked.filled_size = min(tracked.size, clob_filled)
            tracked.status = OrderStatus.PARTIAL
            rem = tracked.remaining
            if rem <= 1e-6:
                tracked.status = OrderStatus.FILLED
                logging.info(
                    "[REST_RECONCILE] ✅ [LIVE] Fill synced after cancel: id=%s %s filled=%.4f @ %.4f",
                    cancelled_order_id[:20],
                    tracked.side,
                    tracked.filled_size,
                    tracked.price,
                )
                return True
            if tracked.side == SELL_SIDE and rem < poly_min:
                return False
            logging.info(
                "⚡ [LIVE] Partial fill after cancel: id=%s filled=%.4f rem=%.4f",
                cancelled_order_id[:20],
                tracked.filled_size,
                rem,
            )
            return False

        async def _fak_dust_if_needed() -> bool:
            """If SELL remainder is sub-minimum, FAK it and return True if done."""
            rem = tracked.remaining
            if rem > 1e-6 and tracked.side == SELL_SIDE and rem < poly_min:
                fak_filled = await self._fak_sell(tracked.token_id, rem)
                tracked.filled_size += fak_filled
                tracked.status = OrderStatus.FILLED
                logging.info(
                    "[LIVE] FAK closed sub-min remainder after cancel sync: %.4f",
                    fak_filled,
                )
                return True
            return False

        for attempt in range(_REPRICE_POST_CANCEL_FILL_POLLS):
            status_str, clob_filled = await _poll_once()
            if status_str in ("matched", "filled") or clob_filled >= tracked.size - 1e-6:
                tracked.filled_size = min(tracked.size, max(tracked.filled_size, clob_filled))
                tracked.status = OrderStatus.FILLED
                logging.info(
                    "✅ [LIVE] Fill synced after cancel: id=%s %s filled=%.4f / %.4f @ %.4f",
                    cancelled_order_id[:20],
                    tracked.side,
                    tracked.filled_size,
                    tracked.size,
                    tracked.price,
                )
                return True

            if status_str in ("partially_matched",) or (
                clob_filled > tracked.filled_size + 1e-9
                and status_str not in ("canceled", "cancelled", "canceled_market_resolved")
            ):
                if _apply_matched_size(clob_filled):
                    return True
                if await _fak_dust_if_needed():
                    return True
                return False

            if status_str in ("canceled", "cancelled", "canceled_market_resolved"):
                if clob_filled > tracked.filled_size + 1e-9:
                    if _apply_matched_size(clob_filled):
                        return True
                    if await _fak_dust_if_needed():
                        return True
                    return False
                return False

            if clob_filled > tracked.filled_size + 1e-9:
                if _apply_matched_size(clob_filled):
                    return True
                if await _fak_dust_if_needed():
                    return True
                return False

            if attempt + 1 < _REPRICE_POST_CANCEL_FILL_POLLS:
                await asyncio.sleep(_REPRICE_POST_CANCEL_POLL_SEC)

        status_str, clob_filled = await _poll_once()
        if status_str in ("matched", "filled") or clob_filled >= tracked.size - 1e-6:
            tracked.filled_size = min(tracked.size, max(tracked.filled_size, clob_filled))
            tracked.status = OrderStatus.FILLED
            logging.info(
                "✅ [LIVE] Late fill after cancel polls: id=%s filled=%.4f",
                cancelled_order_id[:20],
                tracked.filled_size,
            )
            return True
        if clob_filled > tracked.filled_size + 1e-9:
            if _apply_matched_size(clob_filled):
                return True
            if await _fak_dust_if_needed():
                return True
        return False

    def _place_fak_sell(self, token_id: str, size: float) -> tuple[float, float]:
        """Place a FAK (Fill-And-Kill) market SELL for any share size.

        FAK fills what is available immediately and cancels the unfilled remainder —
        it does not require a minimum order size and bypasses the GTC limit order
        minimum.  Returns (filled_shares, avg_price) or (0.0, 0.0) on failure.

        The worst-price floor is set to 0.01 to ensure the order is marketable
        against any resting bid.  Uses ``create_market_order`` from py_clob_client.
        """
        if self.test_mode or self.client is None:
            logging.info("[SIM FAK SELL] size=%.4f token=%s", size, token_id[:20])
            return (size, 0.50)
        try:
            # Validate size parameter
            is_valid, error_msg = self._validate_order_params(SELL_SIDE, 0.01, size)
            if not is_valid:
                logging.error("FAK SELL validation failed: %s", error_msg)
                return (0.0, 0.0)

            from py_clob_client.clob_types import MarketOrderArgs

            best_bid, _ = self.get_best_prices(token_id)
            _fak_worst_mult = max(0.01, min(1.0, req_float("LIVE_FAK_SELL_WORST_BID_MULT")))
            worst_price = max(0.01, round(best_bid * _fak_worst_mult, 4))
            order_args = MarketOrderArgs(
                token_id=token_id,
                side=SELL_SIDE,
                amount=size,
                price=worst_price,
            )
            order = self.client.create_market_order(order_args)
            resp = self.client.post_order(order, OrderType.FAK)
            status = str(resp.get("status", "") if isinstance(resp, dict) else getattr(resp, "status", "")).lower()
            order_id = str(
                resp.get("orderID") or resp.get("order_id", "")
                if isinstance(resp, dict)
                else getattr(resp, "order_id", "")
            )
            logging.info(
                "[LIVE FAK SELL] size=%.4f worst_px=%.4f → id=%s status=%s token=%s",
                size,
                worst_price,
                order_id[:20] if order_id else "?",
                status,
                token_id[:20],
            )
            if status in ("matched", "filled", "live", "delayed", "unmatched"):
                # FAK may fill partially or fully — poll the actual fill amount.
                if order_id:
                    fill_status, filled = self._get_order_fill(order_id)
                    if filled > 0:
                        fak_tracked = TrackedOrder(
                            order_id=order_id,
                            token_id=token_id,
                            side=SELL_SIDE,
                            price=worst_price,
                            size=size,
                            status=OrderStatus.FILLED,
                            filled_size=filled,
                        )
                        vwap_px = self._sell_fill_avg_price(fak_tracked, filled)
                        if abs(vwap_px - worst_price) > 1e-6:
                            logging.info(
                                "[LIVE FAK SELL] VWAP %.4f vs worst floor %.4f (better fill)",
                                vwap_px,
                                worst_price,
                            )
                        return (filled, vwap_px)
                # matched/filled without id → assume full fill
                return (size, worst_price)
            return (0.0, 0.0)
        except Exception as exc:
            logging.error("[LIVE FAK SELL] failed: %s", exc)
            return (0.0, 0.0)

    def get_open_orders(self, token_id: str | None = None) -> list[dict]:
        """Return open orders from Polymarket CLOB, optionally filtered by token_id.

        Uses the official ``get_orders`` endpoint which requires L2 auth.
        Returns an empty list when unavailable or in test mode.
        """
        if self.test_mode or self.client is None:
            return []
        try:
            from py_clob_client.clob_types import OpenOrderParams

            params = OpenOrderParams(asset_id=token_id) if token_id else None
            resp = self.client.get_orders(params) if params else self.client.get_orders()
            if isinstance(resp, list):
                return resp
            return []
        except Exception as exc:
            logging.warning("get_open_orders failed: %s", exc)
            return []

    async def _place_limit_raw(self, token_id: str, side: str, price: float, size: float) -> tuple[str | None, bool]:
        """Submit a GTC limit order; return (order_id, immediate_fill) or (None, False).

        immediate_fill is True when the CLOB responds with status='matched' meaning
        the order was fully filled synchronously (no need to poll).
        The order_id key in Polymarket CLOB dict responses is 'orderID' (capital D).

        Phase 2 Optimization: Non-blocking order placement using executor to avoid
        blocking the event loop during EIP-712 signing and HTTP request.
        """
        if self.test_mode:
            fake_id = f"sim-{side}-{int(time.time() * 1000)}"
            logging.info(
                "[SIM LIMIT] %s size=%.2f @ %.4f token=%s id=%s",
                side,
                size,
                price,
                token_id,
                fake_id,
            )
            return fake_id, False
        if OrderArgs is None or self.client is None:
            logging.error("Cannot place order: py_clob_client unavailable.")
            return None, False
        try:
            # Validate order parameters
            is_valid, error_msg = self._validate_order_params(side, price, size)
            if not is_valid:
                logging.error("Order validation failed: %s", error_msg)
                return None, False

            # Create order args (fast, CPU-bound)
            order = OrderArgs(token_id=token_id, price=price, size=size, side=side)

            # Non-blocking EIP-712 signing (5-10ms CPU-bound)
            loop = asyncio.get_running_loop()
            signed = await loop.run_in_executor(None, self.client.create_order, order)

            # Non-blocking HTTP request (620ms network-bound from Portugal, ~20ms from Ireland)
            # Wrapped in Circuit Breaker
            send_ts = time.time()
            try:
                resp = await self.circuit_breaker.call(
                    lambda: loop.run_in_executor(None, self.client.post_order, signed, OrderType.GTC)
                )
            except CircuitBreakerError:
                logging.error("[CIRCUIT] Order placement aborted: Circuit is OPEN")
                return None, False

            if isinstance(resp, dict):
                order_id = str(resp.get("orderID") or resp.get("order_id") or "")
                immediate = str(resp.get("status", "")).lower() in ("matched", "filled")
            else:
                order_id = str(getattr(resp, "order_id", "") or "")
                immediate = str(getattr(resp, "status", "")).lower() in ("matched", "filled")
            if not order_id:
                logging.error(
                    "Order placement: no order_id in response %s @ %.4f resp=%s",
                    side,
                    price,
                    resp,
                )
                return None, False
            logging.info(
                "[LIVE] %s size=%.2f @ %.4f token=%s -> id=%s immediate_fill=%s",
                side,
                size,
                price,
                token_id[:20],
                order_id[:20],
                immediate,
            )
            return order_id, immediate
        except Exception as exc:
            logging.error("Order placement failed %s @ %.4f: %s", side, price, exc)
            return None, False

    async def _fak_sell(self, token_id: str, size: float) -> float:
        """Execute a Fill-And-Kill (FAK) market SELL order.

        Args:
            token_id (str): The outcome token ID to sell.
            size (float): Number of shares to sell.

        Returns:
            float: Total filled shares from the FAK order.
        """
        filled, price = await asyncio.to_thread(self._place_fak_sell, token_id, size)
        if filled > 0:
            logging.info(
                "🔴 [LIVE] FAK SELL done: filled=%.4f / %.4f @ %.4f token=%s",
                filled,
                size,
                price,
                token_id[:20],
            )
        else:
            logging.error(
                "🛑 FAK SELL failed: %.4f shares token=%s — manual intervention required.",
                size,
                token_id[:20],
            )
        return filled

    async def _poll_order(self, tracked: TrackedOrder) -> None:
        """Monitor fill status using FSM and event queue.

        Production: fills arrive via WebSocket / user-order cache callbacks into
        ``_event_queue``. Tests: ``test_mode`` drives the FSM by polling
        ``_get_order_fill`` (patchable) and enqueueing ``WsOrderEvent``.

        Partial fills accumulate across reprice cycles; after cancel-before-reprice,
        ``_recover_fill_after_cancel`` polls the old order id so fills that race
        with cancel are not mistaken for failed sells.
        """
        fsm = OrderFSM(tracked, self)
        self._fsms[tracked.order_id] = fsm

        await self.initialize()

        timer_task = asyncio.create_task(self._order_timer(tracked.order_id))
        driver_task: asyncio.Task | None = None
        if self.test_mode:
            driver_task = asyncio.create_task(
                self._test_mode_feed_order_fill_events(tracked.order_id),
            )

        try:
            await fsm.wait()
        finally:
            timer_task.cancel()
            if driver_task is not None:
                driver_task.cancel()
                try:
                    await driver_task
                except asyncio.CancelledError:
                    pass
            try:
                await timer_task
            except asyncio.CancelledError:
                pass
            self._fsms.pop(tracked.order_id, None)
            self._active_orders.pop(tracked.order_id, None)

    async def _test_mode_feed_order_fill_events(self, order_id: str) -> None:
        """Poll ``_get_order_fill`` and enqueue WsOrderEvents for the FSM (test_mode only).

        De-duplicates consecutive identical (status, filled) pairs so the event worker
        is not starved by a tight poll interval — ``TimerEvent`` must be able to run
        for stale / reprice paths in tests.
        """
        poll = float(_ORDER_FILL_POLL_SEC)
        last_key: tuple[str, float] | None = None
        try:
            while order_id in self._fsms:
                status, filled = await asyncio.to_thread(self._get_order_fill, order_id)
                if status not in ("unknown", "live") or filled > 0:
                    key = (status, filled)
                    if key != last_key:
                        self._event_queue.put_nowait(
                            WsOrderEvent(order_id=order_id, status=status, filled=filled),
                        )
                        last_key = key
                await asyncio.sleep(poll)
        except asyncio.CancelledError:
            raise

    async def _order_timer(self, order_id: str) -> None:
        """Periodic timer for a specific order to trigger stale checks."""
        while True:
            await asyncio.sleep(1.0)
            self._event_queue.put_nowait(TimerEvent())

    async def _emergency_exit_order(self, tracked: TrackedOrder) -> None:
        """Exit remaining size aggressively after reprice attempts exhausted.

        For SELL orders: uses FAK market order which works for any size including
        sub-minimum.  For BUY orders: crosses the spread with a GTC limit.
        Updates ``tracked.filled_size`` with any additional fills.
        """
        self._entry_stats["emergency_exits"] += 1
        poly_min = req_float("POLY_CLOB_MIN_SHARES")
        remaining = tracked.remaining if tracked.status in (OrderStatus.PARTIAL, OrderStatus.STALE) else tracked.size
        if remaining <= 0:
            return

        logging.warning(
            "🚨 EMERGENCY EXIT: %s %.2f token=%s (min=%.0f filled=%.2f)",
            tracked.side,
            remaining,
            tracked.token_id[:20],
            poly_min,
            tracked.filled_size,
        )

        # Freshness check before emergency exit
        if self.stale_block_actions and not is_fresh_for_trading(
            tracked.token_id, self._market_book_cache, self._user_order_cache
        ):
            logging.warning(
                "[STALE_ORDERBOOK] [EMERGENCY] Proceeding with emergency exit for %s despite stale data (safety first)",
                tracked.token_id[:8],
            )

        # Diagnostic: track intended vs actual exit price for slippage analysis
        intended_price = tracked.price if tracked.price > 0 else 0.0

        if tracked.side == SELL_SIDE:
            # FAK handles any size including sub-minimum — preferred for all SELL exits.
            fak_filled = await self._fak_sell(tracked.token_id, remaining)
            tracked.filled_size += fak_filled

            # Log emergency exit slippage
            if fak_filled > 0:
                actual_avg_price = tracked.filled_size / tracked.size if tracked.size > 0 else 0.0
                slippage_pct = (
                    ((actual_avg_price - intended_price) / intended_price * 100) if intended_price > 0 else 0.0
                )
                logging.warning(
                    "EMERGENCY_EXIT_SLIPPAGE: side=%s intended_price=%.4f actual_price=%.4f "
                    "slippage_pct=%.2f%% filled=%.4f/%.4f",
                    tracked.side,
                    intended_price,
                    actual_avg_price,
                    slippage_pct,
                    tracked.filled_size,
                    tracked.size,
                )
        else:
            best_bid, best_ask = await asyncio.to_thread(self.get_best_prices, tracked.token_id)
            _eb = live_emergency_buy_bump()
            price = max(0.01, min(0.99, best_ask + _eb))
            em_size = self._affordable_buy_shares(price, remaining)
            if em_size <= 0.0 or em_size < poly_min:
                logging.error(
                    "Emergency BUY: cannot afford %.4f sh at %.4f (got %.4f, min=%.0f).",
                    remaining,
                    price,
                    em_size,
                    poly_min,
                )
                self._last_buy_skip_reason = "emergency_buy_failed"
                return
            if em_size < remaining - 1e-6:
                logging.warning(
                    "Emergency BUY: size %.4f → %.4f to fit USDC at %.4f.",
                    remaining,
                    em_size,
                    price,
                )
            # CLOB compares order cost to balance in micro-USDC; leave headroom so
            # rounding does not trigger "not enough balance" when spend ≈ wallet.
            _marg = float(os.getenv("LIVE_EMERGENCY_BUY_BALANCE_MARGIN", "0.002"))
            if not self.test_mode and price > 0.0:
                _bal = self.fetch_usdc_balance()
                if _bal is not None and _bal > 0.0:
                    _cap_sh = (_bal * max(0.0, 1.0 - _marg)) / price
                    _cap_sh = float(int(_cap_sh * 100.0) / 100.0)
                    if _cap_sh + 1e-9 < em_size:
                        logging.info(
                            "[LIVE] Emergency BUY cap: %.4f → %.4f sh (balance margin %.3f%%).",
                            em_size,
                            _cap_sh,
                            _marg * 100.0,
                        )
                        em_size = _cap_sh
            if em_size < poly_min:
                logging.error(
                    "Emergency BUY: after margin cap size %.4f < min %.0f at %.4f.",
                    em_size,
                    poly_min,
                    price,
                )
                self._last_buy_skip_reason = "emergency_buy_failed"
                return
            order_id, immediate = await self._place_limit_raw(tracked.token_id, tracked.side, price, em_size)
            if order_id:
                emergency = TrackedOrder(
                    order_id=order_id,
                    token_id=tracked.token_id,
                    side=tracked.side,
                    price=price,
                    size=em_size,
                    status=OrderStatus.FILLED if immediate else OrderStatus.PENDING,
                    filled_size=em_size if immediate else 0.0,
                )
                self._active_orders[order_id] = emergency
                if not immediate:
                    await self._poll_order(emergency)
                _add = float(emergency.filled_size)
                _prev_f = float(tracked.filled_size)
                _em_px = float(emergency.price)
                tracked.filled_size += _add
                if _add > 0.0 and tracked.filled_size > 0.0:
                    if _prev_f <= 0.0:
                        tracked.price = _em_px
                    else:
                        tracked.price = (_prev_f * float(tracked.price) + _add * _em_px) / tracked.filled_size
            else:
                logging.error(
                    "🛑 Emergency BUY placement FAILED token=%s remaining=%.2f" " — manual intervention required.",
                    tracked.token_id,
                    remaining,
                )
                self._last_buy_skip_reason = "emergency_buy_failed"

    async def emergency_exit(self, token_id: str, size: float, side: str = SELL_SIDE) -> None:
        """Execute an emergency exit by cancelling orders and crossing the book.

        Args:
            token_id (str): The outcome token ID to exit.
            size (float): Number of shares to exit.
            side (str): The side of the exit order (default: SELL_SIDE).
        """
        pending = [o for o in list(self._active_orders.values()) if o.token_id == token_id]
        for order in pending:
            self._cancel_order(order.order_id)
            order.status = OrderStatus.CANCELLED
            self._active_orders.pop(order.order_id, None)

        if size <= 0:
            return

        # Freshness check before aggressive exit
        if self.stale_block_actions and not is_fresh_for_trading(
            token_id, self._market_book_cache, self._user_order_cache
        ):
            logging.warning(
                "[STALE_ORDERBOOK] [EMERGENCY_EXIT] Proceeding with aggressive exit for %s despite stale data",
                token_id[:8],
            )

        best_bid, best_ask = await asyncio.to_thread(self.get_best_prices, token_id)
        _xb = live_emergency_cross_bump()
        if side == SELL_SIDE:
            price = max(0.01, min(0.99, best_bid - _xb))
        else:
            price = max(0.01, min(0.99, best_ask + _xb))

        place_sz = size
        if side == BUY:
            place_sz = self._affordable_buy_shares(price, size)
            if place_sz <= 0.0:
                logging.error(
                    "🛑 EMERGENCY CLOSE BUY: zero affordable size @ %.4f token=%s.",
                    price,
                    token_id[:20],
                )
                return
            if place_sz < size - 1e-6:
                logging.warning(
                    "EMERGENCY CLOSE BUY: size %.2f → %.2f (USDC cap) @ %.4f.",
                    size,
                    place_sz,
                    price,
                )

        poly_min = req_float("POLY_CLOB_MIN_SHARES")
        if side == SELL_SIDE and 0.0 < place_sz < poly_min:
            logging.warning(
                "⚠️ EMERGENCY SELL size=%.4f < CLOB min %.0f — FAK market sell.",
                place_sz,
                poly_min,
            )
            filled = await self._fak_sell(token_id, place_sz)
            if filled <= 0.0:
                logging.error(
                    "🛑 Emergency FAK SELL failed token=%s size=%.4f.",
                    token_id[:20],
                    place_sz,
                )
            return

        logging.warning(
            "🚨 EMERGENCY CLOSE: %s %.4f @ %.4f token=%s",
            side,
            place_sz,
            price,
            token_id[:20],
        )
        self._entry_stats["emergency_exits"] += 1
        # Validate order parameters before placing emergency order
        is_valid, error_msg = self._validate_order_params(side, price, place_sz)
        if not is_valid:
            logging.error("Emergency order validation failed: %s", error_msg)
            return
        order_id, immediate = await self._place_limit_raw(token_id, side, price, place_sz)
        if order_id:
            tracked = TrackedOrder(
                order_id=order_id,
                token_id=token_id,
                side=side,
                price=price,
                size=place_sz,
                status=OrderStatus.FILLED if immediate else OrderStatus.PENDING,
                filled_size=place_sz if immediate else 0.0,
            )
            self._active_orders[order_id] = tracked
            if not immediate:
                asyncio.ensure_future(self._poll_order(tracked))
        else:
            logging.error("🛑 Emergency close FAILED token=%s — manual intervention required.", token_id)

    def _purge_buy_orders_for_token(self, token_id: str) -> None:
        """Drop all in-memory BUY trackers and confirmed keys for this outcome token.

        ``_emergency_exit_order`` can place a second BUY with a new ``order_id`` while
        ``execute`` still references the first ``TrackedOrder``.  If ``execute``
        returns SKIP (strict chain / USDC verify), ``pop(tracked.order_id)`` alone
        leaves the emergency FILLED row in ``_active_orders``, so
        ``filled_buy_shares`` keeps summing ghost size (e.g. 10.63) and OPEN is blocked.
        """
        for oid, o in list(self._active_orders.items()):
            if o.token_id == token_id and o.side == BUY:
                self._active_orders.pop(oid, None)
        self._confirmed_buys.pop(token_id, None)

    def filled_buy_shares(self, token_id: str) -> float:
        """Return total filled BUY shares currently tracked for token_id.

        When the BUY is no longer pending, the value from ``_confirmed_buys`` (the
        same fill passed to ``live_open`` after ``execute``) is authoritative so
        CLOSE never uses ``order.size`` instead of the CLOB-reported fill.
        While a BUY is still PENDING or PARTIAL, sums fills from active orders.
        """
        if self.has_pending_buy(token_id):
            total = 0.0
            for order in self._active_orders.values():
                if order.token_id != token_id or order.side != BUY:
                    continue
                if order.status == OrderStatus.FILLED:
                    total += order.filled_size if order.filled_size > 0 else order.size
                elif order.status in (OrderStatus.PENDING, OrderStatus.PARTIAL):
                    total += order.filled_size
            return total
        if token_id in self._confirmed_buys:
            return float(self._confirmed_buys[token_id])
        total = 0.0
        for order in self._active_orders.values():
            if order.token_id != token_id or order.side != BUY:
                continue
            if order.status == OrderStatus.FILLED:
                total += order.filled_size if order.filled_size > 0 else order.size
            elif order.status in (OrderStatus.PENDING, OrderStatus.PARTIAL):
                total += order.filled_size
        return total

    def clear_filled_buy(self, token_id: str) -> None:
        """Remove the confirmed-buy entry for token_id after a SELL completes."""
        self._confirmed_buys.pop(token_id, None)

    def sync_confirmed_fill(self, token_id: str, shares: float) -> None:
        """Set confirmed BUY shares after bot-side reconcile (chain vs PnL desync)."""
        if shares <= 1e-12:
            self._confirmed_buys.pop(token_id, None)
            return
        self._confirmed_buys[token_id] = float(shares)

    def has_pending_buy(self, token_id: str) -> bool:
        """Return True when there is at least one non-terminal BUY order for token_id.

        Used to detect the race condition where SIM triggers CLOSE before the live
        BUY order has been confirmed filled by the CLOB poll loop.
        """
        return any(
            o.token_id == token_id and o.side == BUY and o.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)
            for o in self._active_orders.values()
        )

    def has_pending_sell(self, token_id: str) -> bool:
        """Return True when a non-terminal SELL order is still tracked for token_id."""
        return any(
            o.token_id == token_id and o.side == SELL_SIDE and o.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)
            for o in self._active_orders.values()
        )

    async def wait_for_buy_fill(self, token_id: str, timeout_sec: float = 5.0) -> float:
        """Wait until all pending BUY orders for token_id reach a terminal state.

        Returns the total filled shares once all BUY orders settle.  If the orders
        do not fill within timeout_sec the method returns whatever filled_size has
        been confirmed so far (may be 0 if nothing filled).
        """
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            pending = [o for o in self._active_orders.values() if o.token_id == token_id and o.side == BUY]
            if not pending:
                break

            # Wait for any of the pending FSMs to finish or timeout
            fsms = [self._fsms[o.order_id] for o in pending if o.order_id in self._fsms]
            if fsms:
                try:
                    await asyncio.wait_for(
                        asyncio.gather(*(f.wait() for f in fsms)), timeout=max(0.1, deadline - time.monotonic())
                    )
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
            else:
                await asyncio.sleep(0.1)

        filled = self.filled_buy_shares(token_id)
        logging.info(
            "[LIVE] wait_for_buy_fill done: token=%s filled=%.4f shares",
            token_id[:20],
            filled,
        )
        return filled

    async def wait_for_exit_readiness(self, token_id: str, timeout_sec: float | None = None) -> None:
        """Wait until in-memory BUY/SELL trackers finish and CLOB has no open orders for token.

        After a partial SELL or ledger lag, fills can still be settling while this
        method drains pending state so ``filled_buy_shares`` and chain probes see truth.
        """
        if self.test_mode:
            return
        if timeout_sec is None:
            timeout_sec = req_float("LIVE_CLOSE_WAIT_PENDING_SEC")
        deadline = time.monotonic() + max(0.1, timeout_sec)
        while time.monotonic() < deadline:
            pending = [o for o in self._active_orders.values() if o.token_id == token_id]
            if pending:
                fsms = [self._fsms[o.order_id] for o in pending if o.order_id in self._fsms]
                if fsms:
                    try:
                        await asyncio.wait_for(
                            asyncio.gather(*(f.wait() for f in fsms)), timeout=max(0.1, deadline - time.monotonic())
                        )
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                else:
                    await asyncio.sleep(0.1)
                continue

            open_list = await asyncio.to_thread(self.get_open_orders, token_id)
            if open_list:
                await asyncio.sleep(0.5)  # Longer sleep for HTTP poll
                continue
            return
        logging.warning(
            "[LIVE] wait_for_exit_readiness: timeout %.1fs token=%s (pending/open may remain).",
            timeout_sec,
            token_id[:20],
        )

    async def probe_chain_shares_for_close(self, token_id: str) -> float:
        """Poll conditional balance when tracked fills are zero; sync ``_confirmed_buys`` if found.

        CLOB-reported fills can land on-chain after the strategy already cleared
        in-memory state.  Uses the same staggered delay pattern as post-BUY balance
        checks.  Returns shares to sell (may be below POLY_CLOB_MIN for FAK exit).
        """
        if self.test_mode:
            return 0.0
        _dust_raw = os.getenv("LIVE_CHAIN_EXIT_DUST_SHARES") or os.getenv("LIVE_SELL_CHAIN_DUST_SHARES")
        dust = (
            float(_dust_raw.strip())
            if _dust_raw and str(_dust_raw).strip()
            else req_float("LIVE_SELL_CHAIN_DUST_SHARES")
        )
        delays = _parse_csv_floats(req_str("LIVE_CLOSE_CHAIN_PROBE_DELAYS_SEC"))
        best: float | None = None
        for d in delays:
            if d > 0:
                await asyncio.sleep(d)
            bal = await asyncio.to_thread(self.fetch_conditional_balance, token_id)
            if bal is not None and bal > dust:
                best = bal
                break
        if best is None or best <= dust:
            return 0.0
        self._confirmed_buys[token_id] = best
        logging.info(
            "[REST_RECONCILE] [LIVE] probe_chain_shares_for_close: token=%s -> %.4f sh (synced _confirmed_buys).",
            token_id[:20],
            best,
        )
        return float(best)

    async def _await_sellable_balance(self, token_id: str, requested: float) -> float | None:
        """Refresh allowance and poll until conditional balance reflects a BUY fill or timeout.

        After a CLOB match, ``get_balance_allowance`` can lag; ``post_order`` SELL then
        fails with balance 0.  This mirrors the post-BUY balance loop with allowance
        refresh each step.
        """
        if self.test_mode:
            return None
        _dust_raw = os.getenv("LIVE_CHAIN_EXIT_DUST_SHARES") or os.getenv("LIVE_SELL_CHAIN_DUST_SHARES")
        dust = (
            float(_dust_raw.strip())
            if _dust_raw and str(_dust_raw).strip()
            else req_float("LIVE_SELL_CHAIN_DUST_SHARES")
        )
        delays = _parse_csv_floats(req_str("LIVE_SELL_BALANCE_WAIT_DELAYS_SEC"))
        for d in delays:
            if d > 0:
                await asyncio.sleep(d)
            await self._ensure_allowance_cached(token_id)
            bal = await asyncio.to_thread(self.fetch_conditional_balance, token_id)
            if bal is not None and bal > dust:
                return min(requested, bal)
        return None

    def _log_entry_stats_if_due(self) -> None:
        """Emit aggregated live entry stats periodically for gate diagnostics.

        Phase 2 WebSocket Migration: Includes WS/HTTP metrics.
        """
        if self.skip_stats_log_sec <= 0:
            return
        now = time.time()
        if now - self._last_skip_stats_log_ts < self.skip_stats_log_sec:
            return

        # Log WS metrics periodically
        if now - self._ws_metrics_last_log >= self._ws_metrics_log_interval:
            self._log_ws_metrics("periodic")

        st = self._entry_stats
        logging.info(
            "Live entry stats: attempts=%s executed=%s skip_ask_cap=%s "
            "skip_spread=%s skip_signal=%s reprice=%s emergency=%s active_orders=%s "
            "ws_events=%d http_fallbacks=%d ws_latency_avg=%.2fms.",
            st["attempts"],
            st["executed"],
            st["skip_ask_cap"],
            st["skip_spread"],
            st["skip_signal"],
            st["reprice_total"],
            st["emergency_exits"],
            len(self._active_orders),
            self._ws_metrics["ws_events_received"],
            self._http_metrics["http_fallbacks_total"],
            self._get_ws_metrics()["ws_latency_avg_ms"],
        )
        self._last_skip_stats_log_ts = now

    async def shutdown(self) -> None:
        """Shutdown execution engine and log final metrics."""
        if self._worker_task:
            self._event_queue.put_nowait(None)
            try:
                await asyncio.wait_for(self._worker_task, timeout=2.0)
            except asyncio.TimeoutError:
                self._worker_task.cancel()
            self._worker_task = None

        self._log_ws_metrics("shutdown")
        logging.info(
            "[LIVE] Final metrics: ws_events=%d http_fallbacks=%d " "ws_latency_avg=%.2fms active_orders=%d",
            self._ws_metrics["ws_events_received"],
            self._http_metrics["http_fallbacks_total"],
            self._get_ws_metrics()["ws_latency_avg_ms"],
            len(self._active_orders),
        )

    async def _maybe_warn_or_fak_chain_remainder(
        self,
        token_id: str,
        requested_size: float,
        total_filled: float,
        avg_price: float,
    ) -> tuple[float, float]:
        """Compare on-chain balance to CLOB fill; warn or optionally FAK the gap."""
        delay = req_float("LIVE_POST_SELL_CHAIN_DELAY_SEC")
        if delay > 0:
            await asyncio.sleep(delay)
        bal = await asyncio.to_thread(self.fetch_conditional_balance, token_id)
        dust = req_float("LIVE_SELL_CHAIN_DUST_SHARES")
        gap = max(0.0, requested_size - total_filled)
        if bal is None or bal <= dust:
            return (total_filled, avg_price)
        if gap <= dust and bal < 0.08:
            return (total_filled, avg_price)
        if gap > dust and os.getenv("LIVE_CHAIN_SELL_REMAINDER", "0") == "1":
            fak_sz = min(bal, gap)
            fak_filled, fak_px = await asyncio.to_thread(self._place_fak_sell, token_id, fak_sz)
            if fak_filled > 0:
                denom = total_filled + fak_filled
                new_avg = (total_filled * avg_price + fak_filled * fak_px) / denom
                logging.info(
                    "🔴 [LIVE] Chain gap FAK: +%.4f @ %.4f (total %.4f) token=%s",
                    fak_filled,
                    fak_px,
                    denom,
                    token_id[:20],
                )
                return (denom, new_avg)
        logging.warning(
            "⚠️ [LIVE] On-chain balance %.4f shares after SELL (filled %.4f / req %.4f). "
            "Set LIVE_CHAIN_SELL_REMAINDER=1 to auto-FAK the gap.",
            bal,
            total_filled,
            requested_size,
        )
        return (total_filled, avg_price)

    async def close_position(self, token_id: str, size: float, signal_ts: float = 0.0) -> tuple[float, float]:
        """Sell all ``size`` shares and return (total_filled_shares, avg_price).

        When ``_confirmed_buys`` holds this token, ``size`` is replaced by that
        confirmed BUY fill so the SELL matches the same quantity recorded at open.

        - size >= POLY_CLOB_MIN_SHARES: GTC limit just above bid; the fill is always
          verified via ``_poll_order`` (even when post_order reports immediate match)
          so ``size_matched`` from the CLOB is trusted instead of assuming full size.
        - size < POLY_CLOB_MIN_SHARES: FAK market SELL immediately — CLOB rejects
          GTC below the minimum.
        - GTC placement failure: FAK fallback then emergency_exit.
        - After a successful fill, optionally compares on-chain balance to the
          request (see ``LIVE_CHAIN_SELL_REMAINDER`` and ``_maybe_warn_or_fak_chain_remainder``).
        - Set ``LIVE_SKIP_PRESELL_BALANCE=1`` to skip the pre-SELL balance query
          when CLOB fill tracking is trusted (lower latency).
        - When pre-SELL balance is fetched, best bid is loaded in parallel with
          that call to save one sequential HTTP round-trip before GTC placement.
        - If pre-SELL balance reads 0 but CLOB filled a BUY, ``_await_sellable_balance``
          polls with allowance refresh before SELL; GTC/FAK placement retries use
          ``LIVE_SELL_PLACE_ATTEMPTS``, ``LIVE_SELL_BALANCE_WAIT_DELAYS_SEC``.

        Blocks until a terminal state and returns (0.0, 0.0) on total failure.
        """
        if size <= 0:
            return (0.0, 0.0)

        # Circuit Breaker check
        from utils.resilience import CircuitState

        if self.circuit_breaker.state == CircuitState.OPEN:
            logging.warning("[CIRCUIT] Blocking close_position: Circuit is OPEN")
            # We still return (0.0, 0.0) but this is more critical as it's an exit
            return (0.0, 0.0)

        # Freshness check before placing SELL
        if self.stale_block_actions and not is_fresh_for_trading(
            token_id, self._market_book_cache, self._user_order_cache
        ):
            logging.warning("[STALE_ORDERBOOK] [SELL] Blocking close_position for %s: data not fresh", token_id[:8])
            return (0.0, 0.0)

        if token_id in self._confirmed_buys:
            cb = float(self._confirmed_buys[token_id])
            if cb > 0.0 and abs(size - cb) > 1e-6:
                logging.info(
                    "[LIVE] close_position: SELL size set to confirmed BUY fill=%.4f " "(caller passed %.4f) token=%s",
                    cb,
                    size,
                    token_id[:20],
                )
            if cb > 0.0:
                size = cb

        poly_min = req_float("POLY_CLOB_MIN_SHARES")
        skip_presell = os.getenv("LIVE_SKIP_PRESELL_BALANCE", "0") == "1"

        # Verify actual on-chain CTF balance before placing any SELL.  Polymarket
        # deducts a protocol fee in shares at fill time, so the wallet balance may
        # be slightly less than the CLOB-reported filled_size.  Selling more than
        # held always fails with "not enough balance / allowance".
        # If the balance API returns 0 (ledger lag) we keep the original size and
        # let the CLOB reject minimally — retry is handled by _poll_order reprice.
        # Set LIVE_SKIP_PRESELL_BALANCE=1 to skip this round-trip when fill size is
        # trusted from CLOB polling (faster exit).
        actual_bal: float | None = None
        bb_pre: float | None = None
        if not skip_presell:
            actual_bal, (bb_pre, _) = await asyncio.gather(
                asyncio.to_thread(self.fetch_conditional_balance, token_id),
                asyncio.to_thread(self.get_best_prices, token_id),
            )
        if actual_bal is not None and actual_bal > 0 and actual_bal < size:
            if actual_bal < poly_min and size >= poly_min:
                logging.warning(
                    "⚠️ [LIVE] On-chain %.4f < CLOB min while selling %.4f "
                    "(likely lag or prior-window dust — keeping requested size) token=%s",
                    actual_bal,
                    size,
                    token_id[:20],
                )
            else:
                logging.warning(
                    "⚠️ [LIVE] SELL size corrected: %.4f → %.4f " "(on-chain balance after fee) token=%s",
                    size,
                    actual_bal,
                    token_id[:20],
                )
                size = actual_bal
        elif actual_bal is not None and actual_bal == 0:
            logging.warning(
                "⚠️ [LIVE] close_position: on-chain balance=0 (possible lag) — " "keeping requested size=%.4f token=%s",
                size,
                token_id[:20],
            )
            if not self.test_mode:
                await asyncio.to_thread(self.ensure_conditional_allowance, token_id)
                _wait_bal = await self._await_sellable_balance(token_id, size)
                if _wait_bal is not None and _wait_bal > 0:
                    size = min(size, _wait_bal)
                    logging.info(
                        "[LIVE] close_position: balance appeared after wait → %.4f sh token=%s",
                        size,
                        token_id[:20],
                    )
        if size <= 0:
            logging.error(
                "🛑 [LIVE] close_position: size is 0 for token=%s — nothing to sell.",
                token_id[:20],
            )
            return (0.0, 0.0)

        if size < poly_min:
            logging.warning(
                "⚠️ [LIVE] close_position: size %.2f < min %.0f — FAK market sell.",
                size,
                poly_min,
            )
            filled, price = await asyncio.to_thread(self._place_fak_sell, token_id, size)
            if filled > 0:
                logging.info(
                    "🔴 [LIVE] FAK SELL done: %.4f @ %.4f token=%s",
                    filled,
                    price,
                    token_id[:20],
                )
                return (filled, price)
            logging.error("🛑 [LIVE] FAK SELL failed: size=%.2f token=%s.", size, token_id[:20])
            return (0.0, 0.0)

        if bb_pre is not None:
            best_bid = bb_pre
        else:
            best_bid, _ = await asyncio.to_thread(self.get_best_prices, token_id)
        # GTC SELL: limit at best_bid + offset (negative offset = below bid, more marketable).
        # Previous best_bid+0.002 sat above top bid and could rest unfilled vs paper's bid exit.
        _sell_off = req_float("LIVE_SELL_GTC_OFFSET_FROM_BID")
        price = max(0.01, min(0.99, best_bid + _sell_off))
        sell_attempts = max(1, req_int("LIVE_SELL_PLACE_ATTEMPTS"))
        order_id: str | None = None
        immediate = False
        for _att in range(sell_attempts):
            await self._ensure_allowance_cached(token_id)
            order_id, immediate = await self._place_limit_raw(token_id, SELL_SIDE, price, size)
            if order_id:
                break
            if _att + 1 < sell_attempts:
                logging.warning(
                    "[LIVE] SELL GTC placement failed — retry %d/%d (balance/allowance lag) token=%s.",
                    _att + 1,
                    sell_attempts,
                    token_id[:20],
                )
                await self._await_sellable_balance(token_id, size)
                await asyncio.sleep(req_float("LIVE_SELL_PLACE_RETRY_SLEEP_SEC"))
        if not order_id:
            logging.warning("⚠️ [LIVE] SELL GTC failed, trying FAK token=%s.", token_id[:20])
            fak_attempts = max(1, req_int("LIVE_SELL_FAK_ATTEMPTS"))
            filled, fak_price = 0.0, 0.0
            for _fa in range(fak_attempts):
                await self._ensure_allowance_cached(token_id)
                if _fa > 0:
                    await self._await_sellable_balance(token_id, size)
                    await asyncio.sleep(req_float("LIVE_SELL_FAK_RETRY_SLEEP_SEC"))
                filled, fak_price = await asyncio.to_thread(self._place_fak_sell, token_id, size)
                if filled > 0:
                    logging.info(
                        "🔴 [LIVE] FAK SELL done (GTC fallback): %.4f @ %.4f token=%s",
                        filled,
                        fak_price,
                        token_id[:20],
                    )
                    return (filled, fak_price)
                if _fa + 1 < fak_attempts:
                    logging.warning(
                        "[LIVE] FAK SELL failed — retry %d/%d token=%s.",
                        _fa + 1,
                        fak_attempts,
                        token_id[:20],
                    )
            await self.emergency_exit(token_id, size, side=SELL_SIDE)
            return (0.0, 0.0)

        tracked = TrackedOrder(
            order_id=order_id,
            token_id=token_id,
            side=SELL_SIDE,
            price=price,
            size=size,
            status=OrderStatus.PENDING,
            filled_size=0.0,
            signal_ts=signal_ts,
            send_ts=send_ts if 'send_ts' in locals() else time.time(),
        )
        self._active_orders[order_id] = tracked
        logging.info(
            "🔴 [LIVE] SELL placed: %.4f @ %.4f id=%s immediate=%s token=%s — polling fill",
            size,
            price,
            order_id[:20],
            immediate,
            token_id[:20],
        )
        await self._poll_order(tracked)

        total_filled = (
            tracked.filled_size
            if tracked.filled_size > 0
            else (tracked.size if tracked.status == OrderStatus.FILLED else 0.0)
        )
        avg_price = self._sell_fill_avg_price(tracked, total_filled)
        if total_filled > 0 and not self.test_mode:
            total_filled, avg_price = await self._maybe_warn_or_fak_chain_remainder(
                token_id, size, total_filled, avg_price
            )
        if total_filled > 0:
            logging.info(
                "🔴 [LIVE] SELL confirmed: filled=%.4f / %.4f @ %.4f token=%s",
                total_filled,
                size,
                avg_price,
                token_id[:20],
            )
        else:
            logging.error(
                "🛑 [LIVE] SELL not filled: size=%.4f @ %.4f token=%s",
                size,
                avg_price,
                token_id[:20],
            )
        return (total_filled, avg_price)

    async def execute(
        self,
        signal: str,
        token_id: str,
        order_size: float | None = None,
        budget_usd: float | None = None,
        *,
        best_bid: float | None = None,
        best_ask: float | None = None,
        signal_ts: float = 0.0,
    ) -> tuple[float, float]:
        """Place a limit BUY, wait for CLOB confirmation, return (filled_shares, avg_price).

        Blocks until the order reaches a terminal state (filled, stale after reprice,
        or failed).  Returns (0.0, 0.0) on any skip or failure so callers can treat a
        non-positive filled_shares as a no-op without rollback gymnastics.

        Size resolution priority:
          1. ``order_size`` — treated as USD notional, converted to shares at best_ask.
          2. ``budget_usd`` — same as order_size.
          3. ``min_order_size`` (LIVE_ORDER_SIZE env) as USD notional fallback.

        The resulting shares are clamped to the Polymarket CLOB minimum
        (POLY_CLOB_MIN_SHARES, default 5).  An insufficient budget causes a
        logged skip rather than an invalid order.

        Optional ``best_bid`` and ``best_ask`` skip a CLOB book HTTP round-trip when
        the caller already has a fresh top-of-book (e.g. from the strategy tick).

        After a reported fill, ``fetch_conditional_balance`` must confirm at least
        ``LIVE_BALANCE_MIN_FRAC`` of the fill before returning success; otherwise the
        run returns (0,0) unless ``LIVE_TRUST_CLOB_WITHOUT_CHAIN_BALANCE`` is unset or
        non-zero (default: trust CLOB). Set ``LIVE_TRUST_CLOB_WITHOUT_CHAIN_BALANCE=0``
        for strict mode (no position until chain confirms).
        """
        _SKIP = (0.0, 0.0)
        self._last_buy_skip_reason = None
        self._entry_stats["attempts"] += 1

        # Circuit Breaker check
        from utils.resilience import CircuitState

        if self.circuit_breaker.state == CircuitState.OPEN:
            logging.warning("[CIRCUIT] Blocking execute: Circuit is OPEN")
            return _SKIP

        # Freshness check before placing BUY
        if self.stale_block_actions and not is_fresh_for_trading(
            token_id, self._market_book_cache, self._user_order_cache
        ):
            logging.warning("[STALE_ORDERBOOK] [BUY] Blocking execute for %s: data not fresh", token_id[:8])
            return _SKIP

        # Anti-doubling safety gate: prevent entering if already have position or pending order
        if not self.can_enter_position(token_id, signal):
            self._entry_stats["skip_signal"] += 1
            self._log_entry_stats_if_due()
            return _SKIP

        if best_bid is not None and best_ask is not None and best_ask > 0.0 and best_bid >= 0.0:
            best_bid, best_ask = float(best_bid), float(best_ask)
        else:
            best_bid, best_ask = await asyncio.to_thread(self.get_best_prices, token_id)

        if not _paper_aligned_buy_price_allows(signal, best_ask, self.max_entry_ask):
            self._entry_stats["skip_ask_cap"] += 1
            logging.warning(
                "Skip %s: best_ask %.4f outside paper-aligned gates (global max HFT_MAX_ENTRY_ASK=%.4f "
                "+ per-outcome HFT_ENTRY_*_ASK_UP/DOWN).",
                signal,
                best_ask,
                self.max_entry_ask,
            )
            self._log_entry_stats_if_due()
            return _SKIP

        spread = best_ask - best_bid
        if spread <= 0 or spread > self.max_spread:
            self._entry_stats["skip_spread"] += 1
            logging.warning(
                "[SPREAD_TOO_WIDE] ⚠️ Bad spread %.4f (bid=%.4f ask=%.4f max=%.4f), skip signal %s.",
                spread,
                best_bid,
                best_ask,
                self.max_spread,
                signal,
            )
            self._log_entry_stats_if_due()
            return _SKIP

        if signal not in ("BUY_UP", "BUY_DOWN"):
            self._entry_stats["skip_signal"] += 1
            logging.warning("Skip signal: unsupported live signal %s.", signal)
            self._log_entry_stats_if_due()
            return _SKIP

        poly_min_shares = req_float("POLY_CLOB_MIN_SHARES")

        exec_price = max(0.001, best_ask)
        usd_notional = order_size or budget_usd or self.min_order_size
        _max_pos_usd = req_float("HFT_MAX_POSITION_USD")
        if _max_pos_usd > 0.0:
            usd_notional = min(usd_notional, _max_pos_usd)
        # Limit price must be known before sizing: shares × limit_price must not
        # exceed budget. Previously shares = budget / best_ask while the order was
        # placed at best_ask + offset, so worst-case spend could exceed budget; the
        # main loop then applied min(budget, notional) and distorted entry_price.
        _buy_offset = req_float("LIVE_BUY_PRICE_OFFSET")
        price = max(0.01, min(0.99, exec_price + _buy_offset))
        shares = usd_notional / price

        if shares < poly_min_shares:
            min_cost = poly_min_shares * price
            logging.warning(
                "⚠️ Skip %s: budget %.2f USD → %.2f shares < CLOB minimum %.0f shares "
                "(need %.2f USD @ limit %.4f). Insufficient balance.",
                signal,
                usd_notional,
                shares,
                poly_min_shares,
                min_cost,
                price,
            )
            self._entry_stats["skip_signal"] += 1
            self._log_entry_stats_if_due()
            return _SKIP

        # Round down to 2 decimal places — CLOB rejects fractional shares beyond that.
        shares = float(int(shares * 100) / 100)
        if shares < poly_min_shares:
            shares = poly_min_shares

        # Place BUY at ask (or slightly above) for immediate fill.
        shares = self._affordable_buy_shares(price, shares)
        if shares < poly_min_shares:
            logging.warning(
                "⚠️ Skip %s: USDC balance only allows %.2f sh < min %.0f at %.4f.",
                signal,
                shares,
                poly_min_shares,
                price,
            )
            self._entry_stats["skip_signal"] += 1
            self._log_entry_stats_if_due()
            return _SKIP
        # Liquidity check: ensure top ask size can accommodate desired shares.
        # Only enforce if we have valid ask_size_top > 0; skip if data missing (test mode or stale book).
        try:
            snap = await asyncio.to_thread(self.get_orderbook_snapshot, token_id, depth=1)
            ask_size_top = snap.get("ask_size_top", 0.0)
            if ask_size_top > 0.0 and ask_size_top < shares:
                logging.warning(
                    "⚠️ Skip %s: top ask size %.2f < desired shares %.2f (insufficient liquidity).",
                    signal,
                    ask_size_top,
                    shares,
                )
                self._entry_stats["skip_signal"] += 1
                self._log_entry_stats_if_due()
                return _SKIP
        except Exception as exc:
            logging.warning("Liquidity check failed: %s — proceeding with caution.", exc)
        usdc_before_snapshot: float | None = None
        if not self.test_mode:
            usdc_before_snapshot = await asyncio.to_thread(self.fetch_usdc_balance)
        order_id, immediate = await self._place_limit_raw(token_id, BUY, price, shares)
        if not order_id:
            logging.error("execute: BUY placement failed for signal %s.", signal)
            self._log_entry_stats_if_due()
            return _SKIP

        tracked = TrackedOrder(
            order_id=order_id,
            token_id=token_id,
            side=BUY,
            price=price,
            size=shares,
            status=OrderStatus.FILLED if immediate else OrderStatus.PENDING,
            filled_size=shares if immediate else 0.0,
            entry_best_ask=best_ask,
            signal_ts=signal_ts,
            send_ts=send_ts if 'send_ts' in locals() else time.time(),
        )
        self._active_orders[order_id] = tracked
        self._entry_stats["executed"] += 1
        logging.info(
            "🟢 [LIVE] BUY placed: %s %.2f sh @ %.4f (%.2f USD) token=%s id=%s immediate=%s",
            signal,
            shares,
            price,
            shares * price,
            token_id[:20],
            order_id[:20],
            immediate,
        )

        if not immediate:
            # Phase 3: Enqueue initial REST response event to trigger FSM
            self._event_queue.put_nowait(RestResponseEvent(order_id=order_id, success=True, status="live"))
            # Wait for _poll_order to confirm fill
            await self._poll_order(tracked)

        filled = (
            tracked.filled_size
            if tracked.filled_size > 0
            else (tracked.size if tracked.status == OrderStatus.FILLED else 0.0)
        )
        avg_price = tracked.price

        self._log_entry_stats_if_due()

        # Order cancelled/failed — but a partial fill may have landed on-chain
        # (e.g. reprice rejected due to insufficient balance while first order
        # was already partially matched).  Check the actual CTF balance before
        # treating this as a skip to avoid phantom positions.
        if tracked.status in (OrderStatus.CANCELLED, OrderStatus.FAILED):
            _rescue_bal = await asyncio.to_thread(self.fetch_conditional_balance, token_id)
            if _rescue_bal and _rescue_bal >= req_float("POLY_CLOB_MIN_SHARES"):
                logging.warning(
                    "⚠️ [LIVE] BUY order %s status=%s but on-chain balance=%.4f sh — "
                    "treating as partial fill to avoid phantom position.",
                    tracked.order_id[:20],
                    tracked.status,
                    _rescue_bal,
                )
                self._confirmed_buys[token_id] = _rescue_bal
                self._active_orders.pop(tracked.order_id, None)
                return (_rescue_bal, tracked.price)
            logging.warning(
                "⚠️ [LIVE] BUY order %s (status=%s, on-chain=%.4f) — skip.",
                tracked.order_id[:20],
                tracked.status,
                _rescue_bal or 0.0,
            )
            self._active_orders.pop(tracked.order_id, None)
            return _SKIP

        # Order was confirmed FILLED or PARTIAL — shares were actually received on-chain.
        # Wait for the CLOB ledger to settle before reading the balance (observed lag
        # up to ~600 ms for immediate fills).  We loop until the balance appears or we
        # exhaust all retries, then TRUST the CLOB-reported fill so we never abandon a
        # real position.
        if filled <= 0:
            if self._last_buy_skip_reason is None and tracked.status == OrderStatus.STALE:
                self._last_buy_skip_reason = "stale_no_fill"
            logging.warning(
                "⚠️ [LIVE] BUY status=%s but filled=0 — skip.",
                tracked.status,
            )
            return _SKIP

        poly_min_shares = req_float("POLY_CLOB_MIN_SHARES")
        # Minimum fraction of the CLOB-reported fill that is accepted as a
        # "real" on-chain balance snapshot (not a partial ledger update).
        # If the on-chain read is < 10% of what CLOB reported, the ledger
        # has not settled yet and we continue polling rather than treating
        # the tiny value as the real post-fee balance.
        _bal_min_frac = req_float("LIVE_BALANCE_MIN_FRAC")
        _bal_delays = _parse_csv_floats(req_str("LIVE_BALANCE_CONFIRM_DELAYS_SEC"))
        actual_bal: float | None = None
        # Immediate CLOB matches (esp. after emergency) often land on-chain seconds later.
        if immediate and not self.test_mode:
            _im_wait = float(os.getenv("LIVE_IMMEDIATE_FILL_CHAIN_WAIT_SEC", "0.45"))
            if _im_wait > 0:
                await asyncio.sleep(_im_wait)
        for _i, _delay in enumerate(_bal_delays):
            await asyncio.sleep(_delay)
            _b = await asyncio.to_thread(self.fetch_conditional_balance, token_id)
            # Require balance >= 10% of CLOB-reported fill to accept as settled.
            if _b is not None and _b >= filled * _bal_min_frac:
                actual_bal = _b
                logging.info(
                    "🟢 [LIVE] On-chain balance confirmed: %.4f sh " "(attempt %d, delay %.1fs) token=%s",
                    actual_bal,
                    _i + 1,
                    _delay,
                    token_id[:20],
                )
                break
            next_delay = _bal_delays[_i + 1] if _i + 1 < len(_bal_delays) else 0
            logging.debug(
                "[LIVE] Balance %.4f < threshold %.4f on attempt %d " "— retrying in %.1fs token=%s",
                _b or 0.0,
                filled * _bal_min_frac,
                _i + 1,
                next_delay,
                token_id[:20],
            )

        if actual_bal is None and self.test_mode and filled > 0:
            actual_bal = filled

        # Strict mode: extra wait + polls when first backoff list is too short for RPC lag.
        _trust_clob_pref = os.getenv("LIVE_TRUST_CLOB_WITHOUT_CHAIN_BALANCE", "1") != "0"
        if actual_bal is None and not self.test_mode and filled > 0 and not _trust_clob_pref:
            _xw = float(os.getenv("LIVE_STRICT_CHAIN_EXTRA_WAIT_SEC", "4"))
            _xp = max(0, int(os.getenv("LIVE_STRICT_CHAIN_EXTRA_POLLS", "8")))
            _xg = float(os.getenv("LIVE_STRICT_CHAIN_EXTRA_POLL_GAP_SEC", "0.75"))
            if _xw > 0:
                logging.info(
                    "[LIVE] Strict mode: extra wait %.1fs before chain polls token=%s",
                    _xw,
                    token_id[:20],
                )
                await asyncio.sleep(_xw)
            for _ep in range(_xp):
                _b = await asyncio.to_thread(self.fetch_conditional_balance, token_id)
                if _b is not None and _b >= filled * _bal_min_frac:
                    actual_bal = _b
                    logging.info(
                        "🟢 [LIVE] On-chain balance confirmed (strict extra poll %d): " "%.4f sh token=%s",
                        _ep + 1,
                        actual_bal,
                        token_id[:20],
                    )
                    break
                await asyncio.sleep(_xg)

        if actual_bal is not None:
            filled_clob = float(filled)
            if abs(actual_bal - filled) > 0.005:
                logging.warning(
                    "⚠️ [LIVE] BUY adjusted for protocol fee: reported=%.4f actual=%.4f " "(fee=%.4f sh) token=%s",
                    filled,
                    actual_bal,
                    filled - actual_bal,
                    token_id[:20],
                )
            filled = float(actual_bal)
            if filled > 0.0 and filled_clob > filled + 1e-9:
                # Fewer shares credited than CLOB fill — same USD spent → higher $/sh for PnL.
                avg_price = avg_price * filled_clob / filled
        else:
            _trust_clob = os.getenv("LIVE_TRUST_CLOB_WITHOUT_CHAIN_BALANCE", "1") != "0"
            if not _trust_clob:
                logging.warning(
                    "⚠️ [LIVE] On-chain balance never matched CLOB fill after backoff + "
                    "strict extra polls — not opening position (strict mode). "
                    "CLOB reported %.4f sh token=%s. "
                    "Set LIVE_TRUST_CLOB_WITHOUT_CHAIN_BALANCE=1 or tune "
                    "LIVE_STRICT_CHAIN_EXTRA_* / LIVE_BALANCE_CONFIRM_DELAYS_SEC.",
                    filled,
                    token_id[:20],
                )
                self._last_buy_skip_reason = "strict_chain_timeout"
                self._purge_buy_orders_for_token(token_id)
                return _SKIP
            logging.warning(
                "⚠️ [LIVE] On-chain balance not confirmed after %d retries "
                "— trusting CLOB fill=%.4f token=%s (trust CLOB; set "
                "LIVE_TRUST_CLOB_WITHOUT_CHAIN_BALANCE=0 for strict chain-only).",
                len(_bal_delays),
                filled,
                token_id[:20],
            )

        _expected_spend = float(filled) * float(avg_price)
        if not await self._verify_usdc_debit_after_buy(usdc_before_snapshot, _expected_spend):
            self._last_buy_skip_reason = "usdc_debit_mismatch"
            self._purge_buy_orders_for_token(token_id)
            return _SKIP

        if filled < poly_min_shares:
            # Fee can leave balance just below POLY_CLOB_MIN_SHARES; we try to FAK out
            # to avoid an unsellable stub. If FAK fails or only partially clears, re-read
            # chain — any remaining balance must still be tracked so the engine can EXIT
            # (close_position uses FAK for sub-min SELL) instead of leaving phantom flat.
            logging.warning(
                "⚠️ [LIVE] Confirmed balance %.4f sh < min %.0f — " "attempting FAK residual exit. token=%s",
                filled,
                poly_min_shares,
                token_id[:20],
            )
            fak_filled = await self._fak_sell(token_id, filled)
            logging.info(
                "🔴 [LIVE] FAK residual exit: sold=%.4f token=%s",
                fak_filled,
                token_id[:20],
            )
            self._active_orders.pop(tracked.order_id, None)
            _dust = req_float("LIVE_INVENTORY_DUST_SHARES")
            _bal_after = await asyncio.to_thread(self.fetch_conditional_balance, token_id)
            if _bal_after is not None and _bal_after > _dust:
                logging.warning(
                    "⚠️ [LIVE] Wallet still holds %.4f sh after residual FAK "
                    "(failed or partial) — syncing open position for strategy EXIT. token=%s",
                    _bal_after,
                    token_id[:20],
                )
                filled = float(_bal_after)
                logging.info(
                    "🟢 [LIVE] BUY confirmed (sub-min shares): %.4f @ %.4f token=%s",
                    filled,
                    avg_price,
                    token_id[:20],
                )
                self._confirmed_buys[token_id] = filled
                return (filled, avg_price)
            return _SKIP

        logging.info(
            "🟢 [LIVE] BUY confirmed: %.4f shares @ %.4f token=%s",
            filled,
            avg_price,
            token_id[:20],
        )
        # Persist fill so close_position can find shares even after _active_orders cleanup.
        self._confirmed_buys[token_id] = filled
        # Remove from active orders — immediate fills skip _poll_order so the dict
        # entry would otherwise accumulate and inflate active_orders counter.
        self._active_orders.pop(tracked.order_id, None)
        return (filled, avg_price)
