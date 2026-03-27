"""Live execution and risk controls for Polymarket CLOB."""


from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

import requests

CLOB_BOOK_HTTP = "https://clob.polymarket.com/book"


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
    from py_clob_client.order_builder.constants import BUY
except Exception:  # pragma: no cover - optional runtime dependency
    ClobClient = None
    OrderArgs = None
    OrderType = None
    BUY = "BUY"


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
    """Place safe limit orders against Polymarket CLOB."""

    def __init__(
        self,
        private_key: str | None,
        funder: str | None,
        test_mode: bool = True,
        min_order_size: float = 10.0,
        max_spread: float = 0.03,
    ) -> None:
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
        }
        self.client = None
        self._http = requests.Session()

        if ClobClient is None:
            if not self.test_mode:
                raise RuntimeError("py_clob_client is not installed.")
            return

        sig_type = int(os.getenv("POLY_SIGNATURE_TYPE", "2"))
        # Public market-data client is available in both SIM and LIVE.
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
            self.client.set_api_creds(self.client.create_or_derive_api_creds())

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

    def _place_limit(self, token_id: str, side: str, price: float, size: float) -> None:
        """Send one GTC limit order or print it in simulation mode."""
        if self.test_mode:
            logging.info("[SIM LIMIT] %s size=%.2f @ %.4f token=%s", side, size, price, token_id)
            return
        order = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        signed = self.client.create_order(order)
        resp = self.client.post_order(signed, OrderType.GTC)
        logging.info("[LIVE] %s size=%.2f @ %.4f token=%s -> %s", side, size, price, token_id, resp)

    def _log_entry_stats_if_due(self) -> None:
        """Emit aggregated live entry stats periodically for gate diagnostics."""
        if self.skip_stats_log_sec <= 0:
            return
        now = time.time()
        if now - self._last_skip_stats_log_ts < self.skip_stats_log_sec:
            return
        st = self._entry_stats
        logging.info(
            "Live entry stats: attempts=%s executed=%s skip_ask_cap=%s skip_spread=%s skip_signal=%s.",
            st["attempts"],
            st["executed"],
            st["skip_ask_cap"],
            st["skip_spread"],
            st["skip_signal"],
        )
        self._last_skip_stats_log_ts = now

    def _place_sell(self, token_id: str, price: float, size: float) -> None:
        """Send GTC SELL limit or print it in simulation mode."""
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType
            from py_clob_client.order_builder.constants import SELL as SELL_SIDE
        except Exception:
            SELL_SIDE = "SELL"
            OrderArgs = None
            OrderType = None

        if self.test_mode:
            logging.info("[SIM LIMIT] SELL size=%.2f @ %.4f token=%s", size, price, token_id)
            return
        if OrderArgs is None or self.client is None:
            logging.warning("Cannot place SELL: py_clob_client unavailable.")
            return
        order = OrderArgs(token_id=token_id, price=price, size=size, side=SELL_SIDE)
        signed = self.client.create_order(order)
        resp = self.client.post_order(signed, OrderType.GTC)
        logging.info("[LIVE] SELL size=%.2f @ %.4f token=%s -> %s", size, price, token_id, resp)

    async def close_position(self, token_id: str, size: float) -> None:
        """Place a SELL limit order to close an open long position."""
        if size <= 0:
            return
        best_bid, _ = await asyncio.to_thread(self.get_best_prices, token_id)
        price = max(0.01, min(0.99, best_bid + 0.002))
        await asyncio.to_thread(self._place_sell, token_id, price, size)
        logging.info("[LIVE] Close position: SELL %.2f sh @ %.4f token=%s", size, price, token_id)

    async def execute(self, signal: str, token_id: str) -> None:
        """Validate spread and place limit order for BUY_UP/BUY_DOWN."""
        self._entry_stats["attempts"] += 1
        best_bid, best_ask = await asyncio.to_thread(self.get_best_prices, token_id)
        if best_ask >= self.max_entry_ask:
            self._entry_stats["skip_ask_cap"] += 1
            logging.warning(
                "Skip %s: best_ask %.4f >= max entry ask %.4f.",
                signal,
                best_ask,
                self.max_entry_ask,
            )
            self._log_entry_stats_if_due()
            return
        spread = best_ask - best_bid
        if spread <= 0 or spread > self.max_spread:
            self._entry_stats["skip_spread"] += 1
            logging.warning("⚠️ Bad spread %.4f, skip signal %s.", spread, signal)
            self._log_entry_stats_if_due()
            return

        size = self.min_order_size
        if signal in ("BUY_UP", "BUY_DOWN"):
            price = max(0.01, min(0.99, best_ask - 0.002))
            await asyncio.to_thread(self._place_limit, token_id, BUY, price, size)
            self._entry_stats["executed"] += 1
        else:
            self._entry_stats["skip_signal"] += 1
            logging.warning("Skip signal: unsupported live signal %s.", signal)
        self._log_entry_stats_if_due()

