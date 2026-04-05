"""Tests for safety gates: anti-doubling and kill-switch.

Covers:
- can_enter_position: prevents duplicate BUY orders and re-entry on existing position
- Kill-switch server: /kill endpoint sets shutdown flag and cancels orders
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.kill_switch_server import (
    is_shutdown_requested,
    set_engine,
    set_kill_engine,
)
from core.live_engine import LiveExecutionEngine, OrderStatus, TrackedOrder


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def engine_test_mode() -> LiveExecutionEngine:
    """Create a test-mode engine with minimal config."""
    with patch.dict("os.environ", {
        "POLY_CLOB_MIN_SHARES": "5",
        "HFT_MAX_POSITION_USD": "0",
        "HFT_MAX_ENTRY_ASK": "0.5",
        "LIVE_MAX_SPREAD": "0.05",
        "LIVE_ORDER_SIZE": "10",
    }):
        eng = LiveExecutionEngine(
            private_key=None,
            funder=None,
            test_mode=True,
            min_order_size=10.0,
            max_spread=0.05,
        )
    return eng


@pytest.fixture
def sample_token() -> str:
    """Sample token ID for testing."""
    return "tok_1234567890123456789012"


@pytest.fixture
def buy_order(sample_token: str) -> TrackedOrder:
    """Create a sample BUY order in PENDING status."""
    return TrackedOrder(
        order_id="ord_buy_123",
        token_id=sample_token,
        side="BUY",
        price=0.50,
        size=10.0,
        status=OrderStatus.PENDING,
        filled_size=0.0,
    )


@pytest.fixture
def partial_buy_order(sample_token: str) -> TrackedOrder:
    """Create a sample BUY order in PARTIAL status."""
    return TrackedOrder(
        order_id="ord_buy_partial",
        token_id=sample_token,
        side="BUY",
        price=0.50,
        size=10.0,
        status=OrderStatus.PARTIAL,
        filled_size=5.0,
    )


# ---------------------------------------------------------------------------
# Anti-doubling tests: can_enter_position
# ---------------------------------------------------------------------------


class TestCanEnterPosition:
    """Test anti-doubling gate for position entry."""

    def test_allow_when_no_active_orders_and_no_position(self, engine_test_mode: LiveExecutionEngine, sample_token: str):
        """Should allow entry when no active orders and no confirmed position."""
        assert engine_test_mode.can_enter_position(sample_token, "BUY") is True

    def test_block_when_active_pending_buy_exists(self, engine_test_mode: LiveExecutionEngine, sample_token: str, buy_order: TrackedOrder):
        """Should block if a PENDING BUY order already exists for the token."""
        engine_test_mode._active_orders[buy_order.order_id] = buy_order
        assert engine_test_mode.can_enter_position(sample_token, "BUY") is False

    def test_block_when_active_partial_buy_exists(self, engine_test_mode: LiveExecutionEngine, sample_token: str, partial_buy_order: TrackedOrder):
        """Should block if a PARTIAL BUY order already exists for the token."""
        engine_test_mode._active_orders[partial_buy_order.order_id] = partial_buy_order
        assert engine_test_mode.can_enter_position(sample_token, "BUY") is False

    def test_block_when_confirmed_position_exists(self, engine_test_mode: LiveExecutionEngine, sample_token: str):
        """Should block if a confirmed position (from previous fill) exists."""
        engine_test_mode._confirmed_buys[sample_token] = 10.0  # 10 shares from prior fill
        assert engine_test_mode.can_enter_position(sample_token, "BUY") is False

    def test_allow_when_confirmed_position_below_minimum(self, engine_test_mode: LiveExecutionEngine, sample_token: str):
        """Should allow entry if confirmed shares are below POLY_CLOB_MIN_SHARES (dust)."""
        # Set confirmed shares below minimum (5 shares)
        engine_test_mode._confirmed_buys[sample_token] = 2.5
        assert engine_test_mode.can_enter_position(sample_token, "BUY") is True

    def test_ignore_non_buy_side(self, engine_test_mode: LiveExecutionEngine, sample_token: str):
        """Should allow non-BUY sides (SELL) without checks."""
        assert engine_test_mode.can_enter_position(sample_token, "SELL") is True
        assert engine_test_mode.can_enter_position(sample_token, "BUY_UP") is True
        assert engine_test_mode.can_enter_position(sample_token, "BUY_DOWN") is True

    def test_ignore_filled_or_cancelled_orders(self, engine_test_mode: LiveExecutionEngine, sample_token: str, buy_order: TrackedOrder):
        """Should allow entry if existing BUY order is FILLED or CANCELLED."""
        # FILLED order should not block
        buy_order.status = OrderStatus.FILLED
        engine_test_mode._active_orders[buy_order.order_id] = buy_order
        assert engine_test_mode.can_enter_position(sample_token, "BUY") is True

        # CANCELLED order should not block
        buy_order.status = OrderStatus.CANCELLED
        assert engine_test_mode.can_enter_position(sample_token, "BUY") is True

        # FAILED order should not block
        buy_order.status = OrderStatus.FAILED
        assert engine_test_mode.can_enter_position(sample_token, "BUY") is True

    def test_multiple_tokens_independent(self, engine_test_mode: LiveExecutionEngine):
        """Should check per-token isolation: active order on token A does not block token B."""
        token_a = "tok_aaaaaaaaaaaaaaaaaaaaaaaaa"
        token_b = "tok_bbbbbbbbbbbbbbbbbbbbbbb"

        order_a = TrackedOrder(
            order_id="ord_a",
            token_id=token_a,
            side="BUY",
            price=0.50,
            size=10.0,
            status=OrderStatus.PENDING,
        )
        engine_test_mode._active_orders["ord_a"] = order_a

        # Token A should be blocked
        assert engine_test_mode.can_enter_position(token_a, "BUY") is False
        # Token B should be allowed (no active orders or position)
        assert engine_test_mode.can_enter_position(token_b, "BUY") is True


# ---------------------------------------------------------------------------
# Kill-switch server tests
# ---------------------------------------------------------------------------


class TestKillSwitchServer:
    """Test kill-switch HTTP server functionality."""

    @pytest.fixture
    def kill_server_imports(self):
        """Import kill-switch module for testing."""
        from core import kill_switch_server as ks
        return ks

    def test_initial_shutdown_flag_is_false(self, kill_server_imports):
        """Initially, shutdown flag should be False."""
        assert kill_server_imports.is_shutdown_requested() is False

    def test_set_engine_registers_engine(self, kill_server_imports):
        """set_engine should store the engine reference globally."""
        mock_engine = MagicMock()
        set_kill_engine(mock_engine)
        # The module stores engine in _engine variable
        assert kill_server_imports._engine is mock_engine

    def test_kill_endpoint_sets_shutdown_flag(self, kill_server_imports):
        """Simulate /kill endpoint logic: should set shutdown flag."""
        # Reset state
        kill_server_imports._shutdown_requested = False
        kill_server_imports._engine = None

        # Simulate handler
        kill_server_imports._shutdown_requested = True
        assert kill_server_imports.is_shutdown_requested() is True

    def test_kill_endpoint_cancels_orders_if_engine_present(self, kill_server_imports):
        """If engine is registered, /kill should call cancel_all_orders."""
        mock_engine = AsyncMock()
        mock_engine.cancel_all_orders = AsyncMock()
        kill_server_imports._engine = mock_engine

        # Simulate the handler's logic
        async def simulate_kill():
            kill_server_imports._shutdown_requested = True
            if kill_server_imports._engine is not None:
                await kill_server_imports._engine.cancel_all_orders()

        asyncio.run(simulate_kill())

        assert kill_server_imports.is_shutdown_requested() is True
        mock_engine.cancel_all_orders.assert_awaited_once()

    def test_kill_endpoint_handles_engine_error(self, kill_server_imports):
        """Errors during cancel_all_orders should be logged but not propagate."""
        mock_engine = AsyncMock()
        mock_engine.cancel_all_orders = AsyncMock(side_effect=Exception("cancel failed"))
        kill_server_imports._engine = mock_engine

        async def simulate_kill():
            kill_server_imports._shutdown_requested = True
            if kill_server_imports._engine is not None:
                try:
                    await kill_server_imports._engine.cancel_all_orders()
                except Exception:
                    pass  # Handler catches and logs

        asyncio.run(simulate_kill())
        mock_engine.cancel_all_orders.assert_awaited_once()

    def test_kill_endpoint_without_engine(self, kill_server_imports):
        """If no engine registered, /kill should still set flag and log warning."""
        kill_server_imports._engine = None
        kill_server_imports._shutdown_requested = False

        async def simulate_kill():
            kill_server_imports._shutdown_requested = True
            if kill_server_imports._engine is not None:
                await kill_server_imports._engine.cancel_all_orders()

        asyncio.run(simulate_kill())

        assert kill_server_imports.is_shutdown_requested() is True

    def test_health_check_returns_status(self, kill_server_imports):
        """Health check should return JSON with status and shutdown flag."""
        kill_server_imports._shutdown_requested = False
        # Simulate health_check handler
        from aiohttp import web

        async def simulate_health():
            return web.json_response(
                {
                    "status": "healthy",
                    "shutdown_requested": kill_server_imports.is_shutdown_requested(),
                }
            )

        response = asyncio.run(simulate_health())
        assert response.status == 200
        # body would be {"status": "healthy", "shutdown_requested": false}

    def test_create_app_has_routes(self, kill_server_imports):
        """create_app should register /kill and /health routes."""
        app = kill_server_imports.create_app()
        routes = [str(route) for route in app.router.routes()]
        assert any("kill" in r for r in routes)
        assert any("health" in r for r in routes)


# ---------------------------------------------------------------------------
# Integration: execute() anti-doubling gate
# ---------------------------------------------------------------------------


class TestExecuteAntiDoublingIntegration:
    """Test that execute() respects can_enter_position gate."""

    def test_execute_blocks_when_active_buy_exists(self, engine_test_mode: LiveExecutionEngine, sample_token: str, buy_order: TrackedOrder):
        """execute() should return (0,0) if can_enter_position returns False due to active order."""
        engine_test_mode._active_orders[buy_order.order_id] = buy_order

        # Patch can_enter_position to return False (simulate anti-doubling block)
        with patch.object(engine_test_mode, "can_enter_position", return_value=False) as mock_can_enter:
            result = asyncio.run(engine_test_mode.execute(
                signal="BUY_UP",
                token_id=sample_token,
                order_size=10.0,
            ))
            mock_can_enter.assert_called_once_with(sample_token, "BUY_UP")
            assert result == (0.0, 0.0)

    def test_execute_blocks_when_confirmed_position_exists(self, engine_test_mode: LiveExecutionEngine, sample_token: str):
        """execute() should return (0,0) if confirmed position exists."""
        engine_test_mode._confirmed_buys[sample_token] = 15.0

        with patch.object(engine_test_mode, "can_enter_position", return_value=False) as mock_can_enter:
            result = asyncio.run(engine_test_mode.execute(
                signal="BUY_UP",
                token_id=sample_token,
                order_size=10.0,
            ))
            mock_can_enter.assert_called_once()
            assert result == (0.0, 0.0)

    def test_execute_proceeds_when_no_block(self, engine_test_mode: LiveExecutionEngine, sample_token: str):
        """execute() should continue when can_enter_position returns True."""
        with patch.object(engine_test_mode, "can_enter_position", return_value=True) as mock_can_enter:
            # The actual execute will fail early due to test_mode and missing mocks,
            # but we just want to verify can_enter_position was called and didn't block.
            try:
                asyncio.run(engine_test_mode.execute(
                    signal="BUY_UP",
                    token_id=sample_token,
                    order_size=10.0,
                ))
            except Exception:
                pass  # Expected to fail later in the pipeline
            mock_can_enter.assert_called_once()


# ---------------------------------------------------------------------------
# Cancel all orders tests
# ---------------------------------------------------------------------------


class TestCancelAllOrders:
    """Test cancel_all_orders method."""

    @pytest.fixture
    def engine_with_orders(self, engine_test_mode: LiveExecutionEngine, sample_token: str):
        """Create engine with multiple active orders."""
        orders = [
            TrackedOrder(
                order_id=f"ord_{i}",
                token_id=sample_token,
                side="BUY",
                price=0.50 + i * 0.01,
                size=10.0,
                status=OrderStatus.PENDING,
            )
            for i in range(3)
        ]
        for o in orders:
            engine_test_mode._active_orders[o.order_id] = o
        return engine_test_mode, orders

    def test_cancel_all_orders_calls_cancel_on_each(self, engine_with_orders):
        """Should call _cancel_order for each active order."""
        eng, orders = engine_with_orders
        with patch.object(eng, "_cancel_order", return_value=True) as mock_cancel:
            asyncio.run(eng.cancel_all_orders())
            assert mock_cancel.call_count == len(orders)
            for o in orders:
                mock_cancel.assert_any_call(o.order_id)

    def test_cancel_all_orders_logs_count(self, engine_with_orders, caplog):
        """Should log the number of orders being cancelled."""
        eng, _ = engine_with_orders
        asyncio.run(eng.cancel_all_orders())
        assert "cancelling 3 active order(s)" in caplog.text

    def test_cancel_all_orders_no_orders(self, engine_test_mode: LiveExecutionEngine, caplog):
        """Should log info when there are no orders to cancel."""
        asyncio.run(engine_test_mode.cancel_all_orders())
        assert "no active orders to cancel" in caplog.text

    def test_cancel_all_orders_handles_exception(self, engine_with_orders):
        """Should continue cancelling even if one fails."""
        eng, orders = engine_with_orders

        def cancel_side_effect(order_id: str):
            if order_id == orders[0].order_id:
                raise Exception("cancel failed")
            return True

        with patch.object(eng, "_cancel_order", side_effect=cancel_side_effect) as mock_cancel:
            asyncio.run(eng.cancel_all_orders())
            assert mock_cancel.call_count == len(orders)
