"""CSV trade journal for V5 meta-optimization."""

from __future__ import annotations

import csv
from pathlib import Path


_FIELDNAMES = [
    "ts",
    "side",
    "entry_edge",
    "exit_edge",
    "duration_sec",
    "entry_trend",
    "entry_speed",
    "entry_depth",
    "entry_imbalance",
    "latency_ms",
    "pnl",
    "exit_reason",
    "exit_rsi",
    "rsi_band_lower",
    "rsi_band_upper",
    "rsi_slope",
    "entry_book_px",
    "entry_exec_px",
    "exit_book_px",
    "exit_exec_px",
    "shares_bought",
    "shares_sold",
    "cost_usd",
    "cost_basis_usd",
    "proceeds_usd",
    "entry_up_bid",
    "entry_up_ask",
    "entry_down_bid",
    "entry_down_ask",
    "exit_up_bid",
    "exit_up_ask",
    "exit_down_bid",
    "exit_down_ask",
    "strategy_name",
    "entry_profile",
    "performance_key",
]


class TradeJournal:
    """Append closed-trade features and outcomes to CSV."""

    def __init__(self, path: str = "reports/trade_journal.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
                writer.writeheader()

    def append(self, row: dict) -> None:
        """Append one trade row with stable column order."""
        with self.path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_FIELDNAMES, extrasaction="ignore")
            writer.writerow({k: row.get(k) for k in _FIELDNAMES})
