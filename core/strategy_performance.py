"""Per-strategy and per-profile realized PnL aggregation for session reports."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class StrategyPerformanceSlice:
    """Cumulative stats for one attribution key (e.g. phase_router:soft_flow)."""

    trades: int = 0
    wins: int = 0
    pnl_sum: float = 0.0


@dataclass(slots=True)
class StrategyPerformanceBook:
    """Track closed-trade PnL by performance_key across a session."""

    slices: dict[str, StrategyPerformanceSlice] = field(default_factory=dict)

    def record_close(self, performance_key: str | None, pnl: float) -> None:
        """Add one closed trade to the bucket for performance_key."""
        if not performance_key:
            return
        key = str(performance_key).strip()
        if not key:
            return
        sl = self.slices.get(key)
        if sl is None:
            sl = StrategyPerformanceSlice()
            self.slices[key] = sl
        sl.trades += 1
        sl.pnl_sum += float(pnl)
        if float(pnl) > 0.0:
            sl.wins += 1

    def reset(self) -> None:
        """Clear all buckets (e.g. optional reset on new market)."""
        self.slices.clear()

    def win_rate(self, key: str) -> float:
        """Return win rate for key or 0 when no trades."""
        sl = self.slices.get(key)
        if not sl or sl.trades <= 0:
            return 0.0
        return sl.wins / sl.trades

    def total_pnl_all_keys(self) -> float:
        """Return sum of pnl_sum across all keys."""
        return sum(s.pnl_sum for s in self.slices.values())

    def lines_for_report(self) -> list[str]:
        """Return human-readable lines for stats / final report."""
        if not self.slices:
            return ["  (no per-strategy closes yet)"]
        lines = []
        for key in sorted(self.slices.keys()):
            sl = self.slices[key]
            wr = (sl.wins / sl.trades * 100.0) if sl.trades > 0 else 0.0
            lines.append(f"  {key:<42} trades={sl.trades:>4}  WR={wr:>5.1f}%  PnL={sl.pnl_sum:>+10.4f} USD")
        lines.append(f"  {'TOTAL (sum of slices)':<42} PnL={self.total_pnl_all_keys():>+10.4f} USD")
        return lines

    def summary_compact(self) -> str:
        """Return a short single-line summary for logging."""
        if not self.slices:
            return ""
        parts = [f"{k}={s.pnl_sum:+.2f}" for k, s in sorted(self.slices.items())]
        return " | ".join(parts)
