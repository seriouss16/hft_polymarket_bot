import logging
import os
import time
from typing import Optional


def _yes_outcome_quotes_ok(yes_bid: float, yes_ask: float) -> bool:
    """Return True when bid/ask look like YES token prices in (0, 1)."""
    return (
        0.0 < yes_bid < 1.0
        and 0.0 < yes_ask <= 1.0
        and yes_ask > yes_bid
        and (yes_ask - yes_bid) < 0.45
    )


def mark_price_for_side(book: dict, side: Optional[str]) -> float:
    """Return outcome token mid for UP or DOWN using explicit legs when present."""
    yes_bid = float(book.get("bid") or 0.0)
    yes_ask = float(book.get("ask") or 0.0)
    down_bid = float(book.get("down_bid") or 0.0)
    down_ask = float(book.get("down_ask") or 0.0)
    if side in ("UP", "YES"):
        if _yes_outcome_quotes_ok(yes_bid, yes_ask):
            return (yes_bid + yes_ask) / 2.0
        m = float(book.get("mid") or 0.0)
        if 0.0 < m < 1.0:
            return m
        return 0.0
    if side in ("DOWN", "NO"):
        if 0.0 < down_bid < down_ask <= 1.0:
            return (down_bid + down_ask) / 2.0
        if _yes_outcome_quotes_ok(yes_bid, yes_ask):
            no_bid = max(0.01, min(0.99, 1.0 - yes_ask))
            no_ask = max(0.01, min(0.99, 1.0 - yes_bid))
            return (no_bid + no_ask) / 2.0
        return 0.0
    return 0.0


def mark_bid_for_side(book: dict, side: Optional[str]) -> float:
    """Return conservative liquidation bid for the held outcome (long mark-to-market)."""
    yes_bid = float(book.get("bid") or 0.0)
    yes_ask = float(book.get("ask") or 0.0)
    down_bid = float(book.get("down_bid") or 0.0)
    down_ask = float(book.get("down_ask") or 0.0)
    if side in ("UP", "YES"):
        if _yes_outcome_quotes_ok(yes_bid, yes_ask):
            return yes_bid
        m = mark_price_for_side(book, side)
        return m
    if side in ("DOWN", "NO"):
        if 0.0 < down_bid < down_ask <= 1.0:
            return down_bid
        if _yes_outcome_quotes_ok(yes_bid, yes_ask):
            return max(0.01, min(0.99, 1.0 - yes_ask))
        return mark_price_for_side(book, side)
    return 0.0


class PnLTracker:
    """Track position state and realized/unrealized PnL on Polymarket shares."""

    def __init__(self, initial_balance=1000.0):
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.inventory = 0.0  # Кол-во токенов (YES или NO)
        self.entry_price = 0.0
        self.entry_ts = 0
        self.position_side = None
        
        # Метрики
        self.trades_count = 0
        self.wins = 0
        self.total_pnl = 0.0
        self.max_drawdown = 0.0
        self.peak_balance = initial_balance
        
        self.fee_rate = float(os.getenv("HFT_SIM_FEE_RATE", "0.001"))
        self.last_realized_pnl = 0.0
        self.last_close_ts = 0.0
        self.trade_amount_usd = float(os.getenv("HFT_DEFAULT_TRADE_USD", "100.0"))

    def log_trade(self, side, price, amount_usd=None):
        """Record a simulated buy or sell; default notional matches HFT_DEFAULT_TRADE_USD when omitted."""
        if amount_usd is None:
            amount_usd = self.trade_amount_usd
        if side in ("BUY", "BUY_YES", "BUY_NO", "BUY_UP", "BUY_DOWN"):
            if self.balance < amount_usd:
                return None

            book_px = float(price)
            exec_price = book_px * (1 + self.fee_rate)
            new_shares = amount_usd / exec_price
            if self.inventory > 0:
                total_cost = self.entry_price * self.inventory + exec_price * new_shares
                self.inventory += new_shares
                self.entry_price = total_cost / self.inventory
            else:
                self.inventory = new_shares
                self.entry_price = exec_price
                self.position_side = "DOWN" if side in ("BUY_NO", "BUY_DOWN") else "UP"
            self.balance -= amount_usd
            self.entry_ts = time.time()
            logging.info(
                "🟢 [SIM %s] book=%.4f exec=%.4f | %0.2f$ → %0.4f sh (pos %0.4f @ avg %0.4f)",
                side,
                book_px,
                exec_price,
                amount_usd,
                new_shares,
                self.inventory,
                self.entry_price,
            )
            return {
                "event": "OPEN",
                "side": self.position_side,
                "book_px": book_px,
                "exec_px": exec_price,
                "amount_usd": amount_usd,
                "shares_filled": new_shares,
                "shares_position": self.inventory,
                "price": exec_price,
                "shares": self.inventory,
            }

        elif side == "SELL":
            if self.inventory <= 0:
                return None

            book_px = float(price)
            shares_sold = float(self.inventory)
            exec_price = book_px * (1 - self.fee_rate)
            cost_basis_usd = shares_sold * self.entry_price
            proceeds_usd = shares_sold * exec_price
            profit = proceeds_usd - cost_basis_usd

            self.balance += proceeds_usd
            self.total_pnl += profit
            self.last_realized_pnl = profit
            self.last_close_ts = time.time()
            self.trades_count += 1
            if profit > 0:
                self.wins += 1

            if self.balance > self.peak_balance:
                self.peak_balance = self.balance
            dd = (self.peak_balance - self.balance) / self.peak_balance
            if dd > self.max_drawdown:
                self.max_drawdown = dd

            win_rate = (self.wins / self.trades_count) * 100
            logging.info(
                "🔴 [SIM SELL] book=%.4f exec=%.4f | sold %0.4f sh | cost %.2f$ → proceeds %.2f$ | PnL %+0.2f$ | WR %.1f%%",
                book_px,
                exec_price,
                shares_sold,
                cost_basis_usd,
                proceeds_usd,
                profit,
                win_rate,
            )

            self.inventory = 0.0
            self.entry_price = 0.0
            self.position_side = None
            return {
                "event": "CLOSE",
                "book_px": book_px,
                "exec_px": exec_price,
                "shares_sold": shares_sold,
                "cost_basis_usd": cost_basis_usd,
                "proceeds_usd": proceeds_usd,
                "pnl": profit,
                "balance": self.balance,
                "price": exec_price,
            }
        return None

    def get_unrealized_pnl(self, book: dict) -> float:
        """Return markPnL at the outcome bid (conservative exit)."""
        if self.inventory <= 0 or not self.position_side:
            return 0.0
        mark = mark_bid_for_side(book, self.position_side)
        if mark <= 0.0:
            return 0.0
        return (mark - self.entry_price) * self.inventory

class RealExecutor:
    """Заглушка для реального API Polymarket."""
    def __init__(self, private_key):
        self.private_key = private_key
        # Тут будет инициализация Polymarket CLOB Client
    
    async def place_order(self, side, token_id, price, amount):
        logging.info(f"🚀 [REAL TRADE] Sending {side} for {token_id} at {price}")
        # Реальная отправка через SDK
        return True