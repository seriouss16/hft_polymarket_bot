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
        """Return True when order has not progressed within the stale window.

        Applies to both PENDING and PARTIAL states since a partial fill can also
        stall indefinitely when the book moves away.
        """
        return (
            self.age_sec >= _ORDER_STALE_SEC
            and self.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)
        )


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
            logging.error("🛑 STOP: daily loss limit reached (pnl=%.4f limit=%.4f).", self.pnl, self.max_daily_loss)
            return False
        return True

    def log_status(self) -> None:
        """Log current risk state for diagnostics."""
        logging.info(
            "[LIVE RISK] session_pnl=%.4f max_daily_loss=%.4f trades=%d can_trade=%s",
            self.pnl, self.max_daily_loss, self.trades, self.can_trade(),
        )


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
        self.min_entry_ask = float(os.getenv("HFT_MIN_ENTRY_ASK", "0.08"))
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
        # Last confirmed BUY fills, keyed by token_id.  Persists after the order
        # leaves _active_orders so that close_position can still find the shares.
        # Cleared explicitly by clear_filled_buy() after a SELL completes.
        self._confirmed_buys: dict[str, float] = {}
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
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
            sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "2"))
            params = BalanceAllowanceParams(
                asset_type=AssetType.COLLATERAL,
                signature_type=sig_type,
            )
            resp = self.client.update_balance_allowance(params=params)
            logging.info("[LIVE] COLLATERAL allowance refreshed: %s", resp)
        except Exception as exc:
            logging.error(
                "[LIVE] ensure_allowances failed: %s — BUY orders may be rejected.", exc,
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
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
            sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "2"))
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=sig_type,
            )
            resp = self.client.update_balance_allowance(params=params)
            logging.info(
                "[LIVE] CONDITIONAL allowance refreshed: token=%s resp=%s",
                token_id[:20], resp,
            )
        except Exception as exc:
            logging.error(
                "[LIVE] ensure_conditional_allowance failed for token=%s: %s "
                "— SELL may be rejected.",
                token_id[:20], exc,
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
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams
            sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "2"))
            params = BalanceAllowanceParams(
                asset_type=AssetType.CONDITIONAL,
                token_id=token_id,
                signature_type=sig_type,
            )
            resp = self.client.get_balance_allowance(params=params)
            raw = (
                resp.get("balance") if isinstance(resp, dict)
                else getattr(resp, "balance", None)
            )
            if raw is None:
                return None
            bal = float(raw) / 1_000_000.0
            logging.debug(
                "[LIVE] Conditional balance: token=%s raw=%s → %.6f shares",
                token_id[:20], raw, bal,
            )
            return bal
        except Exception as exc:
            logging.warning(
                "[LIVE] fetch_conditional_balance failed token=%s: %s", token_id[:20], exc,
            )
            return None

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

        Uses the authenticated client.get_order() call which requires L2 credentials.
        The previous approach of GET /order/{id} always returned 404 — that endpoint
        does not exist on the Polymarket CLOB; the correct API is GET /order/{id}
        via the SDK which internally uses authenticated /orders?id= calls.
        Returns ("unknown", 0.0) when the client is unavailable or in test mode.
        """
        if self.test_mode or self.client is None:
            return "unknown", 0.0
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
            from py_clob_client.clob_types import MarketOrderArgs
            best_bid, _ = self.get_best_prices(token_id)
            worst_price = max(0.01, round(best_bid * 0.90, 4))
            order_args = MarketOrderArgs(
                token_id=token_id,
                side=SELL_SIDE,
                amount=size,
                price=worst_price,
            )
            order = self.client.create_market_order(order_args)
            resp = self.client.post_order(order, OrderType.FAK)
            status = str(
                resp.get("status", "") if isinstance(resp, dict)
                else getattr(resp, "status", "")
            ).lower()
            order_id = str(
                resp.get("orderID") or resp.get("order_id", "")
                if isinstance(resp, dict) else getattr(resp, "order_id", "")
            )
            logging.info(
                "[LIVE FAK SELL] size=%.4f worst_px=%.4f → id=%s status=%s token=%s",
                size, worst_price, order_id[:20] if order_id else "?", status, token_id[:20],
            )
            if status in ("matched", "filled", "live", "delayed", "unmatched"):
                # FAK may fill partially or fully — poll the actual fill amount.
                if order_id:
                    fill_status, filled = self._get_order_fill(order_id)
                    if filled > 0:
                        return (filled, worst_price)
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

    def _place_limit_raw(
        self, token_id: str, side: str, price: float, size: float
    ) -> tuple[str | None, bool]:
        """Submit a GTC limit order; return (order_id, immediate_fill) or (None, False).

        immediate_fill is True when the CLOB responds with status='matched' meaning
        the order was fully filled synchronously (no need to poll).
        The order_id key in Polymarket CLOB dict responses is 'orderID' (capital D).
        """
        if self.test_mode:
            fake_id = f"sim-{side}-{int(time.time() * 1000)}"
            logging.info(
                "[SIM LIMIT] %s size=%.2f @ %.4f token=%s id=%s",
                side, size, price, token_id, fake_id,
            )
            return fake_id, False
        if OrderArgs is None or self.client is None:
            logging.error("Cannot place order: py_clob_client unavailable.")
            return None, False
        try:
            order = OrderArgs(token_id=token_id, price=price, size=size, side=side)
            signed = self.client.create_order(order)
            resp = self.client.post_order(signed, OrderType.GTC)
            if isinstance(resp, dict):
                order_id = str(resp.get("orderID") or resp.get("order_id") or "")
                immediate = str(resp.get("status", "")).lower() in ("matched", "filled")
            else:
                order_id = str(getattr(resp, "order_id", "") or "")
                immediate = str(getattr(resp, "status", "")).lower() in ("matched", "filled")
            if not order_id:
                logging.error(
                    "Order placement: no order_id in response %s @ %.4f resp=%s",
                    side, price, resp,
                )
                return None, False
            logging.info(
                "[LIVE] %s size=%.2f @ %.4f token=%s -> id=%s immediate_fill=%s",
                side, size, price, token_id[:20], order_id[:20], immediate,
            )
            return order_id, immediate
        except Exception as exc:
            logging.error("Order placement failed %s @ %.4f: %s", side, price, exc)
            return None, False

    async def _fak_sell(self, token_id: str, size: float) -> float:
        """Execute a FAK market SELL and return total filled shares.

        Delegates to _place_fak_sell (sync) and logs the result.
        Used when a sub-minimum SELL is needed (CLOB rejects GTC for < min_shares).
        """
        filled, price = await asyncio.to_thread(self._place_fak_sell, token_id, size)
        if filled > 0:
            logging.info(
                "🔴 [LIVE] FAK SELL done: filled=%.4f / %.4f @ %.4f token=%s",
                filled, size, price, token_id[:20],
            )
        else:
            logging.error(
                "🛑 FAK SELL failed: %.4f shares token=%s — manual intervention required.",
                size, token_id[:20],
            )
        return filled

    async def _poll_order(self, tracked: TrackedOrder) -> None:
        """Monitor fill status; reprice stale orders; handle partial fills correctly.

        Loop terminates when the order reaches a terminal state.  Partial fills
        accumulate across reprice cycles — ``tracked.filled_size`` always reflects
        the running total confirmed by the CLOB.

        BUY partial fill logic:
          - If a BUY goes stale with partial fill < POLY_CLOB_MIN_SHARES: cancel the
            BUY and FAK-SELL the already-filled shares so we exit cleanly without
            holding a position we cannot later sell via a normal limit.
          - If partial fill >= min_shares: reprice or emergency-exit as normal.

        SELL partial fill logic:
          - If remaining < min_shares: use FAK SELL instead of a sub-minimum GTC
            (CLOB rejects GTC below the minimum).
        """
        poly_min = float(os.getenv("POLY_CLOB_MIN_SHARES", "5"))

        while tracked.status in (OrderStatus.PENDING, OrderStatus.PARTIAL):
            await asyncio.sleep(_ORDER_FILL_POLL_SEC)

            status_str, clob_filled = await asyncio.to_thread(
                self._get_order_fill, tracked.order_id
            )

            # Polymarket API statuses (after normalisation to lower, prefix stripped):
            #   "live"            — order is open, not yet matched
            #   "matched"         — fully matched (= filled)
            #   "canceled"        — cancelled by user or system
            #   "partially_matched" — some shares filled, rest still open
            # Historic aliases still accepted: "filled", "order_status_matched".
            if status_str in ("matched", "filled"):
                tracked.status = OrderStatus.FILLED
                tracked.filled_size = tracked.size
                logging.info(
                    "✅ Order filled: id=%s %s %.2f @ %.4f",
                    tracked.order_id, tracked.side, tracked.size, tracked.price,
                )
                break

            if status_str in ("canceled", "cancelled", "canceled_market_resolved"):
                # Cancelled externally — may have a partial fill; let reprice/rescue handle.
                tracked.status = OrderStatus.CANCELLED
                tracked.filled_size = clob_filled
                logging.info(
                    "🚫 Order cancelled externally: id=%s filled=%.2f",
                    tracked.order_id, clob_filled,
                )
                break

            if status_str in ("partially_matched",) and clob_filled > tracked.filled_size:
                tracked.filled_size = clob_filled
                tracked.status = OrderStatus.PARTIAL
                logging.info(
                    "⚡ Order partial: id=%s filled=%.2f / %.2f remaining=%.2f",
                    tracked.order_id, clob_filled, tracked.size, tracked.remaining,
                )
                # Reset stale timer on new activity.
                tracked.placed_at = time.time()

            # "live" or "unknown" — order still open, continue polling.

            if not tracked.is_stale:
                continue

            remaining = tracked.remaining
            if remaining <= 0:
                tracked.status = OrderStatus.FILLED
                break

            # --- BUY stale with partial fill below CLOB minimum ---
            # Cancel the pending BUY and FAK-SELL what was already filled to avoid
            # holding unsellable shares.
            if tracked.side == BUY and 0 < tracked.filled_size < poly_min:
                self._cancel_order(tracked.order_id)
                logging.warning(
                    "⚠️ BUY stale with partial fill %.2f < min %.0f shares — "
                    "cancelling BUY and FAK-selling filled shares.",
                    tracked.filled_size, poly_min,
                )
                fak_filled = await self._fak_sell(tracked.token_id, tracked.filled_size)
                # Report net filled as zero so caller treats this as a skip.
                tracked.filled_size = 0.0
                tracked.status = OrderStatus.CANCELLED
                logging.info(
                    "[LIVE] BUY partial exit: FAK sold %.4f shares token=%s.",
                    fak_filled, tracked.token_id[:20],
                )
                break

            if tracked.reprice_count >= _ORDER_MAX_REPRICE:
                logging.warning(
                    "⚠️ Order stale after %d reprice attempts id=%s — emergency exit "
                    "(filled=%.2f remaining=%.2f).",
                    _ORDER_MAX_REPRICE, tracked.order_id,
                    tracked.filled_size, remaining,
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

            # SELL with remaining < min_shares: use FAK to avoid GTC rejection.
            if tracked.side == SELL_SIDE and remaining < poly_min:
                logging.warning(
                    "⚠️ SELL remaining %.2f < min %.0f shares — FAK sell.",
                    remaining, poly_min,
                )
                fak_filled = await self._fak_sell(tracked.token_id, remaining)
                tracked.filled_size += fak_filled
                tracked.status = OrderStatus.FILLED
                break

            if abs(new_price - tracked.price) < 0.001:
                tracked.reprice_count += 1
                tracked.placed_at = time.time()
                continue

            self._entry_stats["reprice_total"] += 1
            tracked.reprice_count += 1
            logging.info(
                "🔄 Repricing order %s: %.4f → %.4f (attempt %d/%d) "
                "filled=%.2f remaining=%.2f",
                tracked.order_id, tracked.price, new_price,
                tracked.reprice_count, _ORDER_MAX_REPRICE,
                tracked.filled_size, remaining,
            )
            new_id, new_immediate = await asyncio.to_thread(
                self._place_limit_raw, tracked.token_id, tracked.side, new_price, remaining
            )
            if new_id:
                self._active_orders.pop(tracked.order_id, None)
                tracked.order_id = new_id
                tracked.price = new_price
                tracked.placed_at = time.time()
                # Preserve accumulated filled_size; only remaining goes into new order.
                tracked.status = OrderStatus.FILLED if new_immediate else OrderStatus.PENDING
                self._active_orders[new_id] = tracked
                if new_immediate:
                    tracked.filled_size += remaining
                    break
            else:
                tracked.status = OrderStatus.FAILED
                logging.error(
                    "Reprice placement failed — filled=%.2f remaining=%.2f unmanaged.",
                    tracked.filled_size, remaining,
                )
                break

        self._active_orders.pop(tracked.order_id, None)

    async def _emergency_exit_order(self, tracked: TrackedOrder) -> None:
        """Exit remaining size aggressively after reprice attempts exhausted.

        For SELL orders: uses FAK market order which works for any size including
        sub-minimum.  For BUY orders: crosses the spread with a GTC limit.
        Updates ``tracked.filled_size`` with any additional fills.
        """
        self._entry_stats["emergency_exits"] += 1
        poly_min = float(os.getenv("POLY_CLOB_MIN_SHARES", "5"))
        remaining = tracked.remaining if tracked.status in (
            OrderStatus.PARTIAL, OrderStatus.STALE
        ) else tracked.size
        if remaining <= 0:
            return

        logging.warning(
            "🚨 EMERGENCY EXIT: %s %.2f token=%s (min=%.0f filled=%.2f)",
            tracked.side, remaining, tracked.token_id[:20], poly_min, tracked.filled_size,
        )

        if tracked.side == SELL_SIDE:
            # FAK handles any size including sub-minimum — preferred for all SELL exits.
            fak_filled = await self._fak_sell(tracked.token_id, remaining)
            tracked.filled_size += fak_filled
        else:
            best_bid, best_ask = await asyncio.to_thread(self.get_best_prices, tracked.token_id)
            price = max(0.01, min(0.99, best_ask + 0.005))
            order_id, immediate = await asyncio.to_thread(
                self._place_limit_raw, tracked.token_id, tracked.side, price, remaining
            )
            if order_id:
                emergency = TrackedOrder(
                    order_id=order_id,
                    token_id=tracked.token_id,
                    side=tracked.side,
                    price=price,
                    size=remaining,
                    status=OrderStatus.FILLED if immediate else OrderStatus.PENDING,
                    filled_size=remaining if immediate else 0.0,
                )
                self._active_orders[order_id] = emergency
                if not immediate:
                    await self._poll_order(emergency)
                tracked.filled_size += emergency.filled_size
            else:
                logging.error(
                    "🛑 Emergency BUY placement FAILED token=%s remaining=%.2f"
                    " — manual intervention required.",
                    tracked.token_id, remaining,
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
        order_id, immediate = await asyncio.to_thread(
            self._place_limit_raw, token_id, side, price, size
        )
        if order_id:
            tracked = TrackedOrder(
                order_id=order_id,
            token_id=token_id,
                side=side,
            price=price,
            size=size,
                status=OrderStatus.FILLED if immediate else OrderStatus.PENDING,
                filled_size=size if immediate else 0.0,
            )
            self._active_orders[order_id] = tracked
            if not immediate:
                asyncio.ensure_future(self._poll_order(tracked))
        else:
            logging.error(
                "🛑 Emergency close FAILED token=%s — manual intervention required.", token_id
            )

    def filled_buy_shares(self, token_id: str) -> float:
        """Return total filled BUY shares currently tracked for token_id.

        Checks both active orders (while poll is running) and the confirmed-buys
        cache (after the order leaves _active_orders).  This prevents the phantom-
        position bug where CLOSE arrives after _active_orders cleanup but before
        clear_filled_buy() is called.
        """
        total = 0.0
        for order in self._active_orders.values():
            if order.token_id != token_id or order.side != BUY:
                continue
            if order.status == OrderStatus.FILLED:
                total += order.filled_size if order.filled_size > 0 else order.size
            elif order.status in (OrderStatus.PENDING, OrderStatus.PARTIAL):
                total += order.filled_size
        if total == 0.0 and token_id in self._confirmed_buys:
            total = self._confirmed_buys[token_id]
        return total

    def clear_filled_buy(self, token_id: str) -> None:
        """Remove the confirmed-buy entry for token_id after a SELL completes."""
        self._confirmed_buys.pop(token_id, None)

    def has_pending_buy(self, token_id: str) -> bool:
        """Return True when there is at least one non-terminal BUY order for token_id.

        Used to detect the race condition where SIM triggers CLOSE before the live
        BUY order has been confirmed filled by the CLOB poll loop.
        """
        return any(
            o.token_id == token_id
            and o.side == BUY
            and o.status in (OrderStatus.PENDING, OrderStatus.PARTIAL)
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
            if not self.has_pending_buy(token_id):
                break
            await asyncio.sleep(_ORDER_FILL_POLL_SEC)
        filled = self.filled_buy_shares(token_id)
        logging.info(
            "[LIVE] wait_for_buy_fill done: token=%s filled=%.4f shares",
            token_id[:20], filled,
        )
        return filled

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

    async def close_position(self, token_id: str, size: float) -> tuple[float, float]:
        """Sell all ``size`` shares and return (total_filled_shares, avg_price).

        - size >= POLY_CLOB_MIN_SHARES: GTC limit just above bid; _poll_order handles
          partial fills and reprices.  Sub-minimum remainder uses FAK automatically.
        - size < POLY_CLOB_MIN_SHARES: FAK market SELL immediately — CLOB rejects
          GTC below the minimum.
        - GTC placement failure: FAK fallback then emergency_exit.

        Blocks until a terminal state and returns (0.0, 0.0) on total failure.
        """
        if size <= 0:
            return (0.0, 0.0)

        poly_min = float(os.getenv("POLY_CLOB_MIN_SHARES", "5"))

        # Verify actual on-chain CTF balance before placing any SELL.  Polymarket
        # deducts a protocol fee in shares at fill time, so the wallet balance may
        # be slightly less than the CLOB-reported filled_size.  Selling more than
        # held always fails with "not enough balance / allowance".
        # If the balance API returns 0 (ledger lag) we keep the original size and
        # let the CLOB reject minimally — retry is handled by _poll_order reprice.
        actual_bal = await asyncio.to_thread(self.fetch_conditional_balance, token_id)
        if actual_bal is not None and actual_bal > 0 and actual_bal < size:
            logging.warning(
                "⚠️ [LIVE] SELL size corrected: %.4f → %.4f "
                "(on-chain balance after fee) token=%s",
                size, actual_bal, token_id[:20],
            )
            size = actual_bal
        elif actual_bal is not None and actual_bal == 0:
            logging.warning(
                "⚠️ [LIVE] close_position: on-chain balance=0 (possible lag) — "
                "keeping requested size=%.4f token=%s",
                size, token_id[:20],
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
                size, poly_min,
            )
            filled, price = await asyncio.to_thread(self._place_fak_sell, token_id, size)
            if filled > 0:
                logging.info(
                    "🔴 [LIVE] FAK SELL done: %.4f @ %.4f token=%s",
                    filled, price, token_id[:20],
                )
                return (filled, price)
            logging.error("🛑 [LIVE] FAK SELL failed: size=%.2f token=%s.", size, token_id[:20])
            return (0.0, 0.0)

        best_bid, _ = await asyncio.to_thread(self.get_best_prices, token_id)
        price = max(0.01, min(0.99, best_bid + 0.002))
        order_id, immediate = await asyncio.to_thread(
            self._place_limit_raw, token_id, SELL_SIDE, price, size
        )
        if not order_id:
            logging.warning("⚠️ [LIVE] SELL GTC failed, trying FAK token=%s.", token_id[:20])
            filled, fak_price = await asyncio.to_thread(self._place_fak_sell, token_id, size)
            if filled > 0:
                logging.info(
                    "🔴 [LIVE] FAK SELL done (GTC fallback): %.4f @ %.4f token=%s",
                    filled, fak_price, token_id[:20],
                )
                return (filled, fak_price)
            await self.emergency_exit(token_id, size, side=SELL_SIDE)
            return (0.0, 0.0)

        tracked = TrackedOrder(
            order_id=order_id,
            token_id=token_id,
            side=SELL_SIDE,
            price=price,
            size=size,
            status=OrderStatus.FILLED if immediate else OrderStatus.PENDING,
            filled_size=size if immediate else 0.0,
        )
        self._active_orders[order_id] = tracked
        logging.info(
            "🔴 [LIVE] SELL placed: %.4f @ %.4f id=%s immediate=%s token=%s",
            size, price, order_id[:20], immediate, token_id[:20],
        )
        if not immediate:
            await self._poll_order(tracked)

        total_filled = tracked.filled_size if tracked.filled_size > 0 else (
            tracked.size if tracked.status == OrderStatus.FILLED else 0.0
        )
        avg_price = tracked.price
        if total_filled > 0:
            logging.info(
                "🔴 [LIVE] SELL confirmed: filled=%.4f / %.4f @ %.4f token=%s",
                total_filled, size, avg_price, token_id[:20],
            )
        else:
            logging.error(
                "🛑 [LIVE] SELL not filled: size=%.4f @ %.4f token=%s",
                size, avg_price, token_id[:20],
            )
        return (total_filled, avg_price)

    async def execute(
        self,
        signal: str,
        token_id: str,
        order_size: float | None = None,
        budget_usd: float | None = None,
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
        """
        _SKIP = (0.0, 0.0)
        self._entry_stats["attempts"] += 1
        best_bid, best_ask = await asyncio.to_thread(self.get_best_prices, token_id)

        if best_ask <= self.min_entry_ask or best_ask >= self.max_entry_ask:
            self._entry_stats["skip_ask_cap"] += 1
            logging.warning(
                "Skip %s: best_ask %.4f outside allowed range [%.3f, %.3f].",
                signal, best_ask, self.min_entry_ask, self.max_entry_ask,
            )
            self._log_entry_stats_if_due()
            return _SKIP

        spread = best_ask - best_bid
        if spread <= 0 or spread > self.max_spread:
            self._entry_stats["skip_spread"] += 1
            logging.warning(
                "⚠️ Bad spread %.4f (bid=%.4f ask=%.4f max=%.4f), skip signal %s.",
                spread, best_bid, best_ask, self.max_spread, signal,
            )
            self._log_entry_stats_if_due()
            return _SKIP

        if signal not in ("BUY_UP", "BUY_DOWN"):
            self._entry_stats["skip_signal"] += 1
            logging.warning("Skip signal: unsupported live signal %s.", signal)
            self._log_entry_stats_if_due()
            return _SKIP

        poly_min_shares = float(os.getenv("POLY_CLOB_MIN_SHARES", "5"))

        exec_price = max(0.001, best_ask)
        usd_notional = order_size or budget_usd or self.min_order_size
        shares = usd_notional / exec_price

        if shares < poly_min_shares:
            min_cost = poly_min_shares * exec_price
            logging.warning(
                "⚠️ Skip %s: budget %.2f USD → %.2f shares < CLOB minimum %.0f shares "
                "(need %.2f USD @ %.4f). Insufficient balance.",
                signal, usd_notional, shares, poly_min_shares, min_cost, exec_price,
            )
            self._entry_stats["skip_signal"] += 1
            self._log_entry_stats_if_due()
            return _SKIP

        # Round down to 2 decimal places — CLOB rejects fractional shares beyond that.
        shares = float(int(shares * 100) / 100)
        if shares < poly_min_shares:
            shares = poly_min_shares

        # Place BUY at ask (or slightly above) for immediate fill.
        # Negative offset means we pay ask exactly; positive would cross the spread.
        _buy_offset = float(os.getenv("LIVE_BUY_PRICE_OFFSET", "0.0"))
        price = max(0.01, min(0.99, exec_price + _buy_offset))
        order_id, immediate = await asyncio.to_thread(
            self._place_limit_raw, token_id, BUY, price, shares
        )
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
        )
        self._active_orders[order_id] = tracked
        self._entry_stats["executed"] += 1
        logging.info(
            "🟢 [LIVE] BUY placed: %s %.2f sh @ %.4f (%.2f USD) token=%s id=%s immediate=%s",
            signal, shares, price, shares * price, token_id[:20], order_id[:20], immediate,
        )

        if not immediate:
            # Wait for _poll_order to confirm fill — run it as a task and await it.
            poll_task = asyncio.ensure_future(self._poll_order(tracked))
            await poll_task

        filled = tracked.filled_size if tracked.filled_size > 0 else (
            tracked.size if tracked.status == OrderStatus.FILLED else 0.0
        )
        avg_price = tracked.price

        self._log_entry_stats_if_due()

        # Order cancelled/failed — but a partial fill may have landed on-chain
        # (e.g. reprice rejected due to insufficient balance while first order
        # was already partially matched).  Check the actual CTF balance before
        # treating this as a skip to avoid phantom positions.
        if tracked.status in (OrderStatus.CANCELLED, OrderStatus.FAILED):
            _rescue_bal = await asyncio.to_thread(self.fetch_conditional_balance, token_id)
            if _rescue_bal and _rescue_bal >= float(os.getenv("POLY_CLOB_MIN_SHARES", "5")):
                logging.warning(
                    "⚠️ [LIVE] BUY order %s status=%s but on-chain balance=%.4f sh — "
                    "treating as partial fill to avoid phantom position.",
                    tracked.order_id[:20], tracked.status, _rescue_bal,
                )
                self._confirmed_buys[token_id] = _rescue_bal
                self._active_orders.pop(tracked.order_id, None)
                return (_rescue_bal, tracked.price)
            logging.warning(
                "⚠️ [LIVE] BUY order %s (status=%s, on-chain=%.4f) — skip.",
                tracked.order_id[:20], tracked.status, _rescue_bal or 0.0,
            )
            self._active_orders.pop(tracked.order_id, None)
            return _SKIP

        # Order was confirmed FILLED or PARTIAL — shares were actually received on-chain.
        # Wait for the CLOB ledger to settle before reading the balance (observed lag
        # up to ~600 ms for immediate fills).  We loop until the balance appears or we
        # exhaust all retries, then TRUST the CLOB-reported fill so we never abandon a
        # real position.
        if filled <= 0:
            logging.warning(
                "⚠️ [LIVE] BUY status=%s but filled=0 — skip.", tracked.status,
            )
            return _SKIP

        poly_min_shares = float(os.getenv("POLY_CLOB_MIN_SHARES", "5"))
        # Minimum fraction of the CLOB-reported fill that is accepted as a
        # "real" on-chain balance snapshot (not a partial ledger update).
        # If the on-chain read is < 10% of what CLOB reported, the ledger
        # has not settled yet and we continue polling rather than treating
        # the tiny value as the real post-fee balance.
        _bal_min_frac = float(os.getenv("LIVE_BALANCE_MIN_FRAC", "0.10"))
        _bal_delays = [0.3, 0.6, 1.0, 1.5, 2.0, 2.5]  # cumulative ~8.4 s max wait
        actual_bal: float | None = None
        for _i, _delay in enumerate(_bal_delays):
            await asyncio.sleep(_delay)
            _b = await asyncio.to_thread(self.fetch_conditional_balance, token_id)
            # Require balance >= 10% of CLOB-reported fill to accept as settled.
            if _b is not None and _b >= filled * _bal_min_frac:
                actual_bal = _b
                logging.info(
                    "🟢 [LIVE] On-chain balance confirmed: %.4f sh "
                    "(attempt %d, delay %.1fs) token=%s",
                    actual_bal, _i + 1, _delay, token_id[:20],
                )
                break
            next_delay = _bal_delays[_i + 1] if _i + 1 < len(_bal_delays) else 0
            logging.debug(
                "[LIVE] Balance %.4f < threshold %.4f on attempt %d "
                "— retrying in %.1fs token=%s",
                _b or 0.0, filled * _bal_min_frac,
                _i + 1, next_delay, token_id[:20],
            )

        if actual_bal is not None:
            if abs(actual_bal - filled) > 0.005:
                logging.warning(
                    "⚠️ [LIVE] BUY adjusted for protocol fee: reported=%.4f actual=%.4f "
                    "(fee=%.4f sh) token=%s",
                    filled, actual_bal, filled - actual_bal, token_id[:20],
                )
            filled = actual_bal
        else:
            # Ledger lag persists beyond all retries — use CLOB-reported fill.
            # The order WAS confirmed FILLED, so shares exist on-chain even if the
            # balance API hasn't caught up.  Proceeding prevents phantom positions.
            logging.warning(
                "⚠️ [LIVE] On-chain balance not confirmed after %d retries "
                "— trusting CLOB fill=%.4f token=%s",
                len(_bal_delays), filled, token_id[:20],
            )

        if filled < poly_min_shares:
            # Confirmed partial fill below CLOB minimum — can't use GTC to sell.
            # FAK-sell whatever arrived and treat as skip (no open position).
            logging.warning(
                "⚠️ [LIVE] Confirmed balance %.4f sh < min %.0f — "
                "FAK-selling residual, skipping. token=%s",
                filled, poly_min_shares, token_id[:20],
            )
            fak_filled = await self._fak_sell(token_id, filled)
            logging.info(
                "🔴 [LIVE] FAK residual exit: sold=%.4f token=%s",
                fak_filled, token_id[:20],
            )
            self._active_orders.pop(tracked.order_id, None)
            return _SKIP

        logging.info(
            "🟢 [LIVE] BUY confirmed: %.4f shares @ %.4f token=%s",
            filled, avg_price, token_id[:20],
        )
        # Persist fill so close_position can find shares even after _active_orders cleanup.
        self._confirmed_buys[token_id] = filled
        # Remove from active orders — immediate fills skip _poll_order so the dict
        # entry would otherwise accumulate and inflate active_orders counter.
        self._active_orders.pop(tracked.order_id, None)
        return (filled, avg_price)
