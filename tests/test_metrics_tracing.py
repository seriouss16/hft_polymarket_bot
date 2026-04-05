"""Unit tests for metrics and tracing instrumentation."""

import time
import pytest
from core.live_common import TrackedOrder, OrderStatus
from utils.stats import _stats_from_realized_pnls
from utils.metrics_registry import MetricsRegistry


def test_tracked_order_timestamps():
    """Verify TrackedOrder can store and retrieve lifecycle timestamps."""
    order = TrackedOrder(
        order_id="test-order",
        token_id="token-123",
        side="BUY",
        price=0.5,
        size=10.0
    )
    
    now = time.time()
    order.signal_ts = now - 0.1
    order.send_ts = now - 0.05
    order.ack_ts = now - 0.02
    order.fill_ts = now
    order.exit_ts = now
    
    assert order.signal_ts < order.send_ts
    assert order.send_ts < order.ack_ts
    assert order.ack_ts < order.fill_ts
    assert order.fill_ts == order.exit_ts


def test_sharpe_ratio_calculation():
    """Verify Sharpe Ratio calculation logic."""
    # Case 1: Consistent wins
    pnls = [0.1, 0.12, 0.08, 0.11, 0.09]
    js = _stats_from_realized_pnls(pnls)
    assert js.sharpe_ratio > 0
    
    # Case 2: Mixed results
    pnls = [0.1, -0.05, 0.08, -0.02, 0.15]
    js = _stats_from_realized_pnls(pnls)
    assert js.sharpe_ratio > 0
    
    # Case 3: Consistent losses
    pnls = [-0.1, -0.12, -0.08, -0.11, -0.09]
    js = _stats_from_realized_pnls(pnls)
    assert js.sharpe_ratio < 0
    
    # Case 4: Zero variance (should return 0.0 to avoid div by zero)
    pnls = [0.1, 0.1, 0.1]
    js = _stats_from_realized_pnls(pnls)
    assert js.sharpe_ratio == 0.0
    
    # Case 5: Single trade (should return 0.0)
    pnls = [0.1]
    js = _stats_from_realized_pnls(pnls)
    assert js.sharpe_ratio == 0.0


def test_metrics_registry_snapshot():
    """Verify MetricsRegistry can generate a snapshot."""
    class MockPnL:
        total_pnl = 100.0
        trades_count = 10
        wins = 7
        max_drawdown = 0.05
        closed_trade_pnls = [10.0] * 10
        
    class MockAggregator:
        def get_latency_stats(self, exchange):
            return {"p50": 5.0, "p95": 15.0, "p99": 25.0}
            
    registry = MetricsRegistry()
    registry.configure(
        pnl_tracker=MockPnL(),
        aggregator=MockAggregator()
    )
    
    snapshot = registry.get_snapshot()
    assert snapshot.pnl_total == 100.0
    assert snapshot.win_rate == 70.0
    assert snapshot.latency_p50 == 5.0
    assert snapshot.latency_p95 == 15.0
    assert snapshot.latency_p99 == 25.0
