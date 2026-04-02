"""Polymarket balance cache with HTTP polling and metrics tracking.

Phase 3 WebSocket Migration: Balance Updates via WebSocket (Not Available)

Since Polymarket does not provide balance updates via WebSocket, this module
implements an optimized HTTP polling approach with:
- Configurable polling intervals
- Cache with staleness detection
- Metrics tracking for balance fetch latency and success rates
- Conditional token balance caching per token_id

WebSocket Limitation:
- Polymarket User WS (wss://ws-subscriptions-clob.polymarket.com/ws/user) only
  provides order and trade events, not balance updates
- No separate balance WebSocket endpoint exists
- Balance must be fetched via REST API (GET /balance-allowance)

Reference: https://docs.polymarket.com/developers/CLOB/websocket/wss-auth
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class BalanceCacheEntry:
    """Cached balance entry with staleness tracking."""
    value: float
    timestamp: float
    token_id: Optional[str] = None  # For conditional token balances
    
    @property
    def age_sec(self) -> float:
        """Return cache age in seconds."""
        return time.time() - self.timestamp

    def is_fresh(self, max_age_sec: float) -> bool:
        """Return True if cache entry is within ``max_age_sec``."""
        return self.age_sec <= max_age_sec


@dataclass
class BalanceMetrics:
    """Metrics for balance fetch operations."""
    fetches_total: int = 0
    cache_hits: int = 0
    http_fallbacks: int = 0
    errors: int = 0
    latency_samples: list[float] = field(default_factory=list)
    max_samples: int = 1000
    
    @property
    def hit_rate(self) -> float:
        """Return cache hit rate as percentage."""
        if self.fetches_total == 0:
            return 0.0
        return (self.cache_hits / self.fetches_total) * 100.0
    
    @property
    def avg_latency_ms(self) -> float:
        """Return average fetch latency in milliseconds."""
        if not self.latency_samples:
            return 0.0
        return sum(self.latency_samples) / len(self.latency_samples)
    
    @property
    def min_latency_ms(self) -> float:
        """Return minimum fetch latency in milliseconds."""
        if not self.latency_samples:
            return 0.0
        return min(self.latency_samples)
    
    @property
    def max_latency_ms(self) -> float:
        """Return maximum fetch latency in milliseconds."""
        if not self.latency_samples:
            return 0.0
        return max(self.latency_samples)
    
    def record_latency(self, latency_ms: float) -> None:
        """Record a latency sample, maintaining max_samples limit."""
        self.latency_samples.append(latency_ms)
        if len(self.latency_samples) > self.max_samples:
            self.latency_samples.pop(0)
    
    def to_dict(self) -> dict[str, Any]:
        """Return metrics as dictionary."""
        return {
            "fetches_total": self.fetches_total,
            "cache_hits": self.cache_hits,
            "http_fallbacks": self.http_fallbacks,
            "errors": self.errors,
            "hit_rate_pct": round(self.hit_rate, 2),
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "min_latency_ms": round(self.min_latency_ms, 2),
            "max_latency_ms": round(self.max_latency_ms, 2),
            "latency_samples": len(self.latency_samples),
        }


class BalanceCache:
    """Thread-safe balance cache with HTTP polling and metrics.
    
    Since Polymarket does not provide balance updates via WebSocket,
    this cache implements optimized HTTP polling with configurable
    staleness thresholds and comprehensive metrics tracking.
    """
    
    def __init__(
        self,
        balance_fetcher: Callable[[], Optional[float]],
        conditional_balance_fetcher: Callable[[str], Optional[float]],
        max_age_sec: float = 5.0,
        conditional_max_age_sec: float = 10.0,
    ) -> None:
        """Initialize balance cache.
        
        Args:
            balance_fetcher: Callable that fetches USDC balance (returns None on error)
            conditional_balance_fetcher: Callable that fetches conditional token balance
                by token_id (returns None on error)
            max_age_sec: Maximum cache age for USDC balance (default 5s)
            conditional_max_age_sec: Maximum cache age for conditional token balances
                (default 10s, longer due to slower chain confirmation)
        """
        self._fetcher = balance_fetcher
        self._conditional_fetcher = conditional_balance_fetcher
        self._max_age_sec = max_age_sec
        self._conditional_max_age_sec = conditional_max_age_sec
        
        self._usdc_cache: Optional[BalanceCacheEntry] = None
        self._conditional_caches: dict[str, BalanceCacheEntry] = {}
        self._max_conditional_entries = max(
            1, int(os.getenv("BALANCE_CACHE_MAX_CONDITIONAL_ENTRIES", "256"))
        )
        self._lock = threading.Lock()

        # Metrics
        self._metrics = BalanceMetrics()
        self._last_log_time = time.time()
        self._log_interval = 60.0  # Log metrics every 60 seconds

    def _trim_conditional_locked(self) -> None:
        """Evict oldest conditional entries when over ``_max_conditional_entries``."""
        n = len(self._conditional_caches)
        if n <= self._max_conditional_entries:
            return
        excess = n - self._max_conditional_entries
        oldest = sorted(
            self._conditional_caches.items(),
            key=lambda kv: kv[1].timestamp,
        )
        for tid, _ in oldest[:excess]:
            del self._conditional_caches[tid]

    def get_usdc_balance(self) -> Optional[float]:
        """Get USDC balance from cache or fetch if stale.
        
        Returns:
            USDC balance in USD, or None if fetch fails
        """
        start_time = time.perf_counter()
        with self._lock:
            self._metrics.fetches_total += 1

            if self._usdc_cache is not None and self._usdc_cache.is_fresh(self._max_age_sec):
                self._metrics.cache_hits += 1
                latency_ms = (time.perf_counter() - start_time) * 1000
                self._metrics.record_latency(latency_ms)
                return self._usdc_cache.value

            try:
                balance = self._fetcher()
                if balance is not None:
                    self._usdc_cache = BalanceCacheEntry(
                        value=balance,
                        timestamp=time.time(),
                    )
                    self._metrics.http_fallbacks += 1
                    logging.debug(
                        "[BALANCE] USDC balance fetched: %.4f USD (cache_age=%.3fs)",
                        balance, self._usdc_cache.age_sec,
                    )
                else:
                    self._metrics.errors += 1
                    logging.warning("[BALANCE] USDC balance fetch returned None")
            except Exception as exc:
                self._metrics.errors += 1
                logging.warning("[BALANCE] USDC balance fetch failed: %s", exc)
                balance = None

            latency_ms = (time.perf_counter() - start_time) * 1000
            self._metrics.record_latency(latency_ms)
            return balance

    def get_cached_usdc_balance(self) -> Optional[float]:
        """Return cached USDC balance without blocking.
        
        This is a non-blocking read-only method for the main loop.
        Returns None if cache is stale or not yet populated.
        Use this instead of get_usdc_balance() in the critical path.
        
        Returns:
            Cached USDC balance in USD, or None if cache is stale/empty
        """
        with self._lock:
            if self._usdc_cache is not None and self._usdc_cache.is_fresh(self._max_age_sec):
                return self._usdc_cache.value
            return None

    def get_cached_conditional_balance(self, token_id: str) -> Optional[float]:
        """Return cached conditional balance without blocking.
        
        This is a non-blocking read-only method for the main loop.
        Returns None if cache is stale or not yet populated.
        
        Args:
            token_id: The conditional token ID to look up
            
        Returns:
            Cached conditional token balance, or None if cache is stale/empty
        """
        with self._lock:
            cache_entry = self._conditional_caches.get(token_id)
            if cache_entry is not None and cache_entry.is_fresh(self._conditional_max_age_sec):
                return cache_entry.value
            return None
    
    def get_conditional_balance(self, token_id: str) -> Optional[float]:
        """Get conditional token balance from cache or fetch if stale.
        
        Args:
            token_id: The conditional token ID to fetch balance for
            
        Returns:
            Conditional token balance in shares, or None if fetch fails
        """
        start_time = time.perf_counter()
        with self._lock:
            self._metrics.fetches_total += 1

            cache_entry = self._conditional_caches.get(token_id)
            if cache_entry is not None and cache_entry.is_fresh(self._conditional_max_age_sec):
                self._metrics.cache_hits += 1
                latency_ms = (time.perf_counter() - start_time) * 1000
                self._metrics.record_latency(latency_ms)
                return cache_entry.value

            try:
                balance = self._conditional_fetcher(token_id)
                if balance is not None:
                    self._conditional_caches[token_id] = BalanceCacheEntry(
                        value=balance,
                        timestamp=time.time(),
                        token_id=token_id,
                    )
                    self._trim_conditional_locked()
                    self._metrics.http_fallbacks += 1
                    logging.debug(
                        "[BALANCE] Conditional balance fetched for %s: %.4f shares (cache_age=%.3fs)",
                        token_id[:20],
                        balance,
                        self._conditional_caches[token_id].age_sec,
                    )
                else:
                    self._metrics.errors += 1
                    logging.warning(
                        "[BALANCE] Conditional balance fetch returned None for %s",
                        token_id[:20],
                    )
            except Exception as exc:
                self._metrics.errors += 1
                logging.warning(
                    "[BALANCE] Conditional balance fetch failed for %s: %s",
                    token_id[:20],
                    exc,
                )
                balance = None

            latency_ms = (time.perf_counter() - start_time) * 1000
            self._metrics.record_latency(latency_ms)
            return balance
    
    def clear_conditional_cache(self, token_id: str) -> None:
        """Clear conditional balance cache for a specific token.
        
        Args:
            token_id: The conditional token ID to clear
        """
        with self._lock:
            if token_id in self._conditional_caches:
                del self._conditional_caches[token_id]
                logging.debug("[BALANCE] Cleared conditional cache for %s", token_id[:20])

    def clear_all(self) -> None:
        """Clear all cached balances."""
        with self._lock:
            self._usdc_cache = None
            self._conditional_caches.clear()
        logging.debug("[BALANCE] Cleared all balance caches")
    
    def get_metrics(self) -> dict[str, Any]:
        """Get current balance cache metrics."""
        with self._lock:
            metrics = self._metrics.to_dict()
            metrics["usdc_cache_age_sec"] = (
                self._usdc_cache.age_sec if self._usdc_cache else None
            )
            metrics["conditional_cache_count"] = len(self._conditional_caches)
            return metrics
    
    def log_metrics(self, reason: str = "periodic") -> None:
        """Log current metrics if enough time has passed since last log."""
        now = time.time()
        with self._lock:
            if now - self._last_log_time < self._log_interval:
                return
            metrics = self._metrics.to_dict()
            metrics["usdc_cache_age_sec"] = (
                self._usdc_cache.age_sec if self._usdc_cache else None
            )
            metrics["conditional_cache_count"] = len(self._conditional_caches)
            self._last_log_time = now
    
        logging.info(
            "[BALANCE_METRICS] %s: fetches=%d hits=%d fallbacks=%d errors=%d "
            "hit_rate=%.1f%% avg_latency=%.2fms min=%.2fms max=%.2fms "
            "usdc_age=%.2fs cond_cache_count=%d",
            reason,
            metrics["fetches_total"],
            metrics["cache_hits"],
            metrics["http_fallbacks"],
            metrics["errors"],
            metrics["hit_rate_pct"],
            metrics["avg_latency_ms"],
            metrics["min_latency_ms"],
            metrics["max_latency_ms"],
            metrics["usdc_cache_age_sec"] or 0.0,
            metrics["conditional_cache_count"],
        )

    def reset_metrics(self) -> None:
        """Reset all metrics counters."""
        with self._lock:
            self._metrics = BalanceMetrics()
            self._last_log_time = time.time()


class BalanceCacheProvider:
    """Provider for creating and managing balance caches.
    
    This class handles the integration with LiveExecutionEngine
    and provides a clean interface for balance caching.
    """
    
    @staticmethod
    def create_from_engine(
        live_engine: Any,
        max_age_sec: float = 5.0,
        conditional_max_age_sec: float = 10.0,
    ) -> BalanceCache:
        """Create a BalanceCache from a LiveExecutionEngine instance.
        
        Args:
            live_engine: LiveExecutionEngine instance with fetch_usdc_balance
                and fetch_conditional_balance methods
            max_age_sec: Maximum cache age for USDC balance
            conditional_max_age_sec: Maximum cache age for conditional balances
            
        Returns:
            Configured BalanceCache instance
        """
        def usdc_fetcher() -> Optional[float]:
            return live_engine.fetch_usdc_balance()
        
        def conditional_fetcher(token_id: str) -> Optional[float]:
            return live_engine.fetch_conditional_balance(token_id)
        
        return BalanceCache(
            balance_fetcher=usdc_fetcher,
            conditional_balance_fetcher=conditional_fetcher,
            max_age_sec=max_age_sec,
            conditional_max_age_sec=conditional_max_age_sec,
        )


@dataclass
class AllowanceCacheEntry:
    """Cached allowance entry with expiration tracking."""
    allowance: float
    expires_at: float
    last_refresh: float

    @property
    def is_expired(self) -> bool:
        """Return True if cache entry has expired."""
        return time.time() > self.expires_at

    @property
    def age_sec(self) -> float:
        """Return cache age in seconds."""
        return time.time() - self.last_refresh


class ConditionalAllowanceCache:
    """Thread-safe TTL-based cache for conditional token allowances.

    Eliminates blocking API calls from the critical path by caching
    allowance values with expiration and supporting pre-emptive
    background refresh during BUY hold periods.

    Cache structure: {token_id: AllowanceCacheEntry}
    Default TTL: 300 seconds (5 minutes) - configurable via ALLOWANCE_CACHE_TTL_SEC
    """

    def __init__(self, ttl_sec: float | None = None) -> None:
        """Initialize allowance cache.

        Args:
            ttl_sec: Time-to-live for cache entries in seconds.
                     Defaults to ALLOWANCE_CACHE_TTL_SEC env var or 300s.
        """
        self._ttl_sec = ttl_sec if ttl_sec is not None else float(
            os.getenv("ALLOWANCE_CACHE_TTL_SEC", "300")
        )
        self._cache: dict[str, AllowanceCacheEntry] = {}
        self._lock = threading.Lock()
        self._refresh_queue: list[str] = []
        self._metrics = {
            "hits": 0,
            "misses": 0,
            "stale_reads": 0,
            "refreshes": 0,
            "batch_refreshes": 0,
        }

    def get_cached_allowance(self, token_id: str) -> float | None:
        """Return cached allowance if not expired, None otherwise.

        Args:
            token_id: The conditional token ID to look up

        Returns:
            Cached allowance value, or None if expired/missing
        """
        with self._lock:
            entry = self._cache.get(token_id)
            if entry is not None and not entry.is_expired:
                self._metrics["hits"] += 1
                return entry.allowance
            if entry is not None:
                # Expired but we have a value — return it as stale read
                # (slightly stale is better than blocking)
                self._metrics["stale_reads"] += 1
                return entry.allowance
            self._metrics["misses"] += 1
            return None

    def set_allowance(self, token_id: str, allowance: float) -> None:
        """Store allowance with expiration timestamp.

        Args:
            token_id: The conditional token ID
            allowance: The allowance value to cache
        """
        now = time.time()
        with self._lock:
            self._cache[token_id] = AllowanceCacheEntry(
                allowance=allowance,
                expires_at=now + self._ttl_sec,
                last_refresh=now,
            )

    def schedule_refresh(self, token_id: str) -> None:
        """Queue a token_id for background allowance refresh.

        Called when entering a position to pre-emptively refresh
        allowance for the opposite token during the BUY hold period.

        Args:
            token_id: The conditional token ID to refresh
        """
        with self._lock:
            if token_id not in self._refresh_queue:
                self._refresh_queue.append(token_id)

    def get_refresh_queue(self) -> list[str]:
        """Return and clear the refresh queue.

        Returns:
            List of token_ids pending refresh
        """
        with self._lock:
            queue = list(self._refresh_queue)
            self._refresh_queue.clear()
            return queue

    def batch_set_allowances(self, allowances: dict[str, float]) -> None:
        """Store multiple allowances at once.

        Args:
            allowances: Dict of {token_id: allowance}
        """
        now = time.time()
        with self._lock:
            for token_id, allowance in allowances.items():
                self._cache[token_id] = AllowanceCacheEntry(
                    allowance=allowance,
                    expires_at=now + self._ttl_sec,
                    last_refresh=now,
                )

    def clear(self, token_id: str | None = None) -> None:
        """Clear cache for a specific token or all tokens.

        Args:
            token_id: If provided, clear only this token's cache
        """
        with self._lock:
            if token_id is not None:
                self._cache.pop(token_id, None)
            else:
                self._cache.clear()

    def get_metrics(self) -> dict[str, int]:
        """Return cache metrics."""
        with self._lock:
            return dict(self._metrics)

    def record_refresh(self) -> None:
        """Record a successful refresh operation."""
        with self._lock:
            self._metrics["refreshes"] += 1

    def record_batch_refresh(self) -> None:
        """Record a batch refresh operation."""
        with self._lock:
            self._metrics["batch_refreshes"] += 1
