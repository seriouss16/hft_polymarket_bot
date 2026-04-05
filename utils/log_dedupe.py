"""Logging filters to reduce identical consecutive lines in high-frequency bots."""

from __future__ import annotations

import logging
import os
import threading
import time


class SameMessageDedupeFilter(logging.Filter):
    """Suppress duplicate INFO+ lines within a cooldown (same formatted text and level).

    Safe for multiple handlers: the decision is cached on ``LogRecord`` so stdout and file
    stay in sync. DEBUG is never filtered. Set ``HFT_LOG_DEDUPE_SAME_MSG_SEC=0`` to disable.
    """

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self._last_key: tuple[int, str] | None = None
        self._last_emit_ts = 0.0

    def filter(self, record: logging.LogRecord) -> bool:
        """Return False when this line is dropped as a duplicate."""
        if getattr(record, "_dedupe_same_msg_resolved", False):
            return bool(getattr(record, "_dedupe_same_msg_allow", True))
        try:
            min_sec = float(os.getenv("HFT_LOG_DEDUPE_SAME_MSG_SEC"))
        except (ValueError, TypeError):
            min_sec = 1.0
        record._dedupe_same_msg_resolved = True
        if min_sec <= 0.0:
            record._dedupe_same_msg_allow = True
            return True
        if record.levelno < logging.INFO:
            record._dedupe_same_msg_allow = True
            return True
        try:
            msg = record.getMessage()
        except Exception:
            record._dedupe_same_msg_allow = True
            return True
        key = (record.levelno, msg)
        now = time.time()
        with self._lock:
            if self._last_key is not None and key == self._last_key and now - self._last_emit_ts < min_sec:
                record._dedupe_same_msg_allow = False
                return False
            self._last_key = key
            self._last_emit_ts = now
        record._dedupe_same_msg_allow = True
        return True
