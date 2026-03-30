"""Read required configuration from the process environment (values from ``config/runtime.env``)."""

from __future__ import annotations

import os


def req_str(name: str) -> str:
    """Return a non-empty string for ``name`` or raise."""
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        raise RuntimeError(
            f"Missing required environment variable {name!r}. "
            "Add it to hft_bot/config/runtime.env (loaded by bot.py or tests)."
        )
    return str(raw).strip()


def req_float(name: str) -> float:
    """Parse ``name`` as float."""
    return float(req_str(name))


def req_int(name: str) -> int:
    """Parse ``name`` as int."""
    return int(req_str(name))
