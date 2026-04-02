"""Tests for ``data.balance_cache`` (no live HTTP)."""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from data.balance_cache import BalanceCache, BalanceCacheEntry, ConditionalAllowanceCache, AllowanceCacheEntry  # noqa: E402


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


# ---------------------------------------------------------------------------
# ConditionalAllowanceCache tests
# ---------------------------------------------------------------------------


class TestConditionalAllowanceCache:
    """TTL-based allowance cache for pre-emptive background refresh."""

    def test_set_and_get_allowance(self) -> None:
        """set_allowance stores value; get_cached_allowance returns it."""
        cache = ConditionalAllowanceCache(ttl_sec=300.0)
        cache.set_allowance("token_abc", 1.0)
        assert cache.get_cached_allowance("token_abc") == 1.0

    def test_get_cached_allowance_returns_none_for_missing(self) -> None:
        """get_cached_allowance returns None for unknown token."""
        cache = ConditionalAllowanceCache(ttl_sec=300.0)
        assert cache.get_cached_allowance("unknown") is None

    def test_expired_allowance_returns_stale_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When TTL expires, get_cached_allowance still returns stale value."""
        t0 = 1_000_000.0
        ttl_sec = 300.0
        clock = {"t": t0}

        def fake_time() -> float:
            return clock["t"]

        monkeypatch.setattr(time, "time", fake_time)
        cache = ConditionalAllowanceCache(ttl_sec=ttl_sec)
        cache.set_allowance("token_abc", 0.5)
        clock["t"] = t0 + ttl_sec + 1.0
        result = cache.get_cached_allowance("token_abc")
        assert result == 0.5

    def test_schedule_refresh_and_get_refresh_queue(self) -> None:
        """schedule_refresh queues token; get_refresh_queue returns and clears it."""
        cache = ConditionalAllowanceCache()
        cache.schedule_refresh("token_up")
        cache.schedule_refresh("token_down")
        queue = cache.get_refresh_queue()
        assert set(queue) == {"token_up", "token_down"}
        # Queue should be cleared after retrieval
        assert cache.get_refresh_queue() == []

    def test_schedule_refresh_queue_bounded_fifo_eviction(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When over MAX_REFRESH_QUEUE_SIZE, oldest entries are evicted (FIFO)."""
        import data.balance_cache as bc

        monkeypatch.setattr(bc, "MAX_REFRESH_QUEUE_SIZE", 3)
        cache = ConditionalAllowanceCache()
        cache.schedule_refresh("t0")
        cache.schedule_refresh("t1")
        cache.schedule_refresh("t2")
        cache.schedule_refresh("t_new")
        q = cache.get_refresh_queue()
        assert q == ["t1", "t2", "t_new"]
        assert "t0" not in q

    def test_batch_set_allowances(self) -> None:
        """batch_set_allowances stores multiple values at once."""
        cache = ConditionalAllowanceCache(ttl_sec=300.0)
        cache.batch_set_allowances({"t1": 1.0, "t2": 2.0, "t3": 3.0})
        assert cache.get_cached_allowance("t1") == 1.0
        assert cache.get_cached_allowance("t2") == 2.0
        assert cache.get_cached_allowance("t3") == 3.0

    def test_clear_specific(self) -> None:
        """clear(token_id) removes only that token's cache."""
        cache = ConditionalAllowanceCache(ttl_sec=300.0)
        cache.set_allowance("t1", 1.0)
        cache.set_allowance("t2", 2.0)
        cache.clear("t1")
        assert cache.get_cached_allowance("t1") is None
        assert cache.get_cached_allowance("t2") == 2.0

    def test_clear_all(self) -> None:
        """clear() without args removes all entries."""
        cache = ConditionalAllowanceCache(ttl_sec=300.0)
        cache.set_allowance("t1", 1.0)
        cache.set_allowance("t2", 2.0)
        cache.clear()
        assert cache.get_cached_allowance("t1") is None
        assert cache.get_cached_allowance("t2") is None

    def test_metrics_tracking(self) -> None:
        """Cache tracks hits, misses, and stale_reads."""
        cache = ConditionalAllowanceCache(ttl_sec=300.0)
        cache.set_allowance("t1", 1.0)
        cache.get_cached_allowance("t1")  # hit
        cache.get_cached_allowance("t1")  # hit
        cache.get_cached_allowance("unknown")  # miss
        metrics = cache.get_metrics()
        assert metrics["hits"] == 2
        assert metrics["misses"] == 1

    def test_stale_read_metrics(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Expired entries count as stale_reads."""
        t0 = 2_000_000.0
        ttl_sec = 60.0
        clock = {"t": t0}

        def fake_time() -> float:
            return clock["t"]

        monkeypatch.setattr(time, "time", fake_time)
        cache = ConditionalAllowanceCache(ttl_sec=ttl_sec)
        cache.set_allowance("t1", 1.0)
        clock["t"] = t0 + ttl_sec + 1.0
        cache.get_cached_allowance("t1")  # stale read
        metrics = cache.get_metrics()
        assert metrics["stale_reads"] == 1

    def test_refresh_and_batch_refresh_metrics(self) -> None:
        """record_refresh and record_batch_refresh update metrics."""
        cache = ConditionalAllowanceCache()
        cache.record_refresh()
        cache.record_refresh()
        cache.record_batch_refresh()
        metrics = cache.get_metrics()
        assert metrics["refreshes"] == 2
        assert metrics["batch_refreshes"] == 1

    def test_allowance_cache_entry_properties(self) -> None:
        """AllowanceCacheEntry has correct is_expired and age_sec."""
        now = time.time()
        entry = AllowanceCacheEntry(allowance=1.0, expires_at=now + 100, last_refresh=now)
        assert entry.is_expired is False
        assert entry.age_sec < 1.0

        expired = AllowanceCacheEntry(allowance=1.0, expires_at=now - 1, last_refresh=now - 200)
        assert expired.is_expired is True
        assert expired.age_sec > 100.0

    def test_default_ttl_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """TTL defaults to ALLOWANCE_CACHE_TTL_SEC env var."""
        monkeypatch.setenv("ALLOWANCE_CACHE_TTL_SEC", "600")
        cache = ConditionalAllowanceCache()
        assert cache._ttl_sec == 600.0
