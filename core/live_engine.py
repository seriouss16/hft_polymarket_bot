"""Live execution and risk controls for Polymarket CLOB."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

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
        self.client = None

        if self.test_mode:
            return

        if not private_key or not funder:
            raise ValueError("LIVE_MODE=1 requires PRIVATE_KEY and FUNDER env vars.")
        if ClobClient is None:
            raise RuntimeError("py_clob_client is not installed.")

        self.client = ClobClient(
            "https://clob.polymarket.com",
            key=private_key,
            chain_id=137,
            signature_type=1,
            funder=funder,
        )
        self.client.set_api_creds(self.client.create_or_derive_api_creds())

    def get_best_prices(self, token_id: str) -> tuple[float, float]:
        """Return best bid and best ask from CLOB order book."""
        if self.client is None:
            return 0.0, 1.0
        book = self.client.get_order_book(token_id)
        best_bid = float(book.bids[0].price) if book.bids else 0.0
        best_ask = float(book.asks[0].price) if book.asks else 1.0
        return best_bid, best_ask

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

    async def execute(self, signal: str, token_id: str) -> None:
        """Validate spread and place limit order for BUY_YES/BUY_NO."""
        best_bid, best_ask = await asyncio.to_thread(self.get_best_prices, token_id)
        spread = best_ask - best_bid
        if spread <= 0 or spread > self.max_spread:
            logging.warning("⚠️ Bad spread %.4f, skip signal %s.", spread, signal)
            return

        size = self.min_order_size
        if signal == "BUY_YES":
            price = max(0.01, min(0.99, best_ask - 0.002))
            await asyncio.to_thread(self._place_limit, token_id, BUY, price, size)
        elif signal == "BUY_NO":
            no_ask = 1.0 - best_bid
            price = max(0.01, min(0.99, no_ask - 0.002))
            await asyncio.to_thread(self._place_limit, token_id, BUY, price, size)

