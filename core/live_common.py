"""Shared live CLOB constants, order types, book parsing, and paper-alignment helpers.

Used by :mod:`core.live_engine` so ``LiveExecutionEngine`` stays a thinner facade.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from utils.env_config import req_float, req_int, req_str
from utils.env_merge import merge_env_file

# Ensure config/runtime.env is merged before reading LIVE_* (tests import live_engine
# without loading bot.py first).
_ROOT = Path(__file__).resolve().parent.parent
merge_env_file(_ROOT / "config" / "runtime.env", overwrite=False)

CLOB_BOOK_HTTP = req_str("CLOB_BOOK_HTTP")
_CLOB_BOOK_HTTP_TIMEOUT = req_float("LIVE_CLOB_BOOK_HTTP_TIMEOUT")

_ORDER_FILL_POLL_SEC = req_float("LIVE_ORDER_FILL_POLL_SEC")
_ORDER_STALE_SEC = req_float("LIVE_ORDER_STALE_SEC")
_ORDER_MAX_REPRICE = req_int("LIVE_ORDER_MAX_REPRICE")
_ORDER_EMERGENCY_TICKS = req_int("LIVE_ORDER_EMERGENCY_TICKS")
_REPRICE_POST_CANCEL_SLEEP_SEC = req_float("LIVE_REPRICE_POST_CANCEL_SLEEP_SEC")
_REPRICE_POST_CANCEL_FILL_POLLS = max(1, req_int("LIVE_REPRICE_POST_CANCEL_FILL_POLLS"))
_REPRICE_POST_CANCEL_POLL_SEC = req_float("LIVE_REPRICE_POST_CANCEL_POLL_SEC")

if _ORDER_MAX_REPRICE == 0:
    logging.warning(
        "LIVE_ORDER_MAX_REPRICE=0: stale orders are not repriced; the first stale hit "
        "becomes emergency after LIVE_ORDER_STALE_SEC (~%.1fs). Set >0 to allow chase.",
        _ORDER_STALE_SEC,
    )


def _parse_csv_floats(raw: str) -> list[float]:
    """Parse comma-separated floats (``LIVE_BALANCE_CONFIRM_DELAYS_SEC``)."""
    out: list[float] = []
    for part in str(raw).split(","):
        p = part.strip()
        if p:
            out.append(float(p))
    if not out:
        raise RuntimeError("Comma-separated float list is empty.")
    return out


def _parse_usdc_verify_delays() -> list[float]:
    """Delays (seconds) before each post-BUY USDC balance poll."""
    raw = os.getenv("LIVE_USDC_DEBIT_VERIFY_DELAYS_SEC", "0,0.2,0.45,0.9,1.5")
    parts = [p.strip() for p in str(raw).split(",") if p.strip()]
    if not parts:
        return [0.0]
    return [float(p) for p in parts]


def _collateral_usd_from_balance_allowance_response(resp: object) -> float | None:
    """Parse ``GET /balance-allowance`` for ``asset_type=COLLATERAL`` (free USDC).

    Polymarket returns ``balance`` (1e-6 USDC, matches UI Cash) and separately
    ``allowance`` (ERC20 approval to the exchange — not spendable cash). Using
    ``balance or allowance`` is wrong: when ``balance`` is ``0``, Python treats it
    as falsy and would substitute a huge allowance — the UI would look «stuck».
    """
    if isinstance(resp, dict):
        if "balance" in resp and resp["balance"] is not None:
            return float(resp["balance"]) / 1_000_000.0
        logging.warning(
            "fetch_usdc_balance: COLLATERAL response missing balance key: %s",
            resp,
        )
        return None
    bal = getattr(resp, "balance", None)
    if bal is not None:
        return float(bal) / 1_000_000.0
    logging.warning(
        "fetch_usdc_balance: COLLATERAL response object has no balance attr: %s",
        resp,
    )
    return None


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
    entry_best_ask: float | None = None

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


def _env_float_inactive0(key: str) -> float:
    """Parse env float; missing/empty → 0.0 so optional caps stay inactive (matches engine)."""
    v = os.getenv(key)
    if v is None or not str(v).strip():
        return 0.0
    return float(v)


def _paper_aligned_outcome_ask_ok(ask: float, min_cap: float, max_cap: float) -> bool:
    """Match ``HFTEngine._entry_outcome_price_allows`` / per-outcome min–max bands."""
    ask_val = float(ask)
    min_active = 0.0 < float(min_cap) < 1.0
    max_active = 0.0 < float(max_cap) < 1.0
    if min_active and ask_val < float(min_cap):
        return False
    if max_active and ask_val > float(max_cap):
        return False
    return True


def _paper_aligned_buy_price_allows(signal: str, best_ask: float, max_entry_ask: float) -> bool:
    """Match paper OPEN gates: global ``_entry_ask_allows_open`` + per-outcome caps.

    Live previously used a single ``[HFT_MIN_ENTRY_ASK, HFT_MAX_ENTRY_ASK]`` window for
    both outcome tokens. UP and DOWN tokens have different typical price levels; the
    same global band skewed fills toward DOWN-only. See ``HFTEngine`` OPEN conditions.
    """
    if float(best_ask) >= float(max_entry_ask):
        return False
    if signal == "BUY_UP":
        return _paper_aligned_outcome_ask_ok(
            best_ask,
            _env_float_inactive0("HFT_ENTRY_MIN_ASK_UP"),
            _env_float_inactive0("HFT_ENTRY_MAX_ASK_UP"),
        )
    if signal == "BUY_DOWN":
        return _paper_aligned_outcome_ask_ok(
            best_ask,
            _env_float_inactive0("HFT_ENTRY_MIN_ASK_DOWN"),
            _env_float_inactive0("HFT_ENTRY_MAX_ASK_DOWN"),
        )
    return False


@dataclass
class LiveRiskManager:
    """Session realized-PnL guard and trade counter (bot process lifetime)."""

    max_session_loss: float = -50.0
    pnl: float = 0.0
    trades: int = 0

    def update(self, pnl_change: float) -> None:
        """Accumulate realized pnl and number of trades."""
        self.pnl += pnl_change
        self.trades += 1

    def session_loss_breached(self) -> bool:
        """True when realized session PnL is at or beyond the configured loss cap.

        For a negative limit (e.g. -50 USD), this is True when ``pnl <= limit``.
        """
        if self.max_session_loss < 0.0:
            return self.pnl <= self.max_session_loss
        return self.pnl < self.max_session_loss

    def can_trade(self) -> bool:
        """Return False when session loss limit is breached (no new entries)."""
        return not self.session_loss_breached()

    def log_status(self) -> None:
        """Log current risk state for diagnostics."""
        logging.info(
            "[LIVE RISK] session_pnl=%.4f max_session_loss=%.4f trades=%d can_trade=%s",
            self.pnl, self.max_session_loss, self.trades, self.can_trade(),
        )
