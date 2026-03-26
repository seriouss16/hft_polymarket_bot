"""Live execution and risk controls for Polymarket CLOB."""


from __future__ import annotations

import asyncio
import logging
import os
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
    from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY, SELL
except Exception:  # pragma: no cover - optional runtime dependency
    ClobClient = None
    ApiCreds = None
    OrderArgs = None
    OrderType = None
    BUY = "BUY"
    SELL = "SELL"


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
        api_key: str | None = None,
        api_secret: str | None = None,
        api_passphrase: str | None = None,
        test_mode: bool = True,
        min_order_size: float = 10.0,
        max_spread: float = 0.03,
    ) -> None:
        self.test_mode = test_mode
        self.min_order_size = min_order_size
        self.max_spread = max_spread
        self.max_entry_ask = float(os.getenv("HFT_MAX_ENTRY_ASK", "0.99"))
        self.min_entry_ask = float(os.getenv("HFT_MIN_ENTRY_ASK", "0.08"))
        self.default_trade_usd = float(os.getenv("HFT_DEFAULT_TRADE_USD", str(min_order_size)))
        self.min_entry_shares = float(os.getenv("HFT_MIN_ENTRY_SHARES", "6.0"))
        self.min_exit_shares = float(os.getenv("HFT_MIN_EXIT_SHARES", "5.0"))
        self.share_fee_buffer = float(os.getenv("HFT_SHARE_FEE_BUFFER", "0.2"))
        self.enforce_collateral_check = os.getenv("HFT_ENFORCE_COLLATERAL_CHECK", "1") == "1"
        self.client = None
        self._http = requests.Session()

        if ClobClient is None:
            if not self.test_mode:
                raise RuntimeError("py_clob_client is not installed.")
            return

        # Public market-data client is available in both SIM and LIVE.
        signature_type = int(
            os.getenv("HFT_CLOB_SIGNATURE_TYPE")
            or os.getenv("POLY_SIGNATURE_TYPE")
            or "1"
        )
        self.client = ClobClient(
            "https://clob.polymarket.com",
            key=private_key or "",
            chain_id=137,
            signature_type=signature_type,
            funder=funder or "",
        )
        if not self.test_mode:
            if not private_key or not funder:
                raise ValueError("LIVE_MODE=1 requires PRIVATE_KEY and FUNDER env vars.")
            if api_key and api_secret and api_passphrase:
                try:
                    self.client.set_api_creds(
                        ApiCreds(
                            api_key=api_key,
                            api_secret=api_secret,
                            api_passphrase=api_passphrase,
                        )
                    )
                    logging.info("Using explicit Polymarket CLOB API credentials from environment.")
                except Exception as exc:
                    logging.warning(
                        "Explicit CLOB API credentials failed (%s); falling back to derived API creds.",
                        exc,
                    )
                    self.client.set_api_creds(self.client.create_or_derive_api_creds())
            else:
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

    def _place_limit(self, token_id: str, side: str, price: float, size: float) -> bool:
        """Send one GTC limit order or print it in simulation mode."""
        if self.test_mode:
            logging.info("[SIM LIMIT] %s size=%.2f @ %.4f token=%s", side, size, price, token_id)
            return True
        order = OrderArgs(
            token_id=token_id,
            price=price,
            size=size,
            side=side,
        )
        try:
            signed = self.client.create_order(order)
            resp = self.client.post_order(signed, OrderType.GTC)
            logging.info("[LIVE] %s size=%.2f @ %.4f token=%s -> %s", side, size, price, token_id, resp)
            return True
        except Exception as exc:
            logging.error(
                "Order placement failed: side=%s size=%.4f price=%.4f token=%s error=%s",
                side,
                size,
                price,
                token_id,
                exc,
            )
            return False

    @staticmethod
    def _to_float(value: object) -> float | None:
        """Convert API payload value to float when possible."""
        try:
            if value is None:
                return None
            return float(value)
        except (TypeError, ValueError):
            return None

    def _extract_collateral_available(self, payload: object) -> float | None:
        """Try to extract available collateral from CLOB allowance payload."""
        if payload is None:
            return None
        if isinstance(payload, dict):
            candidates = (
                payload.get("available"),
                payload.get("balance"),
                payload.get("allowance"),
                payload.get("available_balance"),
                payload.get("availableBalance"),
            )
            for item in candidates:
                val = self._to_float(item)
                if val is not None:
                    return val
            nested = payload.get("response")
            if isinstance(nested, dict):
                return self._extract_collateral_available(nested)
            return None
        for attr in ("available", "balance", "allowance", "available_balance", "availableBalance"):
            val = self._to_float(getattr(payload, attr, None))
            if val is not None:
                return val
        return None

    def _get_available_collateral(self) -> float | None:
        """Return available collateral from CLOB API when method is supported."""
        if self.client is None or not hasattr(self.client, "get_balance_allowance"):
            return None
        method = getattr(self.client, "get_balance_allowance")
        payload = None
        try:
            from py_clob_client.clob_types import AssetType, BalanceAllowanceParams

            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            payload = method(params=params)
        except Exception:
            try:
                payload = method()
            except Exception:
                return None
        return self._extract_collateral_available(payload)

    def _resolve_entry_size(self, best_ask: float) -> tuple[float, float] | None:
        """Pick entry size: at least exchange min shares, up to default trade USD budget."""
        if best_ask <= 0.0:
            return None

        required_shares = max(
            self.min_entry_shares,
            self.min_exit_shares + self.share_fee_buffer,
        )

        max_shares_by_budget = self.default_trade_usd / best_ask

        shares = min(max_shares_by_budget, max(required_shares, self.min_order_size))

        if shares < required_shares:
            shares = required_shares

        notional = shares * best_ask

        if notional > self.default_trade_usd + 0.5:
            logging.warning(
                "Entry size capped by budget: shares=%.3f notional=%.2f (budget=%s)",
                shares,
                notional,
                self.default_trade_usd,
            )

        return shares, notional

    async def execute(self, signal: str, token_id: str) -> bool:
        """Validate ask band and place limit order for BUY_UP/BUY_DOWN (no spread gate)."""
        _, best_ask = await asyncio.to_thread(self.get_best_prices, token_id)
        if best_ask >= self.max_entry_ask or best_ask < self.min_entry_ask:
            logging.warning(
                "Skip %s: best_ask %.4f outside entry ask band [%.4f, %.4f).",
                signal,
                best_ask,
                self.min_entry_ask,
                self.max_entry_ask,
            )
            return False
        resolved_size = self._resolve_entry_size(best_ask)
        if resolved_size is None:
            return False
        size, notional = resolved_size
        if self.enforce_collateral_check and not self.test_mode:
            available = await asyncio.to_thread(self._get_available_collateral)
            if available is not None and available + 1e-9 < notional:
                logging.warning(
                    "Skip entry: insufficient collateral available=%.4f required=%.4f (includes open orders).",
                    available,
                    notional,
                )
                return False
        if signal == "BUY_UP":
            price = max(0.01, min(0.99, best_ask - 0.002))
            return await asyncio.to_thread(self._place_limit, token_id, BUY, price, size)
        elif signal == "BUY_DOWN":
            price = max(0.01, min(0.99, best_ask - 0.002))
            return await asyncio.to_thread(self._place_limit, token_id, BUY, price, size)
        return False

    async def close_position(self, token_id: str, size: float) -> bool:
        """Close a previously opened outcome position by selling to the bid."""
        if size <= 0:
            return False
        if size + self.share_fee_buffer + 1e-9 < self.min_exit_shares:
            logging.warning(
                "Skip close: size %.3f with fee buffer %.3f below min exit shares %.3f.",
                size,
                self.share_fee_buffer,
                self.min_exit_shares,
            )
            return False
        best_bid, _ = await asyncio.to_thread(self.get_best_prices, token_id)
        price = max(0.01, min(0.99, best_bid + 0.001))
        return await asyncio.to_thread(self._place_limit, token_id, SELL, price, size)

