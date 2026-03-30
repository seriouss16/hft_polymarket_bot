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

    def __init__(self, initial_balance=None, live_mode: bool = False):
        """Initialize balance from HFT_DEPOSIT_USD when initial_balance is omitted."""
        if initial_balance is not None:
            self.initial_balance = float(initial_balance)
        else:
            self.initial_balance = float(os.getenv("HFT_DEPOSIT_USD"))
        self.live_mode = live_mode
        # When True, log_trade(BUY) is a no-op — position is written only via
        # live_open() after CLOB confirms the fill.
        self._suppress_buy = live_mode
        self.balance = self.initial_balance
        self.inventory = 0.0
        self.entry_price = 0.0
        self.entry_ts = 0
        self.position_side = None
        # Actual USD debited from the account for the current open position
        # (may differ from entry_price * inventory due to protocol fees at buy time).
        self._buy_cost_usd: float = 0.0

        self.trades_count = 0
        self.wins = 0
        self.total_pnl = 0.0
        # Realized PnL per closed round-trip this process (sim + live); used for session median stats.
        self.closed_trade_pnls: list[float] = []
        self.max_drawdown = 0.0
        self.peak_balance = self.initial_balance

        self.fee_rate = float(os.getenv("HFT_SIM_FEE_RATE"))
        self.last_realized_pnl = 0.0
        self.last_close_ts = 0.0
        self.trade_amount_usd = float(os.getenv("HFT_DEFAULT_TRADE_USD"))

        self.recent_pnls = deque(
            maxlen=int(os.getenv("HFT_RECENT_TRADES_FOR_REGIME"))
        )
        self.regime_cooldown_until = 0.0
        self._good_regime_winrate = float(os.getenv("HFT_GOOD_REGIME_WINRATE"))
        self.strategy_performance = StrategyPerformanceBook()

    def reset_strategy_performance(self) -> None:
        """Clear per-strategy PnL buckets when starting a new market (optional)."""
        self.strategy_performance.reset()

    def rollback_last_open(self, amount_usd: float) -> None:
        """Undo the last BUY that was sim-recorded but rejected by the live CLOB.

        Restores balance and clears inventory so the engine does not attempt to
        close a position that was never opened on-chain.  Only call immediately
        after an OPEN decision when live execution returned False (SKIP).
        No-op when inventory is already zero (prevents double-restoration).
        """
        if self.inventory <= 0.0:
            logging.debug(
                "[LIVE] rollback_last_open called with no open position — skipped.",
            )
            return
        self.balance += amount_usd
        self.inventory = 0.0
        self.entry_price = 0.0
        self.entry_ts = 0
        self.position_side = None
        self._buy_cost_usd = 0.0
        logging.info(
            "[LIVE] Sim OPEN rolled back — live BUY skipped. Balance restored to %.4f USD.",
            self.balance,
        )

    def live_open(
        self,
        side: str,
        filled_shares: float,
        avg_price: float,
        amount_usd: float,
        strategy_name: str = "",
    ) -> None:
        """Record a confirmed live BUY fill directly into PnL state.

        Called only after CLOB confirms the fill — bypasses all sim-mode balance
        checks and uses real CLOB fill data instead of simulated exec_price.
        Does nothing when filled_shares is zero (order was not filled).
        """
        if filled_shares <= 0.0:
            return
        new_side = "DOWN" if side == "BUY_DOWN" else "UP"
        if self.inventory > 0:
            if self.position_side and self.position_side != new_side:
                logging.warning(
                    "Mixed-side live_open blocked: held %s, attempted %s.",
                    self.position_side, new_side,
                )
                return
            total_cost = self.entry_price * self.inventory + avg_price * filled_shares
            self.inventory += filled_shares
            self.entry_price = total_cost / self.inventory
        else:
            self.inventory = filled_shares
            self.entry_price = avg_price
            self.position_side = new_side
        self.balance -= amount_usd
        self._buy_cost_usd += amount_usd
        self.entry_ts = time.time()
        _tag = f"{side} {strategy_name}".strip() if strategy_name else side
        logging.info(
            "🟢 [LIVE %s] filled=%.4f sh @ avg %.4f | cost %.2f$ (pos %.4f @ avg %.4f)",
            _tag, filled_shares, avg_price, amount_usd, self.inventory, self.entry_price,
        )

    def live_close(
        self,
        filled_shares: float,
        avg_price: float,
        strategy_name: str = "",
        performance_key: str | None = None,
    ) -> float:
        """Record a confirmed live SELL fill and return realized PnL USD.

        Called only after CLOB confirms the sell fill.  Updates all PnL counters
        using real fill data.  Returns 0.0 when there is no open position.
        """
        if self.inventory <= 0.0 or filled_shares <= 0.0:
            return 0.0
        inv_before = self.inventory
        proceeds = filled_shares * avg_price
        cost_basis = self.entry_price * filled_shares
        # Prefer cash PnL vs proportional buy cost so realized PnL matches Polymarket
        # when amount_usd reflects actual debits (emergency fills, protocol fee in shares).
        alloc_buy_usd = 0.0
        if self._buy_cost_usd > 0.0 and inv_before > 0.0:
            alloc_buy_usd = self._buy_cost_usd * (filled_shares / inv_before)
            pnl = proceeds - alloc_buy_usd
        else:
            pnl = proceeds - cost_basis
        net_pnl = pnl
        self.balance += proceeds
        self._buy_cost_usd = max(0.0, self._buy_cost_usd - alloc_buy_usd)
        self.inventory = max(0.0, self.inventory - filled_shares)
        _dust = float(os.getenv("LIVE_INVENTORY_DUST_SHARES"))
        if self.inventory <= _dust:
            self.inventory = 0.0
        if self.inventory <= 0.0:
            self.entry_price = 0.0
            self.entry_ts = 0
            self.position_side = None
        self.total_pnl += pnl
        self.last_realized_pnl = pnl
        self.last_close_ts = time.time()
        self.trades_count += 1
        self.closed_trade_pnls.append(pnl)
        if pnl > 0:
            self.wins += 1
        self.recent_pnls.append(pnl)
        if self.balance > self.peak_balance:
            self.peak_balance = self.balance
        dd = (self.peak_balance - self.balance) / self.peak_balance if self.peak_balance > 0 else 0.0
        if dd > self.max_drawdown:
            self.max_drawdown = dd
        if performance_key:
            self.strategy_performance.record_close(str(performance_key), pnl)
        wr = self.wins / self.trades_count * 100 if self.trades_count else 0.0
        _tag = f"{strategy_name}".strip() if strategy_name else ""
        logging.info(
            "🔴 [LIVE SELL%s] filled=%.4f sh @ avg %.4f | proceeds %.2f$ | PnL %+.2f$ (net %+.2f$) | WR %.1f%%",
            f" {_tag}" if _tag else "", filled_shares, avg_price, proceeds, pnl, net_pnl, wr,
        )
        return pnl

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
        In live mode BUY calls are suppressed — position is written via live_open() after CLOB confirms fill.
        In live mode SELL calls are suppressed — position is closed via live_close() after CLOB confirms fill.
        """
        if amount_usd is None:
            amount_usd = self.trade_amount_usd
        if side in ("BUY", "BUY_UP", "BUY_DOWN") and getattr(self, "_suppress_buy", False):
            # Mirror paper BUY gates and pricing math so HFTEngine + bot live path see the
            # same book/exec/shares metadata as SIM, without mutating balance/inventory
            # (those are updated in live_open() after CLOB fill).
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
                    self.balance,
                    amount_usd,
                )
                return None
            if self.inventory > 0.0:
                logging.warning(
                    "[LIVE] log_trade(BUY) suppressed with open inventory=%.4f — "
                    "blocked for parity (engine OPEN expects flat).",
                    self.inventory,
                )
                return None
            book_px = float(price)
            exec_price = book_px * (1 + self.fee_rate)
            new_shares = amount_usd / exec_price if exec_price > 0 else 0.0
            logging.debug(
                "[LIVE] log_trade(BUY) suppressed — will record via live_open(); "
                "paper-equivalent book=%.4f exec=%.4f sh=%.4f $=%.2f.",
                book_px,
                exec_price,
                new_shares,
                amount_usd,
            )
            return {
                "suppressed": True,
                "side": side,
                "book_px": book_px,
                "exec_px": exec_price,
                "amount_usd": amount_usd,
                "shares_filled": new_shares,
                "shares_position": new_shares,
                "price": exec_price,
                "shares": new_shares,
            }
        if side == "SELL" and getattr(self, "_suppress_buy", False):
            logging.debug("[LIVE] log_trade(SELL) suppressed — will record via live_close() after fill.")
            return {"suppressed": True, "side": side}
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
            _mode = "LIVE" if self.live_mode else "SIM"
            logging.info(
                "🟢 [%s %s] book=%.4f exec=%.4f | %0.2f$ → %0.4f sh (pos %0.4f @ avg %0.4f)",
                _mode,
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
            self.closed_trade_pnls.append(profit)
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
                bad_wr = float(os.getenv("HFT_BAD_REGIME_WINRATE"))
                cooldown_sec = float(os.getenv("HFT_REGIME_COOLDOWN_SEC"))
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
            _mode = "LIVE" if self.live_mode else "SIM"
            _sell_hdr = f"[{_mode} SELL {_sn}]" if _sn else f"[{_mode} SELL]"
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
