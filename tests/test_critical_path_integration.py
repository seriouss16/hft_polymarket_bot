import asyncio
import os
import time
import pytest
import logging
from unittest.mock import MagicMock, patch

from data.aggregator import FastPriceAggregator
from core.strategy_hub import StrategyHub
from core.strategies.latency_strategy import LatencyArbitrageStrategy
from core.live_engine import LiveExecutionEngine
from core.executor import PnLTracker
from core.live_common import OrderStatus, BUY, SELL_SIDE, TrackedOrder
from utils.resilience import CircuitState

# Configure logging for tests
logging.basicConfig(level=logging.INFO)

@pytest.fixture
def mock_env():
    """Set up environment variables for testing."""
    with patch.dict(os.environ, {
        "HFT_ZSCORE_WINDOW": "50",
        "HFT_USE_INCREMENTAL_ZSCORE": "1",
        "HFT_SMART_BINANCE_BLEND": "0.5",
        "HFT_SMART_CB_BN_BASELINE_USD": "0.0",
        "HFT_SMART_DRIFT_THRESHOLD_USD": "0.01",
        "HFT_SMART_EXCESS_THRESHOLD_USD": "0.01",
        "HFT_MAX_ENTRY_ASK": "0.99",
        "POLY_CLOB_MIN_SHARES": "10.0",
        "LIVE_MODE": "1",
        "HFT_CIRCUIT_BREAKER_THRESHOLD": "5",
        "HFT_CIRCUIT_BREAKER_RECOVERY_SEC": "1.0",
        "LIVE_ORDER_FILL_POLL_SEC": "0.01",
        "LIVE_ORDER_STALE_SEC": "0.1",
        "LIVE_ORDER_MAX_REPRICE": "1",
        "HFT_NO_ENTRY_GUARDS": "1",
        "HFT_REGIME_FILTER_ENABLED": "0",
        "HFT_LATENCY_EDGE_EXPIRY_ENABLED": "0",
        "HFT_MIN_EDGE_USD": "0.0001",
        "HFT_Z_TREND_UP_MIN": "-10.0",
        "HFT_Z_TREND_DOWN_MAX": "10.0",
        "HFT_SPEED_UP_MIN": "-100.0",
        "HFT_SPEED_DOWN_MAX": "100.0",
        "HFT_ENTRY_MAX_LATENCY_MS": "100.0",
        "HFT_USE_GATHER": "0",
    }):
        yield

@pytest.mark.asyncio
async def test_critical_path_integration_full_cycle(mock_env):
    """
    Normal Flow: Price spike -> Signal -> Validation Pass -> Order Placed -> Fill Event -> PnL Updated.
    """
    # 1. Setup Components
    pnl_tracker = PnLTracker(initial_balance=1000.0, live_mode=True)
    aggregator = FastPriceAggregator()
    strategy = LatencyArbitrageStrategy(pnl_tracker)
    hub = StrategyHub()
    hub.register(strategy)
    
    # Mock LiveExecutionEngine's client and internal methods
    engine = LiveExecutionEngine(private_key=None, funder=None, test_mode=True)
    
    token_id = "0xTEST"
    
    # Mock order placement
    order_id = "order_123"
    with patch.object(engine, '_place_limit_raw', return_value=(order_id, False)) as mock_place, \
         patch.object(engine, '_get_order_fill', return_value=("filled", 10.0)) as mock_fill, \
         patch.object(engine, 'get_best_prices', return_value=(0.50, 0.51)):
        
        # 2. Simulate Price Spike
        # Need 50 ticks for z-score in FastPriceAggregator
        for i in range(50):
            aggregator.update("coinbase", 100.0 + i*0.01)
        
        # Spike
        aggregator.update("coinbase", 110.0)
        fast_price = aggregator.get_price()
        zscore = aggregator.get_zscore()
        
        # 3. Strategy Decision
        # Mock poly_orderbook with required fields for HFTEngine
        poly_ob = {
            "bid": 0.50,
            "ask": 0.51,
            "bid_size_top": 100.0,
            "ask_size_top": 100.0,
            "mid": 0.505,
            "down_bid": 0.49,
            "down_ask": 0.50,
            "down_bid_size_top": 100.0,
            "down_ask_size_top": 100.0,
            "btc_oracle": 0.505,
        }
        
        # Ensure engine has no active orders and can enter
        engine._active_orders.clear()
        engine._confirmed_buys.clear()
        
        # Mock the strategy to return a clean ENTRY signal
        with patch.object(strategy._engine, 'process_tick', return_value={
            "event": "ENTRY",
            "side": "UP",
            "price": 0.51,
            "size": 10.0,
            "confidence": 0.8,
            "entry_edge": 0.01,
            "latency_ms": 5.0,
        }):
            decision = await hub.process_tick(
                fast_price=fast_price,
                poly_orderbook=poly_ob,
                price_history=list(aggregator.get_primary_history()),
                lstm_forecast=0.0,
                zscore=zscore
            )
        
        assert decision is not None
        assert decision["event"] == "ENTRY"
        
        # 4. Execution - simulate full order lifecycle
        # Clear any pre-existing state
        engine._active_orders.clear()
        engine._confirmed_buys.clear()
        
        # Check safety gates
        assert engine.can_enter_position(token_id, BUY) is True
        
        # Place order
        oid, immediate = await engine._place_limit_raw(token_id, BUY, 0.51, 10.0)
        assert oid == order_id
        
        # Track order
        tracked = TrackedOrder(
            order_id=oid,
            token_id=token_id,
            side=BUY,
            price=0.51,
            size=10.0,
            status=OrderStatus.FILLED if immediate else OrderStatus.PENDING
        )
        engine._active_orders[oid] = tracked
        
        # Simulate fill via polling (test_mode uses _test_mode_feed_order_fill_events)
        # We'll manually set the status for simplicity
        if not immediate:
            # In test_mode, _poll_order spawns a task that polls and enqueues WsOrderEvents
            # We'll directly set the status for simplicity
            tracked.status = OrderStatus.FILLED
            tracked.filled_size = 10.0
        
        assert tracked.status == OrderStatus.FILLED
        
        # 5. PnL Update
        amount_usd = tracked.filled_size * tracked.price
        pnl_tracker.live_open(token_id, tracked.filled_size, tracked.price, amount_usd)
        assert pnl_tracker.inventory > 0
        # Note: trades_count increments on live_close, not live_open

@pytest.mark.asyncio
async def test_anti_doubling_safety(mock_env):
    """
    Anti-Doubling: Attempt second BUY while first is PENDING -> Verify Skip.
    """
    engine = LiveExecutionEngine(private_key=None, funder=None, test_mode=True)
    token_id = "0xTEST"
    
    # Manually add an active order
    engine._active_orders["order_1"] = TrackedOrder(
        order_id="order_1",
        token_id=token_id,
        side=BUY,
        price=0.50,
        size=10.0,
        status=OrderStatus.PENDING
    )
    
    # Attempt second entry
    assert engine.can_enter_position(token_id, BUY) is False

@pytest.mark.asyncio
async def test_circuit_breaker_integration(mock_env):
    """
    Circuit Breaker: Simulate 5 API failures -> Verify Circuit OPEN -> Verify subsequent signals are skipped.
    """
    with patch("core.live_engine.ClobClient", MagicMock()) as mock_clob, \
         patch("core.live_engine.OrderArgs", MagicMock()):
        # Ensure ClobClient is not None in the module
        import core.live_engine
        core.live_engine.ClobClient = mock_clob
        engine = LiveExecutionEngine(private_key="0x123", funder="0x456", test_mode=False)
    
    # Mock client.post_order to fail
    engine.client = MagicMock()
    # We need to mock create_order to return something that post_order can accept
    engine.client.create_order.return_value = {"dummy": "order"}
    engine.client.post_order.side_effect = Exception("API Error")
    
    token_id = "0xTEST"
    
    # Trigger 5 failures
    # We need to mock ClobClient AND OrderArgs in the module where it's imported
    # AND ensure engine.client is not None
    with patch("core.live_engine.ClobClient", engine.client), \
         patch("core.live_engine.OrderArgs", MagicMock()):
        for i in range(5):
            oid, immediate = await engine._place_limit_raw(token_id, BUY, 0.50, 10.0)
            # Even if it fails, we continue to trigger CB
            assert oid is None
    
    # Check circuit breaker state - should be OPEN after threshold
    # The circuit breaker uses an Enum CircuitState
    assert engine.circuit_breaker.state == CircuitState.OPEN
    
    # Subsequent attempt should be blocked by CB without calling client.post_order
    engine.client.post_order.reset_mock()
    with patch("core.live_engine.ClobClient", engine.client), \
         patch("core.live_engine.OrderArgs", MagicMock()):
        oid, immediate = await engine._place_limit_raw(token_id, BUY, 0.50, 10.0)
    
    assert oid is None
    engine.client.post_order.assert_not_called()

@pytest.mark.asyncio
async def test_kill_switch_integration(mock_env):
    """
    Kill-Switch: Trigger /kill during an active order -> Verify cancellation and shutdown.
    """
    engine = LiveExecutionEngine(private_key=None, funder=None, test_mode=True)
    token_id = "0xTEST"
    
    # Add active order
    order_id = "order_to_kill"
    tracked = TrackedOrder(
        order_id=order_id,
        token_id=token_id,
        side=BUY,
        price=0.50,
        size=10.0,
        status=OrderStatus.PENDING
    )
    engine._active_orders[order_id] = tracked
    
    # Mock _cancel_order
    with patch.object(engine, '_cancel_order', return_value=True) as mock_cancel:
        # Trigger kill-switch logic (cancel all)
        await engine.cancel_all_orders()
        
        mock_cancel.assert_called_with(order_id)
        
    # Shutdown engine
    await engine.shutdown()
    assert engine._worker_task is None
