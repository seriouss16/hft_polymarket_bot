import logging
import time
from typing import Optional


def mark_price_for_side(book: dict, side: Optional[str]) -> float:
    """Return outcome token mid for YES or NO from YES outcome order book."""
    yes_bid = float(book.get("bid") or 0.0)
    yes_ask = float(book.get("ask") or 0.0)
    if side == "YES":
        mid = float(book.get("mid") or 0.0)
        if mid > 0.0:
            return mid
        if yes_bid > 0.0 and yes_ask > yes_bid:
            return (yes_bid + yes_ask) / 2.0
        return 0.0
    if side == "NO":
        no_bid = max(0.01, min(0.99, 1.0 - yes_ask))
        no_ask = max(0.01, min(0.99, 1.0 - yes_bid))
        return (no_bid + no_ask) / 2.0
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
        
        self.fee_rate = 0.001 # 0.1% (комиссия + среднее проскальзывание)
        self.last_realized_pnl = 0.0
        self.last_close_ts = 0.0

    def log_trade(self, side, price, amount_usd=100.0):
        if side in ("BUY", "BUY_YES", "BUY_NO"):
            if self.balance < amount_usd:
                return None
            
            exec_price = price * (1 + self.fee_rate) # Покупаем чуть дороже рынка
            new_shares = amount_usd / exec_price
            if self.inventory > 0:
                total_cost = self.entry_price * self.inventory + exec_price * new_shares
                self.inventory += new_shares
                self.entry_price = total_cost / self.inventory
            else:
                self.inventory = new_shares
                self.entry_price = exec_price
                self.position_side = "NO" if side == "BUY_NO" else "YES"
            self.balance -= amount_usd
            self.entry_ts = time.time()
            logging.info(
                "🟢 [SIM %s] Price: %.4f | Size: %.2f$ | Shares: %.4f | Avg: %.4f",
                side,
                exec_price,
                amount_usd,
                self.inventory,
                self.entry_price,
            )
            return {
                "event": "OPEN",
                "side": self.position_side,
                "price": exec_price,
                "amount_usd": amount_usd,
                "shares": self.inventory,
            }

        elif side == "SELL":
            if self.inventory <= 0:
                return None
            
            exec_price = price * (1 - self.fee_rate)
            revenue = self.inventory * exec_price
            profit = revenue - (self.inventory * self.entry_price)
            
            self.balance += revenue
            self.total_pnl += profit
            self.last_realized_pnl = profit
            self.last_close_ts = time.time()
            self.trades_count += 1
            if profit > 0: self.wins += 1
            
            # Расчет Drawdown
            if self.balance > self.peak_balance: self.peak_balance = self.balance
            dd = (self.peak_balance - self.balance) / self.peak_balance
            if dd > self.max_drawdown: self.max_drawdown = dd
            
            win_rate = (self.wins / self.trades_count) * 100
            logging.info(f"🔴 [SIM SELL] Price: {exec_price:.4f} | PnL: {profit:>+6.2f}$ | WR: {win_rate:.1f}% | Balance: {self.balance:.2f}$")
            
            self.inventory = 0.0
            self.entry_price = 0.0
            self.position_side = None
            return {
                "event": "CLOSE",
                "price": exec_price,
                "pnl": profit,
                "balance": self.balance,
            }
        return None

    def get_unrealized_pnl(self, book: dict) -> float:
        """Return mark-to-market PnL using the correct token mid (YES vs NO)."""
        if self.inventory <= 0 or not self.position_side:
            return 0.0
        mark = mark_price_for_side(book, self.position_side)
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