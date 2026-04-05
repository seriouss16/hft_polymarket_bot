"""Centralized metrics registry for HFT bot.

Exposes key performance indicators (PnL, Win Rate, Sharpe, Latency)
in a format suitable for monitoring and reporting.
"""

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class MetricsSnapshot:
    """Snapshot of current bot metrics."""

    timestamp: float = field(default_factory=time.time)
    pnl_total: float = 0.0
    win_rate: float = 0.0
    trades_count: int = 0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0

    # Latency metrics (ms)
    latency_p50: float = 0.0
    latency_p95: float = 0.0
    latency_p99: float = 0.0

    # WebSocket metrics
    ws_events_received: int = 0
    http_fallbacks: int = 0

    # System metrics
    uptime_sec: float = 0.0


class MetricsRegistry:
    """Centralized registry for all bot metrics."""

    def __init__(self):
        self._start_time = time.time()
        self._pnl_tracker = None
        self._aggregator = None
        self._live_engine = None
        self._stats_collector = None

    def configure(
        self, pnl_tracker: Any = None, aggregator: Any = None, live_engine: Any = None, stats_collector: Any = None
    ):
        """Wire up components to the registry."""
        if pnl_tracker:
            self._pnl_tracker = pnl_tracker
        if aggregator:
            self._aggregator = aggregator
        if live_engine:
            self._live_engine = live_engine
        if stats_collector:
            self._stats_collector = stats_collector

    def get_snapshot(self) -> MetricsSnapshot:
        """Generate a fresh snapshot of all metrics."""
        snapshot = MetricsSnapshot(uptime_sec=time.time() - self._start_time)

        if self._pnl_tracker:
            snapshot.pnl_total = float(getattr(self._pnl_tracker, "total_pnl", 0.0))
            snapshot.trades_count = int(getattr(self._pnl_tracker, "trades_count", 0))
            wins = int(getattr(self._pnl_tracker, "wins", 0))
            if snapshot.trades_count > 0:
                snapshot.win_rate = (wins / snapshot.trades_count) * 100.0
            snapshot.max_drawdown = float(getattr(self._pnl_tracker, "max_drawdown", 0.0))

        # Sharpe Ratio from stats_collector or calculated from closed_trade_pnls
        if self._pnl_tracker and hasattr(self._pnl_tracker, "closed_trade_pnls"):
            from utils.stats import _stats_from_realized_pnls

            pnls = self._pnl_tracker.closed_trade_pnls
            if pnls:
                js = _stats_from_realized_pnls(pnls)
                snapshot.sharpe_ratio = js.sharpe_ratio

        if self._aggregator:
            # Use coinbase as primary latency source for now
            stats = self._aggregator.get_latency_stats("coinbase")
            snapshot.latency_p50 = stats.get("p50", 0.0)
            snapshot.latency_p95 = stats.get("p95", 0.0)
            snapshot.latency_p99 = stats.get("p99", 0.0)

        if self._live_engine:
            ws_metrics = getattr(self._live_engine, "_ws_metrics", {})
            snapshot.ws_events_received = ws_metrics.get("ws_events_received", 0)
            http_metrics = getattr(self._live_engine, "_http_metrics", {})
            snapshot.http_fallbacks = http_metrics.get("http_fallbacks_total", 0)

        return snapshot

    def to_json(self) -> str:
        """Return metrics as a JSON string."""
        return json.dumps(asdict(self.get_snapshot()), indent=2)

    def to_prometheus(self) -> str:
        """Return metrics in Prometheus-compatible format."""
        snap = self.get_snapshot()
        lines = [
            f"hft_pnl_total {snap.pnl_total}",
            f"hft_win_rate {snap.win_rate}",
            f"hft_trades_total {snap.trades_count}",
            f"hft_sharpe_ratio {snap.sharpe_ratio}",
            f"hft_max_drawdown {snap.max_drawdown}",
            f"hft_latency_ms_p50 {snap.latency_p50}",
            f"hft_latency_ms_p95 {snap.latency_p95}",
            f"hft_latency_ms_p99 {snap.latency_p99}",
            f"hft_ws_events_total {snap.ws_events_received}",
            f"hft_http_fallbacks_total {snap.http_fallbacks}",
            f"hft_uptime_seconds {snap.uptime_sec}",
        ]
        return "\n".join(lines) + "\n"


# Global registry instance
registry = MetricsRegistry()
