"""Live execution and risk controls for Polymarket CLOB."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum

import requests

CLOB_BOOK_HTTP = "https://clob.polymarket.com/book"

_ORDER_FILL_POLL_SEC = float(os.getenv("LIVE_ORDER_FILL_POLL_SEC", "0.4"))
_ORDER_STALE_SEC = float(os.getenv("LIVE_ORDER_STALE_SEC", "3.0"))
_ORDER_MAX_REPRICE = int(os.getenv("LIVE_ORDER_MAX_REPRICE", "2"))
_ORDER_EMERGENCY_TICKS = int(os.getenv("LIVE_ORDER_EMERGENCY_TICKS", "3"))


class OrderStatus(str, Enum):
    """Lifecycle states for a tracked live order."""

    PENDING = "pending"
    FILLED = "filled"
    PARTIAL = "partial"
    STALE = "stale"
    CANCELLED = "cancelled"
    FAILED = "failed"


@dataclass
class TrackedOrder:
    """Single tracked CLOB order with lifecycle metadata."""

    order_id: str
    token_id: str
    side: str
    price: float
    size: float
    placed_at: float = field(default_factory=time.time)
    status: OrderStatus = OrderStatus.PENDING
    filled_size: float = 0.0
    reprice_count: int = 0

    @property
    def age_sec(self) -> float:
        """Return seconds since the order was placed."""
        return time.time() - self.placed_at

    @property
    def remaining(self) -> float:
        """Return unfilled size."""
        return max(0.0, self.size - self.filled_size)

    @property
    def is_stale(self) -> bool:
        """Return True when order has not filled within the stale window."""
        return self.age_sec >= _ORDER_STALE_SEC and self.status == OrderStatus.PENDING


def _levels_from_book_rows(rows: list | None) -> list[tuple[float, float]]:
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


try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL as SELL_SIDE
except Exception:  # pragma: no cover - optional runtime dependency
    ClobClient = None
    OrderArgs = None
    OrderType = None
    BUY = "BUY"
    SELL_SIDE = "SELL"


@dataclass
class LiveRiskManager:
    """Keep simple daily loss guard and trade counter."""

    max_daily_loss: float = -50.0
    pnl: float = 0.0
    trades: int = 0

    def update(self, pnl_change: float) -> None:
        """Accumulate realized pnl and number of trades."""
        self.pnl += pnl_change
        self.trades += 1

    def can_trade(self) -> bool:
        """Return False when daily drawdown limit is breached."""
        if self.pnl < self.max_daily_loss:
            logging.error("🛑 STOP: daily loss limit reached (%.2f).", self.pnl)
            return False
        return True


class LiveExecutionEngine:
    """Place safe limit orders against Polymarket CLOB with full order lifecycle management.

    Order lifecycle:
      1. execute() / close_position() places a GTC limit and tracks it as PENDING.
      2. _poll_order() polls fill status every LIVE_ORDER_FILL_POLL_SEC seconds.
      3. If unfilled after LIVE_ORDER_STALE_SEC the order is repriced up to
         LIVE_ORDER_MAX_REPRICE times toward best market price.
      4. If still unfilled after all reprice attempts, emergency_exit() is called
         which cancels the stale order and places an aggressive market-crossing limit.
      5. emergency_exit() can also be triggered externally when the engine decides
         the position must close regardless of conditions.
    """

    def __init__(
        self,
        private_key: str | None,
        funder: str | None,
        test_mode: bool = True,
        min_order_size: float = 10.0,
        max_spread: float = 0.03,
    ) -> None:
        """Initialise execution engine and optionally connect to Polymarket CLOB."""
        self.test_mode = test_mode
        self.min_order_size = min_order_size
        self.max_spread = max_spread
        self.max_entry_ask = float(os.getenv("HFT_MAX_ENTRY_ASK", "0.99"))
        self.skip_stats_log_sec = float(os.getenv("HFT_LIVE_SKIP_STATS_LOG_SEC", "30"))
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
        self._active_orders: dict[str, TrackedOrder] = {}
        self.client = None
        self._http = requests.Session()

        if ClobClient is None:
            if not self.test_mode:
                raise RuntimeError("py_clob_client is not installed.")
            return

        sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "2"))
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
            logging.info(
                "[LIVE] ClobClient credentials derived from private key (key=%.8s...).",
                derived.api_key,
            )

    def fetch_usdc_balance(self) -> float | None:
        """Return available USDC balance on the Polymarket CLOB account.

        Returns None when the client is unavailable or the call fails.
        In test_mode returns None (no real account to check).
        """
        if self.test_mode or self.client is None:
            return None
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
            sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "2"))
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=sig_type,
            )
            resp = self.client.get_balance_allowance(params=params)
            if isinstance(resp, dict):
                raw = resp.get("balance") or resp.get("allowance")
            else:
                raw = getattr(resp, "balance", None) or getattr(resp, "allowance", None)
            if raw is None:
                return None
            # USDC on Polymarket CLOB is denominated in 1e-6 units (micro-USDC).
            return float(raw) / 1_000_000.0
        except Exception as exc:
            logging.warning("fetch_usdc_balance failed: %s", exc)
            return None

    def get_best_prices(self, token_id: str) -> tuple[float, float]:
        """Return best bid and best ask from CLOB order book."""
        snap = self.get_orderbook_snapshot(token_id, depth=1)
        return float(snap["best_bid"]), float(snap["best_ask"])

    def _orderbook_snapshot_http(self, token_id: str, depth: int) -> dict:
        """Fetch and summarize the order book from the public CLOB HTTP endpoint."""
        try:
            resp = self._http.get(CLOB_BOOK_HTTP, params={"token_id": token_id}, timeout=5)
            resp.raise_for_status()
            data = resp.json()
            bid_levels = _levels_from_book_rows(data.get("bids"))
            ask_levels = _levels_from_book_rows(data.get("asks"))
            return _snapshot_from_levels(bid_levels, ask_levels, depth)
        except Exception as exc:
            logging.warning(
                "HTTP CLOB book failed token=%s…: %s",
                token_id[:28] if token_id else "",
                exc,
            )
            return {
                "best_bid": 0.0,
                "best_ask": 1.0,
                "bid_size_top": 0.0,
                "ask_size_top": 0.0,
                "imbalance": 0.0,
                "bid_vol_topn": 0.0,
                "ask_vol_topn": 0.0,
                "pressure": 0.0,
            }

    def get_orderbook_snapshot(self, token_id: str, depth: int = 5) -> dict:
        """Return top-N orderbook metrics for imbalance and pressure."""
        if self.client is None:
            return self._orderbook_snapshot_http(token_id, depth)
        book = self.client.get_order_book(token_id)
        bid_levels = _levels_from_book_rows(book.bids)
        ask_levels = _levels_from_book_rows(book.asks)
        return _snapshot_from_levels(bid_levels, ask_levels, depth)

    def _get_order_fill(self, order_id: str) -> tuple[str, float]:
        """Return (status_str, filled_size) for an active order from CLOB.

        Returns ("unknown", 0.0) when the client is unavailable or in test mode.
        """
        if self.test_mode or self.client is None:
            return "unknown", 0.0
        try:
            resp = self.client.get_order(order_id)
            status = str(getattr(resp, "status", "unknown")).lower()
            filled = float(getattr(resp, "size_matched", 0.0) or 0.0)
            return status, filled
        except Exception as exc:
            logging.warning("Order fill poll failed order=%s: %s", order_id, exc)
            return "unknown", 0.0

    def _cancel_order(self, order_id: str) -> bool:
        """Cancel an open order; return True on success."""
        if self.test_mode or self.client is None:
            logging.info("[SIM] Cancel order %s.", order_id)
            return True
        try:
            self.client.cancel(order_id)
            logging.info("[LIVE] Cancelled order %s.", order_id)
            return True
        except Exception as exc:
            logging.warning("Cancel failed order=%s: %s", order_id, exc)
            return False

    def _place_limit_raw(self, token_id: str, side: str, price: float, size: float) -> str | None:
        """Submit a GTC limit order; return order_id or None on failure."""
        if self.test_mode:
            fake_id = f"sim-{side}-{int(time.time() * 1000)}"
            logging.info(
                "[SIM LIMIT] %s size=%.2f @ %.4f token=%s id=%s",
                side, size, price, token_id, fake_id,
            )
            return fake_id
        if OrderArgs is None or self.client is None:
            logging.error("Cannot place order: py_clob_client unavailable.")
            return None
        try:
            order = OrderArgs(token_id=token_id, price=price, size=size, side=side)
            signed = self.client.create_order(order)
            resp = self.client.post_order(signed, OrderType.GTC)
            order_id = str(getattr(resp, "order_id", resp))
            logging.info(
                "[LIVE] %s size=%.2f @ %.4f token=%s -> id=%s",
                side, size, price, token_id, order_id,
            )
            return order_id
        except Exception as exc:
            logging.error("Order placement failed %s @ %.4f: %s", side, price, exc)
            return None

    async def _poll_order(self, tracked: TrackedOrder) -> None:
        """Monitor fill status and reprice or emergency-exit stale orders.

        Poll loop runs until the order reaches a terminal state
        (filled, cancelled, or failed).
        """
        while tracked.status == OrderStatus.PENDING:
            await asyncio.sleep(_ORDER_FILL_POLL_SEC)

            status_str, filled = await asyncio.to_thread(
                self._get_order_fill, tracked.order_id
            )

            if status_str in ("matched", "filled"):
                tracked.status = OrderStatus.FILLED
                tracked.filled_size = tracked.size
                logging.info(
                    "✅ Order filled: id=%s %s %.2f @ %.4f",
                    tracked.order_id, tracked.side, tracked.size, tracked.price,
                )
                break

            if status_str == "partially_matched":
                tracked.status = OrderStatus.PARTIAL
                tracked.filled_size = filled
                logging.info(
                    "⚡ Order partial: id=%s filled=%.2f / %.2f",
                    tracked.order_id, filled, tracked.size,
                )

            if not tracked.is_stale:
                continue

            if tracked.reprice_count >= _ORDER_MAX_REPRICE:
                logging.warning(
                    "⚠️ Order stale after %d reprice attempts id=%s — emergency exit.",
                    _ORDER_MAX_REPRICE, tracked.order_id,
                )
                self._cancel_order(tracked.order_id)
                tracked.status = OrderStatus.STALE
                await self._emergency_exit_order(tracked)
                break

            best_bid, best_ask = await asyncio.to_thread(self.get_best_prices, tracked.token_id)
            self._cancel_order(tracked.order_id)

            if tracked.side == BUY:
                new_price = max(0.01, min(0.99, best_ask + 0.001))
            else:
                new_price = max(0.01, min(0.99, best_bid - 0.001))

            if abs(new_price - tracked.price) < 0.001:
                logging.info(
                    "Price unchanged after reprice (%.4f) — skipping reprice %d.",
                    new_price, tracked.reprice_count + 1,
                )
                tracked.reprice_count += 1
                tracked.placed_at = time.time()
                continue

            self._entry_stats["reprice_total"] += 1
            tracked.reprice_count += 1
            remaining = tracked.remaining if tracked.status == OrderStatus.PARTIAL else tracked.size
            logging.info(
                "🔄 Repricing order %s: %.4f → %.4f (attempt %d/%d)",
                tracked.order_id, tracked.price, new_price,
                tracked.reprice_count, _ORDER_MAX_REPRICE,
            )
            new_id = await asyncio.to_thread(
                self._place_limit_raw, tracked.token_id, tracked.side, new_price, remaining
            )
            if new_id:
                tracked.order_id = new_id
                tracked.price = new_price
                tracked.placed_at = time.time()
                tracked.status = OrderStatus.PENDING
                self._active_orders[new_id] = tracked
            else:
                tracked.status = OrderStatus.FAILED
                logging.error("Reprice placement failed — position may be unmanaged.")
                break

        self._active_orders.pop(tracked.order_id, None)

    async def _emergency_exit_order(self, tracked: TrackedOrder) -> None:
        """Cross the spread aggressively to guarantee fill of remaining size.

        Used when normal repricing exhausted or when force-close is requested.
        Places a limit at best market price + aggressive offset to cross the book.
        """
        self._entry_stats["emergency_exits"] += 1
        remaining = tracked.remaining if tracked.status in (
            OrderStatus.PARTIAL, OrderStatus.STALE
        ) else tracked.size
        if remaining <= 0:
            return

        best_bid, best_ask = await asyncio.to_thread(self.get_best_prices, tracked.token_id)

        if tracked.side == BUY:
            price = max(0.01, min(0.99, best_ask + 0.005))
        else:
            price = max(0.01, min(0.99, best_bid - 0.005))

        logging.warning(
            "🚨 EMERGENCY EXIT: %s %.2f @ %.4f token=%s",
            tracked.side, remaining, price, tracked.token_id,
        )
        order_id = await asyncio.to_thread(
            self._place_limit_raw, tracked.token_id, tracked.side, price, remaining
        )
        if order_id:
            emergency = TrackedOrder(
                order_id=order_id,
                token_id=tracked.token_id,
                side=tracked.side,
                price=price,
                size=remaining,
            )
            self._active_orders[order_id] = emergency
            asyncio.ensure_future(self._poll_order(emergency))
        else:
            logging.error(
                "🛑 Emergency exit placement FAILED token=%s — manual intervention required.",
                tracked.token_id,
            )

    async def emergency_exit(self, token_id: str, size: float, side: str = SELL_SIDE) -> None:
        """Externally triggered emergency close: cancel all open orders then cross the book.

        Called by the engine when the position must be closed immediately
        (e.g. market regime deteriorated, trailing SL hit, slot expiry, shutdown).
        """
        pending = [o for o in list(self._active_orders.values()) if o.token_id == token_id]
        for order in pending:
            self._cancel_order(order.order_id)
            order.status = OrderStatus.CANCELLED
            self._active_orders.pop(order.order_id, None)

        if size <= 0:
            return

        best_bid, best_ask = await asyncio.to_thread(self.get_best_prices, token_id)
        if side == SELL_SIDE:
            price = max(0.01, min(0.99, best_bid - 0.005))
        else:
            price = max(0.01, min(0.99, best_ask + 0.005))

        logging.warning(
            "🚨 EMERGENCY CLOSE: %s %.2f @ %.4f token=%s",
            side, size, price, token_id,
        )
        self._entry_stats["emergency_exits"] += 1
        order_id = await asyncio.to_thread(self._place_limit_raw, token_id, side, price, size)
        if order_id:
            tracked = TrackedOrder(
                order_id=order_id,
                token_id=token_id,
                side=side,
                price=price,
                size=size,
            )
            self._active_orders[order_id] = tracked
            asyncio.ensure_future(self._poll_order(tracked))
        else:
            logging.error(
                "🛑 Emergency close FAILED token=%s — manual intervention required.", token_id
            )

    def _log_entry_stats_if_due(self) -> None:
        """Emit aggregated live entry stats periodically for gate diagnostics."""
        if self.skip_stats_log_sec <= 0:
            return
        now = time.time()
        if now - self._last_skip_stats_log_ts < self.skip_stats_log_sec:
            return
        st = self._entry_stats
        logging.info(
            "Live entry stats: attempts=%s executed=%s skip_ask_cap=%s "
            "skip_spread=%s skip_signal=%s reprice=%s emergency=%s active_orders=%s.",
            st["attempts"], st["executed"], st["skip_ask_cap"],
            st["skip_spread"], st["skip_signal"], st["reprice_total"],
            st["emergency_exits"], len(self._active_orders),
        )
        self._last_skip_stats_log_ts = now

    async def close_position(self, token_id: str, size: float) -> None:
        """Place a SELL limit order to close an open long position with lifecycle tracking."""
        if size <= 0:
            return
        best_bid, _ = await asyncio.to_thread(self.get_best_prices, token_id)
        price = max(0.01, min(0.99, best_bid + 0.002))
        order_id = await asyncio.to_thread(
            self._place_limit_raw, token_id, SELL_SIDE, price, size
        )
        if not order_id:
            logging.warning("close_position: placement failed, trying emergency exit.")
            await self.emergency_exit(token_id, size, side=SELL_SIDE)
            return
        tracked = TrackedOrder(
            order_id=order_id,
            token_id=token_id,
            side=SELL_SIDE,
            price=price,
            size=size,
        )
        self._active_orders[order_id] = tracked
        asyncio.ensure_future(self._poll_order(tracked))
        logging.info("[LIVE] Close position tracked: SELL %.2f @ %.4f id=%s", size, price, order_id)

    async def execute(
        self,
        signal: str,
        token_id: str,
        order_size: float | None = None,
        budget_usd: float | None = None,
    ) -> None:
        """Validate spread and place limit BUY order with full lifecycle tracking.

        Size resolution priority:
          1. ``order_size`` when given — treated as USD notional and converted to
             shares using the current best_ask price.
          2. ``budget_usd`` — USD budget to convert to shares at best_ask.
          3. ``min_order_size`` (from LIVE_ORDER_SIZE config) as USD notional.

        The resulting shares are clamped to the Polymarket CLOB minimum
        (POLY_CLOB_MIN_SHARES, default 5) and floored so that an insufficient
        budget causes a logged skip rather than an invalid order.
        Supports both BUY_UP and BUY_DOWN signals.
        """
        self._entry_stats["attempts"] += 1
        best_bid, best_ask = await asyncio.to_thread(self.get_best_prices, token_id)

        if best_ask >= self.max_entry_ask:
            self._entry_stats["skip_ask_cap"] += 1
            logging.warning(
                "Skip %s: best_ask %.4f >= max entry ask %.4f.",
                signal, best_ask, self.max_entry_ask,
            )
            self._log_entry_stats_if_due()
            return

        spread = best_ask - best_bid
        if spread <= 0 or spread > self.max_spread:
            self._entry_stats["skip_spread"] += 1
            logging.warning("⚠️ Bad spread %.4f, skip signal %s.", spread, signal)
            self._log_entry_stats_if_due()
            return

        if signal not in ("BUY_UP", "BUY_DOWN"):
            self._entry_stats["skip_signal"] += 1
            logging.warning("Skip signal: unsupported live signal %s.", signal)
            self._log_entry_stats_if_due()
            return

        # Polymarket CLOB requires at least this many shares per order.
        poly_min_shares = float(os.getenv("POLY_CLOB_MIN_SHARES", "5"))

        # Convert USD notional → shares using current ask price.
        exec_price = max(0.001, best_ask)
        usd_notional = order_size or budget_usd or self.min_order_size
        shares = usd_notional / exec_price

        if shares < poly_min_shares:
            # Try to fill up to the minimum using all available budget.
            min_cost = poly_min_shares * exec_price
            logging.warning(
                "⚠️ Skip %s: budget %.2f USD → %.2f shares < CLOB minimum %.0f shares "
                "(need %.2f USD @ %.4f). Insufficient balance.",
                signal, usd_notional, shares, poly_min_shares, min_cost, exec_price,
            )
            self._entry_stats["skip_signal"] += 1
            self._log_entry_stats_if_due()
            return

        # Round down to 2 decimal places — CLOB rejects fractional shares beyond that.
        shares = float(int(shares * 100) / 100)
        if shares < poly_min_shares:
            shares = poly_min_shares

        # Limit order just inside best ask to maximise fill probability.
        price = max(0.01, min(0.99, exec_price - 0.002))
        order_id = await asyncio.to_thread(
            self._place_limit_raw, token_id, BUY, price, shares
        )
        if not order_id:
            logging.error("execute: BUY placement failed for signal %s.", signal)
            self._log_entry_stats_if_due()
            return

        tracked = TrackedOrder(
            order_id=order_id,
            token_id=token_id,
            side=BUY,
            price=price,
            size=shares,
        )
        self._active_orders[order_id] = tracked
        asyncio.ensure_future(self._poll_order(tracked))
        self._entry_stats["executed"] += 1
        logging.info(
            "[LIVE] Entry tracked: %s %.2f shares @ %.4f (%.2f USD) token=%s id=%s",
            signal, shares, price, shares * price, token_id[:20], order_id,
        )
        self._log_entry_stats_if_due()
