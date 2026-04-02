"""CSV trade journal for V5 meta-optimization and live session audit.

Supports both synchronous writes (legacy) and async queue-based writes
for non-blocking main loop operation.
"""

from __future__ import annotations

import asyncio
import csv
import logging
import os
import tempfile
import time
from collections import deque
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
    "entry_adx",
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
    if set(target_fields).issubset(set(header_keys)):
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
            "entry_adx": decision.get("entry_adx"),
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
            "entry_adx": decision.get("entry_adx"),
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


# Maximum number of pending journal entries in the async queue.
# Oldest entries are dropped when full to prevent memory growth.
_JOURNAL_QUEUE_MAX_SIZE = 100


class TradeJournal:
    """Append trade journal rows to CSV with a stable schema.

    Supports async queue-based writes for non-blocking main loop operation.
    Use ``start_async_writer()`` to enable background task, and
    ``stop_async_writer()`` to flush remaining entries on shutdown.
    """

    def __init__(self, path: str = "reports/trade_journal.csv") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.path.stat().st_size > 0:
            migrate_journal_schema_if_needed(self.path, JOURNAL_FIELDNAMES)
        if not self.path.exists() or not _has_header(self.path):
            with self.path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDNAMES)
                writer.writeheader()

        # Async write queue (non-blocking main loop)
        self._write_queue: deque[dict[str, Any]] = deque(maxlen=_JOURNAL_QUEUE_MAX_SIZE)
        self._async_writer_task: asyncio.Task | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._queue_dropped_count: int = 0

    def _write_row(self, row: Mapping[str, Any]) -> None:
        """Synchronously write a single row to the journal CSV."""
        with self.path.open("a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=JOURNAL_FIELDNAMES, extrasaction="ignore")
            writer.writerow({k: _str_cell(row.get(k)) for k in JOURNAL_FIELDNAMES})

    def _write_row_sync(self, row: Mapping[str, Any]) -> None:
        """Synchronous write (legacy compatibility)."""
        self._write_row(row)

    def _enqueue_journal_row(self, row: dict[str, Any]) -> None:
        """Append ``row`` to ``_write_queue``, counting implicit maxlen evictions.

        :class:`collections.deque` with ``maxlen`` drops the leftmost item on append
        when full without raising; we detect ``len >= maxlen`` before append and
        increment :attr:`_queue_dropped_count` for that eviction.
        """
        q = self._write_queue
        maxlen = q.maxlen
        if maxlen is not None and len(q) >= maxlen:
            self._queue_dropped_count += 1
            logging.debug(
                "Trade journal write queue full (%d pending); evicting oldest row",
                maxlen,
            )
        q.append(row)

    def start_async_writer(self) -> None:
        """Start the background async writer task.

        Must be called from within an async context with a running event loop.
        After calling this, use ``queue_close()`` and ``queue_open()`` instead
        of ``record_close()`` and ``record_open()`` for non-blocking writes.
        """
        if self._async_writer_task is not None and not self._async_writer_task.done():
            return  # Already running
        self._shutdown_event = asyncio.Event()
        self._async_writer_task = asyncio.create_task(self._async_writer_loop())

    async def stop_async_writer(self) -> int:
        """Stop the background writer and drain the queue on shutdown.

        Sets :attr:`_shutdown_event` so :meth:`_async_writer_loop` exits, then waits up
        to 5 seconds for :attr:`_async_writer_task` to finish. On
        :exc:`asyncio.TimeoutError` or :exc:`asyncio.CancelledError`, pending rows are
        written synchronously via :meth:`_flush_queue`. If there was no writer task,
        :meth:`_flush_queue` runs unconditionally.

        **Return value:** :attr:`_queue_dropped_count` — the cumulative number of journal
        rows dropped because the bounded queue was full (not the number of rows flushed
        to disk). Callers use this for logging (e.g. ``Journal: N entries dropped…``).
        """
        if self._shutdown_event is None:
            return 0
        self._shutdown_event.set()
        if self._async_writer_task is not None:
            try:
                await asyncio.wait_for(self._async_writer_task, timeout=5.0)
            except asyncio.TimeoutError:
                logging.warning("Async journal writer timed out — flushing remaining entries synchronously")
                self._flush_queue()
            except asyncio.CancelledError:
                self._flush_queue()
        else:
            self._flush_queue()
        return self._queue_dropped_count

    def _flush_queue(self) -> None:
        """Drain and write all pending queue entries synchronously."""
        while self._write_queue:
            try:
                row = self._write_queue.popleft()
                self._write_row(row)
            except Exception as exc:
                logging.error("Failed to flush journal entry: %s", exc)

    async def _async_writer_loop(self) -> None:
        """Background task that drains the write queue and writes to CSV."""
        if self._shutdown_event is None:
            return
        while not self._shutdown_event.is_set():
            if self._write_queue:
                try:
                    row = self._write_queue.popleft()
                    await asyncio.to_thread(self._write_row, row)
                except Exception as exc:
                    logging.error("Async journal write failed: %s", exc)
            else:
                # No entries to write — sleep briefly to avoid busy-waiting
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass  # Normal: no shutdown signal yet
        # Final flush after shutdown signal
        self._flush_queue()

    def queue_close(
        self,
        *,
        decision: dict[str, Any],
        live_pnl: float,
        rsi_state: Mapping[str, Any] | None,
    ) -> bool:
        """Queue a close entry for async writing (non-blocking).

        When the queue is at capacity, the oldest pending row is evicted and
        :attr:`dropped_count` is incremented; the new row is still enqueued.
        """
        row = JournalEntryComposer.close_row(decision, live_pnl, rsi_state)
        self._enqueue_journal_row(row)
        return True

    def queue_open(
        self,
        *,
        decision: dict[str, Any],
        filled_shares: float,
        avg_price: float,
        amount_usd: float,
        rsi_state: Mapping[str, Any] | None = None,
        book_snapshot: Mapping[str, Any] | None = None,
    ) -> bool:
        """Queue an open entry for async writing (non-blocking).

        When the queue is at capacity, the oldest pending row is evicted and
        :attr:`dropped_count` is incremented; the new row is still enqueued.
        """
        row = JournalEntryComposer.open_row(
            decision,
            filled_shares,
            avg_price,
            amount_usd,
            rsi_state,
            book_snapshot,
        )
        self._enqueue_journal_row(row)
        return True

    def record_close(
        self,
        *,
        decision: dict[str, Any],
        live_pnl: float,
        rsi_state: Mapping[str, Any] | None,
    ) -> None:
        """Record a closed trade (paper or live). Synchronous write."""
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
        """Record a confirmed live BUY fill. Synchronous write."""
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

    @property
    def queue_size(self) -> int:
        """Number of entries pending in the async write queue."""
        return len(self._write_queue)

    @property
    def dropped_count(self) -> int:
        """Number of entries dropped due to queue overflow."""
        return self._queue_dropped_count
