import logging
import os
import time
from collections import deque
from typing import Optional

from core.strategy_performance import StrategyPerformanceBook


def _up_outcome_quotes_ok(up_bid: float, up_ask: float) -> bool:
    """Return True when bid/ask look like UP-outcome token prices in (0, 1)."""
    return (
        0.0 < up_bid < 1.0
        and 0.0 < up_ask <= 1.0
        and up_ask > up_bid
        and (up_ask - up_bid) < 0.45
    )


def mark_price_for_side(book: dict, side: Optional[str]) -> float:
    """Return outcome token mid for UP or DOWN using explicit legs when present."""
    up_bid = float(book.get("bid") or 0.0)
    up_ask = float(book.get("ask") or 0.0)
    down_bid = float(book.get("down_bid") or 0.0)
    down_ask = float(book.get("down_ask") or 0.0)
    if side == "UP":
        if _up_outcome_quotes_ok(up_bid, up_ask):
            return (up_bid + up_ask) / 2.0
        m = float(book.get("mid") or 0.0)
        if 0.0 < m < 1.0:
            return m
        return 0.0
    if side == "DOWN":
        if 0.0 < down_bid < down_ask <= 1.0:
            return (down_bid + down_ask) / 2.0
        if _up_outcome_quotes_ok(up_bid, up_ask):
            d_bid = max(0.01, min(0.99, 1.0 - up_ask))
            d_ask = max(0.01, min(0.99, 1.0 - up_bid))
            return (d_bid + d_ask) / 2.0
        return 0.0
    return 0.0


def mark_bid_for_side(book: dict, side: Optional[str]) -> float:
    """Return conservative liquidation bid for the held outcome (long mark-to-market)."""
    up_bid = float(book.get("bid") or 0.0)
    up_ask = float(book.get("ask") or 0.0)
    down_bid = float(book.get("down_bid") or 0.0)
    down_ask = float(book.get("down_ask") or 0.0)
    if side == "UP":
        if _up_outcome_quotes_ok(up_bid, up_ask):
            return up_bid
        m = mark_price_for_side(book, side)
        return m
    if side == "DOWN":
        if 0.0 < down_bid < down_ask <= 1.0:
            return down_bid
        if _up_outcome_quotes_ok(up_bid, up_ask):
            return max(0.01, min(0.99, 1.0 - up_ask))
        return mark_price_for_side(book, side)
    return 0.0


class PnLTracker:
    """Track position state and realized/unrealized PnL on Polymarket shares."""

    def __init__(self, initial_balance=None):
        """Initialize balance from HFT_DEPOSIT_USD when initial_balance is omitted."""
        if initial_balance is not None:
            self.initial_balance = float(initial_balance)
        else:
            self.initial_balance = float(os.getenv("HFT_DEPOSIT_USD", "100.0"))
        self.balance = self.initial_balance
        self.inventory = 0.0
        self.entry_price = 0.0
        self.entry_ts = 0
        self.position_side = None

        self.trades_count = 0
        self.wins = 0
        self.total_pnl = 0.0
        self.max_drawdown = 0.0
        self.peak_balance = self.initial_balance

        self.fee_rate = float(os.getenv("HFT_SIM_FEE_RATE", "0.001"))
        self.last_realized_pnl = 0.0
        self.last_close_ts = 0.0
        self.trade_amount_usd = float(os.getenv("HFT_DEFAULT_TRADE_USD", "10.0"))

        self.recent_pnls = deque(
            maxlen=int(os.getenv("HFT_RECENT_TRADES_FOR_REGIME", "12"))
        )
        self.regime_cooldown_until = 0.0
        self._good_regime_winrate = float(os.getenv("HFT_GOOD_REGIME_WINRATE", "0.49"))
        self.strategy_performance = StrategyPerformanceBook()

    def reset_strategy_performance(self) -> None:
        """Clear per-strategy PnL buckets when starting a new market (optional)."""
        self.strategy_performance.reset()

    def is_good_regime(self) -> bool:
        """Return True when new entries are allowed based on recent realized PnL."""
        if time.time() < getattr(self, "regime_cooldown_until", 0.0):
            return False
        if len(self.recent_pnls) < 8:
            return True
        winrate = sum(1 for p in self.recent_pnls if p > 0) / len(self.recent_pnls)
        avg_pnl = sum(self.recent_pnls) / len(self.recent_pnls)
        return winrate >= self._good_regime_winrate or avg_pnl > -1.1

    def log_trade(
        self,
        side,
        price,
        amount_usd=None,
        settlement_fill=False,
        performance_key=None,
        strategy_name=None,
    ):
        """Record a simulated buy or sell; default notional matches HFT_DEFAULT_TRADE_USD when omitted.

        For SELL, performance_key (e.g. latency:latency or soft:soft_flow) attributes realized PnL to a bucket.
        Optional strategy_name labels SIM logs for attribution.
        """
        if amount_usd is None:
            amount_usd = self.trade_amount_usd
        if side in ("BUY", "BUY_UP", "BUY_DOWN"):
            if self.balance <= 0.0:
                logging.error(
                    "🛑 HALT: session balance is zero or negative (%.4f USD). "
                    "All entries blocked until manual restart.",
                    self.balance,
                )
                return None
            if self.balance < amount_usd:
                logging.warning(
                    "Balance %.2f USD is below trade notional %.2f USD — skipping entry.",
                    self.balance, amount_usd,
                )
                return None

            book_px = float(price)
            exec_price = book_px * (1 + self.fee_rate)
            new_shares = amount_usd / exec_price
            new_side = "DOWN" if side == "BUY_DOWN" else "UP"
            if self.inventory > 0:
                if self.position_side and self.position_side != new_side:
                    logging.warning(
                        "Mixed-side add blocked: held %s, attempted %s.",
                        self.position_side, new_side,
                    )
                    return None
                total_cost = self.entry_price * self.inventory + exec_price * new_shares
                self.inventory += new_shares
                self.entry_price = total_cost / self.inventory
            else:
                self.inventory = new_shares
                self.entry_price = exec_price
                self.position_side = new_side
            self.balance -= amount_usd
            self.entry_ts = time.time()
            _sn = str(strategy_name).strip() if strategy_name else ""
            _tag = f"{side} {_sn}".strip() if _sn else side
            logging.info(
                "🟢 [SIM %s] book=%.4f exec=%.4f | %0.2f$ → %0.4f sh (pos %0.4f @ avg %0.4f)",
                _tag,
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
            if settlement_fill:
                exec_price = book_px
            else:
                exec_price = book_px * (1 - self.fee_rate)
            cost_basis_usd = shares_sold * self.entry_price
            proceeds_usd = shares_sold * exec_price
            profit = proceeds_usd - cost_basis_usd

            self.balance += proceeds_usd
            if self.balance < 0.0:
                logging.error(
                    "🛑 ANOMALY: session balance went negative after SELL "
                    "(%.4f USD). proceeds=%.4f cost=%.4f. Clamping to 0.",
                    self.balance, proceeds_usd, cost_basis_usd,
                )
                self.balance = 0.0
            self.total_pnl += profit
            self.last_realized_pnl = profit
            self.last_close_ts = time.time()
            self.trades_count += 1
            if profit > 0:
                self.wins += 1

            self.recent_pnls.append(profit)
            if performance_key:
                self.strategy_performance.record_close(performance_key, profit)
            if len(self.recent_pnls) >= 6:
                winrate = sum(1 for p in self.recent_pnls if p > 0) / len(
                    self.recent_pnls
                )
                avg_pnl = sum(self.recent_pnls) / len(self.recent_pnls)
                bad_wr = float(os.getenv("HFT_BAD_REGIME_WINRATE", "0.48"))
                cooldown_sec = float(os.getenv("HFT_REGIME_COOLDOWN_SEC", "150"))
                if winrate < bad_wr or avg_pnl < -0.5:
                    self.regime_cooldown_until = time.time() + cooldown_sec
                    logging.warning(
                        "BAD REGIME detected: WR=%.1f%% avgPnL=%.2f$ -> cooldown %ss",
                        winrate * 100.0,
                        avg_pnl,
                        int(cooldown_sec),
                    )

            if self.balance > self.peak_balance:
                self.peak_balance = self.balance
            dd = (self.peak_balance - self.balance) / self.peak_balance
            if dd > self.max_drawdown:
                self.max_drawdown = dd

            win_rate = (self.wins / self.trades_count) * 100
            _sn = str(strategy_name).strip() if strategy_name else ""
            _sell_hdr = f"[SIM SELL {_sn}]" if _sn else "[SIM SELL]"
            logging.info(
                "🔴 %s book=%.4f exec=%.4f | sold %0.4f sh | cost %.2f$ → proceeds %.2f$ | PnL %+0.2f$ | WR %.1f%%",
                _sell_hdr,
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
                "performance_key": performance_key,
            }
        return None

    def get_unrealized_pnl(self, book: dict) -> float:
        """Return markPnL at the outcome bid (conservative exit)."""
        if self.inventory <= 0 or not self.position_side:
            return 0.0
        mark = mark_bid_for_side(book, self.position_side)
        if mark <= 0.0:
            logging.warning(
                "Unrealized PnL mark=0 for side=%s — book may be stale or empty.",
                self.position_side,
            )
            return 0.0
        return (mark - self.entry_price) * self.inventory

class RealExecutor:
    """Заглушка для реального API Polymarket."""

    def __init__(self, private_key):
        self.private_key = private_key

    async def place_order(self, side, token_id, price, amount):
        logging.info(f"🚀 [REAL TRADE] Sending {side} for {token_id} at {price}")
        return True
