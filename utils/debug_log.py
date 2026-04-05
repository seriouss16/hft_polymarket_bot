"""Centralized debug logging with async queue.

Provides a simple function `_append_debug_log` that can be imported
by any module without circular dependency issues.
"""

from __future__ import annotations

from typing import Any

from utils.async_debug_logger import AsyncDebugLogger

# Global debug logger instance (set by HFTEngine)
_debug_logger: AsyncDebugLogger | None = None


def set_debug_logger(logger: AsyncDebugLogger) -> None:
    """Set the global debug logger instance."""
    global _debug_logger
    _debug_logger = logger


def _append_debug_log(payload: dict) -> None:
    """Queue a debug log entry for non-blocking async write."""
    if _debug_logger is not None:
        _debug_logger.queue_log(payload)
