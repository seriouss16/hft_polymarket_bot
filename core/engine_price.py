"""Compact price history arrays for RSI / indicators (shared by engine modules)."""

from __future__ import annotations

import itertools

import numpy as np


def price_array_for_rsi(price_history, max_len: int) -> np.ndarray:
    """Build a compact float array for RSI without copying unbounded history."""
    if not price_history:
        return np.empty(0, dtype=np.float64)
    n = len(price_history)
    if n <= max_len:
        return np.asarray(price_history, dtype=np.float64)
    return np.asarray(
        list(itertools.islice(price_history, n - max_len, None)),
        dtype=np.float64,
    )
