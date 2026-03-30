"""CSV trade journal for V5 meta-optimization and live session audit."""

from __future__ import annotations

import csv
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping


def _str_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "1" if v else "0"
    return str(v)


# Canonical column order for new and migrated files.
JOURNAL_FIELDNAMES = [
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
    "exit_rsi_raw",
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
    "row_kind",
]

# Backward compatibility for modules that imported ``_FIELDNAMES``.
_FIELDNAMES = JOURNAL_FIELDNAMES


def migrate_journal_schema_if_needed(path: Path, target_fields: list[str]) -> None:
    """If the file exists with an older header, rewrite it with ``target_fields`` and pad rows."""
    if not path.is_file() or path.stat().st_size == 0:
        return
    with path.open("r", encoding="utf-8", newline="") as f:
        first = f.readline()
    if not first.strip():
        return
    header_keys = next(csv.reader([first.strip()]))
    if all(k in header_keys for k in ("row_kind", "exit_rsi_raw")):
        return
    rows: list[dict[str, str]] = []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row is None:
                continue
            normalized: dict[str, str] = {}
            for k in target_fields:
                if k == "row_kind":
                    v = row.get("row_kind") or row.get("event") or ""
                    normalized[k] = v if v else "close"
                elif k == "exit_rsi_raw":
                    normalized[k] = row.get("exit_rsi_raw") or ""
                else:
                    normalized[k] = row.get(k) if row.get(k) is not None else ""
            rows.append(normalized)
    fd, tmp_path = tempfile.mkstemp(
        suffix=".csv", dir=path.parent, text=True,
    )
    os.close(fd)
    tmp_path = Path(tmp_path)
    try:
        with tmp_path.open("w", encoding="utf-8", newline="") as out:
            w = csv.DictWriter(out, fieldnames=target_fields)
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k, "") for k in target_fields})
        os.replace(tmp_path, path)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _has_header(path: Path) -> bool:
    """Return True when the first non-empty line of the CSV looks like a header row."""
    try:
        with path.open("r", encoding="utf-8", newline="") as f:
            first = f.readline().strip()
        return first.startswith("ts,") or first == ",".join(JOURNAL_FIELDNAMES[:3])
    except OSError:
        return False


class JournalEntryComposer:
    """Builds normalized journal rows from engine decisions and live fills (single place for field mapping)."""

    @staticmethod
    def _rsi(rsi_state: Mapping[str, Any] | None) -> dict[str, Any]:
        if not rsi_state:
            return {
                "exit_rsi": "",
                "exit_rsi_raw": "",
                "rsi_band_lower": "",
                "rsi_band_upper": "",
                "rsi_slope": "",
            }
        return {
            "exit_rsi": rsi_state.get("rsi", ""),
            "exit_rsi_raw": rsi_state.get("rsi_raw", rsi_state.get("rsi", "")),
            "rsi_band_lower": rsi_state.get("lower", ""),
            "rsi_band_upper": rsi_state.get("upper", ""),
            "rsi_slope": rsi_state.get("slope", ""),
        }

    @staticmethod
    def close_row(
        decision: dict[str, Any],
        live_pnl: float,
        rsi_state: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Row for a closed trade (paper or live)."""
        rs = JournalEntryComposer._rsi(rsi_state)
        return {
            "ts": time.time(),
            "row_kind": "close",
            "side": decision.get("side"),
            "entry_edge": decision.get("entry_edge"),
            "exit_edge": decision.get("exit_edge"),
            "duration_sec": decision.get("duration_sec"),
            "entry_trend": decision.get("entry_trend"),
            "entry_speed": decision.get("entry_speed"),
            "entry_depth": decision.get("entry_depth"),
            "entry_imbalance": decision.get("entry_imbalance"),
            "latency_ms": decision.get("latency_ms"),
            "pnl": live_pnl,
            "exit_reason": decision.get("reason"),
            **rs,
            "entry_book_px": decision.get("entry_book_px"),
            "entry_exec_px": decision.get("entry_exec_px"),
            "exit_book_px": decision.get("exit_book_px"),
            "exit_exec_px": decision.get("exit_exec_px"),
            "shares_bought": decision.get("shares_bought"),
            "shares_sold": decision.get("shares_sold"),
            "cost_usd": decision.get("cost_usd"),
            "cost_basis_usd": decision.get("cost_basis_usd"),
            "proceeds_usd": decision.get("proceeds_usd"),
            "entry_up_bid": decision.get("entry_up_bid"),
            "entry_up_ask": decision.get("entry_up_ask"),
            "entry_down_bid": decision.get("entry_down_bid"),
            "entry_down_ask": decision.get("entry_down_ask"),
            "exit_up_bid": decision.get("exit_up_bid"),
            "exit_up_ask": decision.get("exit_up_ask"),
            "exit_down_bid": decision.get("exit_down_bid"),
            "exit_down_ask": decision.get("exit_down_ask"),
            "strategy_name": decision.get("strategy_name"),
            "entry_profile": decision.get("entry_profile"),
            "performance_key": decision.get("performance_key"),
        }

    @staticmethod
    def open_row(
        decision: dict[str, Any],
        filled_shares: float,
        avg_price: float,
        amount_usd: float,
        rsi_state: Mapping[str, Any] | None,
        book_snapshot: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Row for a confirmed live entry (CLOB fill)."""
        trade = decision.get("trade") or {}
        book = book_snapshot or {}
        rs = JournalEntryComposer._rsi(rsi_state)
        return {
            "ts": time.time(),
            "row_kind": "open",
            "side": decision.get("side"),
            "entry_edge": decision.get("entry_edge"),
            "exit_edge": "",
            "duration_sec": "",
            "entry_trend": decision.get("entry_trend"),
            "entry_speed": decision.get("entry_speed"),
            "entry_depth": decision.get("entry_depth"),
            "entry_imbalance": decision.get("entry_imbalance"),
            "latency_ms": decision.get("latency_ms"),
            "pnl": "",
            "exit_reason": "",
            **rs,
            "entry_book_px": trade.get("book_px"),
            "entry_exec_px": avg_price,
            "exit_book_px": "",
            "exit_exec_px": "",
            "shares_bought": filled_shares,
            "shares_sold": "",
            "cost_usd": amount_usd,
            "cost_basis_usd": amount_usd,
            "proceeds_usd": "",
            "entry_up_bid": book.get("bid"),
            "entry_up_ask": book.get("ask"),
            "entry_down_bid": book.get("down_bid"),
            "entry_down_ask": book.get("down_ask"),
            "exit_up_bid": "",
            "exit_up_ask": "",
            "exit_down_bid": "",
            "exit_down_ask": "",
            "strategy_name": decision.get("strategy_name"),
            "entry_profile": decision.get("entry_profile"),
            "performance_key": decision.get("performance_key"),
        }


class TradeJournal:
    """Append trade journal rows to CSV with a stable schema."""

    def __init__(self, path: str = "reports/trade_journal.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 0:
            migrate_journal_schema_if_needed(self.path, JOURNAL_FIELDNAMES)
        if not self.path.exists() or not _has_header(self.path):
            with self.path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDNAMES)
                writer.writeheader()

    def _write_row(self, row: Mapping[str, Any]) -> None:
        with self.path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDNAMES, extrasaction="ignore")
            writer.writerow({k: _str_cell(row.get(k)) for k in JOURNAL_FIELDNAMES})

    def record_close(
        self,
        *,
        decision: dict[str, Any],
        live_pnl: float,
        rsi_state: Mapping[str, Any] | None,
    ) -> None:
        """Record a closed trade (paper or live)."""
        self._write_row(JournalEntryComposer.close_row(decision, live_pnl, rsi_state))

    def record_open(
        self,
        *,
        decision: dict[str, Any],
        filled_shares: float,
        avg_price: float,
        amount_usd: float,
        rsi_state: Mapping[str, Any] | None = None,
        book_snapshot: Mapping[str, Any] | None = None,
    ) -> None:
        """Record a confirmed live BUY fill."""
        self._write_row(
            JournalEntryComposer.open_row(
                decision,
                filled_shares,
                avg_price,
                amount_usd,
                rsi_state,
                book_snapshot,
            )
        )
