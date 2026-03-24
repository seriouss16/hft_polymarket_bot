"""Risk and meta controls for V5 trading loop."""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class RiskEngine:
    """Manage drawdown, sizing, and cooldown after losses."""

    max_drawdown_pct: float = 0.10
    max_position_pct: float = 0.10
    loss_cooldown_sec: float = 10.0
    peak_equity: float = 0.0
    cooldown_until: float = 0.0

    def update_equity(self, equity: float) -> None:
        """Track new peak equity."""
        if equity > self.peak_equity:
            self.peak_equity = equity

    def drawdown_pct(self, equity: float) -> float:
        """Return current drawdown percentage from peak equity."""
        if self.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.peak_equity - equity) / self.peak_equity)

    def can_trade(self, now_ts: float, equity: float) -> bool:
        """Return False when in cooldown or drawdown stop triggered."""
        if now_ts < self.cooldown_until:
            return False
        if self.drawdown_pct(equity) >= self.max_drawdown_pct:
            return False
        return True

    def allowed_notional(self, equity: float) -> float:
        """Return max notional size allowed for next position."""
        return max(0.0, equity * self.max_position_pct)

    def on_trade_closed(self, pnl: float, now_ts: float) -> None:
        """Apply post-trade cooldown after a losing trade."""
        if pnl < 0:
            self.cooldown_until = max(self.cooldown_until, now_ts + self.loss_cooldown_sec)

