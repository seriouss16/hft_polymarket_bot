"""Tests for latency optimizations: non-blocking debug logging and balance cache."""

from __future__ import annotations

import asyncio
import os
import time
from unittest.mock import Mock, patch

import pytest

from utils.async_debug_logger import AsyncDebugLogger


class TestAsyncDebugLogger:
    """Test the non-blocking async debug logger."""

    @pytest.fixture
    def temp_log_file(self, tmp_path):
        """Create a temporary log file."""
        return tmp_path / "debug.log"

    def test_logger_enabled_from_env(self, temp_log_file, monkeypatch):
        """Test logger is enabled when HFT_DEBUG_LOG_ENABLED=1."""
        monkeypatch.setenv("HFT_DEBUG_LOG_ENABLED", "1")
        monkeypatch.setenv("DEBUG_LOG_PATH", str(temp_log_file))
        monkeypatch.setenv("DEBUG_SESSION_ID", "test-session")

        logger = AsyncDebugLogger(str(temp_log_file), "test-session")
        assert logger.is_enabled() is True

    def test_logger_disabled_by_default(self, temp_log_file):
        """Test logger is disabled by default."""
        logger = AsyncDebugLogger(str(temp_log_file), "test-session")
        assert logger.is_enabled() is False

    def test_queue_log_disabled_when_not_enabled(self, temp_log_file):
        """Test queue_log returns False when logger is disabled."""
        logger = AsyncDebugLogger(str(temp_log_file), "test-session")
        result = logger.queue_log({"event": "test"})
        assert result is False

    @pytest.mark.asyncio
    async def test_queue_log_enabled_queues_payload(self, temp_log_file, monkeypatch):
        """Test queue_log adds payload to queue when enabled."""
        monkeypatch.setenv("HFT_DEBUG_LOG_ENABLED", "1")
        logger = AsyncDebugLogger(str(temp_log_file), "test-session")
        logger.start()

        payload = {"event": "test", "data": {"value": 123}}
        result = logger.queue_log(payload)
        assert result is True
        assert len(logger._write_queue) == 1
        assert logger._write_queue[0] == payload

        await logger.stop()

    @pytest.mark.asyncio
    async def test_queue_drops_oldest_when_full(self, temp_log_file, monkeypatch):
        """Test that oldest entry is dropped when queue is full."""
        monkeypatch.setenv("HFT_DEBUG_LOG_ENABLED", "1")
        logger = AsyncDebugLogger(str(temp_log_file), "test-session")
        logger.start()

        max_size = logger._write_queue.maxlen
        for i in range(max_size):
            logger.queue_log({"event": f"entry_{i}"})

        logger.queue_log({"event": "new_entry"})
        assert len(logger._write_queue) == max_size
        assert logger._write_queue[-1] == {"event": "new_entry"}
        assert logger._queue_dropped_count == 1

        await logger.stop()

    def test_flush_queue_writes_all_entries(self, temp_log_file, monkeypatch):
        """Test _flush_queue writes all pending entries to disk."""
        monkeypatch.setenv("HFT_DEBUG_LOG_ENABLED", "1")
        logger = AsyncDebugLogger(str(temp_log_file), "test-session")

        entries = [
            {"event": "test1"},
            {"event": "test2"},
            {"event": "test3"},
        ]
        for entry in entries:
            logger._enqueue_log(entry)

        logger._flush_queue()

        assert temp_log_file.exists()
        lines = temp_log_file.read_text().strip().split("\n")
        assert len(lines) == 3
        for line, expected in zip(lines, entries):
            import json
            assert json.loads(line) == expected

    @pytest.mark.asyncio
    async def test_start_and_stop_async_writer(self, temp_log_file, monkeypatch):
        """Test starting and stopping the async writer task."""
        monkeypatch.setenv("HFT_DEBUG_LOG_ENABLED", "1")
        logger = AsyncDebugLogger(str(temp_log_file), "test-session")

        logger.start()
        assert logger._async_writer_task is not None
        assert not logger._async_writer_task.done()

        logger.queue_log({"event": "async_test"})

        dropped = await logger.stop()
        assert logger._async_writer_task is None or logger._async_writer_task.done()
        assert dropped >= 0


class TestBalanceCacheDoubleCheckedLocking:
    """Test that BalanceCache uses double-checked locking to minimize lock contention."""

    @pytest.fixture
    def mock_fetchers(self):
        """Create mock balance fetchers."""
        usdc_fetcher = Mock(return_value=100.0)
        conditional_fetcher = Mock(return_value=50.0)
        return usdc_fetcher, conditional_fetcher

    def test_get_usdc_balance_fresh_cache_returns_immediately(self, mock_fetchers):
        """Test that fresh cache returns without calling fetcher."""
        usdc_fetcher, _ = mock_fetchers
        from data.balance_cache import BalanceCache

        cache = BalanceCache(usdc_fetcher, lambda t: 0.0, max_age_sec=5.0)

        balance1 = cache.get_usdc_balance()
        assert balance1 == 100.0
        usdc_fetcher.assert_called_once()

        usdc_fetcher.reset_mock()
        balance2 = cache.get_usdc_balance()
        assert balance2 == 100.0
        usdc_fetcher.assert_not_called()

    def test_get_conditional_balance_fresh_cache_returns_immediately(self, mock_fetchers):
        """Test that fresh conditional cache returns without calling fetcher."""
        _, conditional_fetcher = mock_fetchers
        from data.balance_cache import BalanceCache

        cache = BalanceCache(lambda: 0.0, conditional_fetcher, conditional_max_age_sec=5.0)

        balance1 = cache.get_conditional_balance("valid_token")
        assert balance1 == 50.0
        assert conditional_fetcher.call_count == 1

        conditional_fetcher.reset_mock()
        balance2 = cache.get_conditional_balance("valid_token")
        assert balance2 == 50.0
        conditional_fetcher.assert_not_called()

    def test_get_usdc_balance_stale_cache_refetches(self, mock_fetchers):
        """Test that stale cache triggers a fetch."""
        usdc_fetcher, _ = mock_fetchers
        from data.balance_cache import BalanceCache, BalanceCacheEntry

        cache = BalanceCache(usdc_fetcher, lambda t: 0.0, max_age_sec=0.1)

        balance1 = cache.get_usdc_balance()
        assert balance1 == 100.0
        assert usdc_fetcher.call_count == 1

        time.sleep(0.15)

        usdc_fetcher.reset_mock()
        balance2 = cache.get_usdc_balance()
        assert balance2 == 100.0
        usdc_fetcher.assert_called_once()

    def test_get_cached_methods_do_not_block(self, mock_fetchers):
        """Test that get_cached_* methods are non-blocking and return None for stale cache."""
        usdc_fetcher, conditional_fetcher = mock_fetchers
        from data.balance_cache import BalanceCache

        cache = BalanceCache(usdc_fetcher, conditional_fetcher, max_age_sec=0.1, conditional_max_age_sec=0.1)

        assert cache.get_cached_usdc_balance() is None
        assert cache.get_cached_conditional_balance("token") is None

        assert cache.get_usdc_balance() == 100.0

        assert cache.get_cached_usdc_balance() == 100.0

        time.sleep(0.15)
        assert cache.get_cached_usdc_balance() is None

    def test_concurrent_access_does_not_corrupt_state(self, mock_fetchers):
        """Test that concurrent calls don't cause race conditions or corrupted state."""
        import threading
        usdc_fetcher, conditional_fetcher = mock_fetchers
        from data.balance_cache import BalanceCache

        cache = BalanceCache(usdc_fetcher, lambda t: 0.0, max_age_sec=5.0)

        results = []
        errors = []

        def worker():
            try:
                bal = cache.get_usdc_balance()
                results.append(bal)
            except Exception as e:
                errors.append(e)

        cache.get_usdc_balance()

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert all(r == 100.0 for r in results)


class TestBalanceCacheMetrics:
    """Test metrics tracking in BalanceCache."""

    def test_metrics_recorded_correctly(self):
        from data.balance_cache import BalanceCache, BalanceMetrics

        usdc_fetcher = Mock(return_value=100.0)
        cond_fetcher = Mock(return_value=50.0)

        cache = BalanceCache(usdc_fetcher, cond_fetcher, max_age_sec=5.0)

        bal1 = cache.get_usdc_balance()
        assert bal1 == 100.0
        metrics = cache.get_metrics()
        assert metrics["fetches_total"] == 1
        assert metrics["http_fallbacks"] == 1
        assert metrics["cache_hits"] == 0

        bal2 = cache.get_usdc_balance()
        assert bal2 == 100.0
        metrics = cache.get_metrics()
        assert metrics["fetches_total"] == 2
        assert metrics["cache_hits"] == 1

    def test_latency_samples_tracked(self):
        from data.balance_cache import BalanceCache

        usdc_fetcher = Mock(side_effect=[100.0, 200.0])
        cache = BalanceCache(usdc_fetcher, lambda t: 0.0, max_age_sec=5.0)

        cache.get_usdc_balance()
        cache.get_usdc_balance()

        metrics = cache.get_metrics()
        assert metrics["latency_samples"] >= 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
