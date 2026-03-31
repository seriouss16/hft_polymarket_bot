"""Tests for ``data.balance_cache`` (no live HTTP)."""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.balance_cache import BalanceCache, BalanceCacheEntry  # noqa: E402


def test_balance_cache_entry_is_fresh_method() -> None:
    e = BalanceCacheEntry(value=1.0, timestamp=time.time())
    assert e.is_fresh(5.0) is True
    old = BalanceCacheEntry(value=1.0, timestamp=time.time() - 100.0)
    assert old.is_fresh(5.0) is False


def test_conditional_cache_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BALANCE_CACHE_MAX_CONDITIONAL_ENTRIES", "2")
    calls: list[str] = []

    def _fetch_usdc() -> float:
        return 1.0

    def _fetch_cond(tid: str) -> float:
        calls.append(tid)
        return 1.0

    c = BalanceCache(_fetch_usdc, _fetch_cond, max_age_sec=0.0, conditional_max_age_sec=0.0)
    for i in range(4):
        c.get_conditional_balance(f"t{i}")
    assert len(c._conditional_caches) == 2
