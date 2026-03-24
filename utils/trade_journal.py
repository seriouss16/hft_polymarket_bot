"""CSV trade journal for V5 meta-optimization."""

from __future__ import annotations

import csv
from pathlib import Path


class TradeJournal:
    """Append closed-trade features and outcomes to CSV."""

    def __init__(self, path: str = "reports/trade_journal.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            with self.path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(
                    [
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
                    ]
                )

    def append(self, row: dict) -> None:
        """Append one trade row with stable column order."""
        with self.path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    row.get("ts"),
                    row.get("side"),
                    row.get("entry_edge"),
                    row.get("exit_edge"),
                    row.get("duration_sec"),
                    row.get("entry_trend"),
                    row.get("entry_speed"),
                    row.get("entry_depth"),
                    row.get("entry_imbalance"),
                    row.get("latency_ms"),
                    row.get("pnl"),
                ]
            )

