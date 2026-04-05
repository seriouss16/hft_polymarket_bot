"""Non-blocking async debug logger using background queue.

Similar to TradeJournal pattern: logs are queued and written by a background
task to avoid blocking the hot path with synchronous file I/O.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from pathlib import Path
from typing import Any

# Maximum number of pending log entries in the async queue.
# Oldest entries are dropped when full to prevent memory growth.
_DEBUG_QUEUE_MAX_SIZE = 1000


class AsyncDebugLogger:
    """Async debug logger that writes NDJSON lines in a background task.

    Usage:
        logger = AsyncDebugLogger(debug_log_path, session_id)
        await logger.start()
        # In hot path:
        logger.queue_log(payload)  # Non-blocking
        # On shutdown:
        await logger.stop()
    """

    def __init__(self, log_path: str, session_id: str | None = None) -> None:
        self.log_path = Path(log_path)
        self.session_id = session_id
        self._write_queue: deque[dict[str, Any]] = deque(maxlen=_DEBUG_QUEUE_MAX_SIZE)
        self._async_writer_task: asyncio.Task | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._queue_dropped_count: int = 0
        self._enabled = os.getenv("HFT_DEBUG_LOG_ENABLED", "0") == "1"

        if self._enabled:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def is_enabled(self) -> bool:
        return self._enabled

    def _write_row(self, payload: dict[str, Any]) -> None:
        """Synchronously write a single NDJSON line to the debug log."""
        if not self._enabled:
            return
        try:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=True) + "\n")
        except OSError:
            pass

    def _enqueue_log(self, payload: dict[str, Any]) -> None:
        """Append payload to queue, dropping oldest if full."""
        q = self._write_queue
        maxlen = q.maxlen
        if maxlen is not None and len(q) >= maxlen:
            self._queue_dropped_count += 1
            q.popleft()  # Drop oldest to make room
        q.append(payload)

    def queue_log(self, payload: dict[str, Any]) -> bool:
        """Queue a log entry for async writing (non-blocking).

        Returns True if queued successfully, False if disabled.
        """
        if not self._enabled:
            return False
        self._enqueue_log(payload)
        return True

    def start(self) -> None:
        """Start the background async writer task.

        Must be called from within an async context with a running event loop.
        """
        if self._async_writer_task is not None and not self._async_writer_task.done():
            return  # Already running
        self._shutdown_event = asyncio.Event()
        self._async_writer_task = asyncio.create_task(self._async_writer_loop())

    async def stop(self) -> int:
        """Stop the background writer and drain the queue on shutdown.

        Sets shutdown event so the writer loop exits, then waits up to 5 seconds
        for the writer task to finish. On timeout or cancellation, remaining
        entries are written synchronously.

        Returns the number of log entries dropped due to queue overflow.
        """
        if self._shutdown_event is None:
            self._flush_queue()
            return self._queue_dropped_count

        self._shutdown_event.set()
        if self._async_writer_task is not None:
            try:
                await asyncio.wait_for(self._async_writer_task, timeout=5.0)
            except asyncio.TimeoutError:
                logging.warning("Async debug logger timed out — flushing remaining entries synchronously")
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
                payload = self._write_queue.popleft()
                self._write_row(payload)
            except Exception as exc:
                logging.error("Failed to flush debug log entry: %s", exc)

    async def _async_writer_loop(self) -> None:
        """Background task that drains the write queue and writes to file."""
        if self._shutdown_event is None:
            return
        while not self._shutdown_event.is_set():
            if self._write_queue:
                try:
                    payload = self._write_queue.popleft()
                    await asyncio.to_thread(self._write_row, payload)
                except Exception as exc:
                    logging.error("Async debug log write failed: %s", exc)
            else:
                # No entries to write — sleep briefly to avoid busy-waiting
                try:
                    await asyncio.wait_for(self._shutdown_event.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass  # Normal: no shutdown signal yet
        # Final flush after shutdown signal
        self._flush_queue()
