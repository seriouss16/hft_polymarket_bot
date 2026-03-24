import logging
import time

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

    def log_trade(self, side, price, amount_usd=100.0):
        if side in ("BUY", "BUY_YES", "BUY_NO"):
            if self.balance < amount_usd: return
            
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

        elif side == "SELL":
            if self.inventory <= 0: return
            
            exec_price = price * (1 - self.fee_rate)
            revenue = self.inventory * exec_price
            profit = revenue - (self.inventory * self.entry_price)
            
            self.balance += revenue
            self.total_pnl += profit
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

    def get_unrealized_pnl(self, current_price: float) -> float:
        """Return mark-to-market PnL for open shares."""
        if self.inventory <= 0:
            return 0.0
        if self.position_side == "YES":
            return (current_price - self.entry_price) * self.inventory
        return (self.entry_price - current_price) * self.inventory

class RealExecutor:
    """Заглушка для реального API Polymarket."""
    def __init__(self, private_key):
        self.private_key = private_key
        # Тут будет инициализация Polymarket CLOB Client
    
    async def place_order(self, side, token_id, price, amount):
        logging.info(f"🚀 [REAL TRADE] Sending {side} for {token_id} at {price}")
        # Реальная отправка через SDK
        return True