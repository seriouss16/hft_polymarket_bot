import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from utils.resilience import CircuitBreaker, CircuitBreakerError, CircuitState


@pytest.mark.asyncio
async def test_circuit_breaker_state_transitions():
    cb = CircuitBreaker(name="test", error_threshold=2, recovery_timeout=0.1)

    assert cb.state == CircuitState.CLOSED

    async def failing_func():
        raise ValueError("fail")

    async def success_func():
        return "ok"

    # First failure
    with pytest.raises(ValueError):
        await cb.call(failing_func)
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 1

    # Second failure -> OPEN
    with pytest.raises(ValueError):
        await cb.call(failing_func)
    assert cb.state == CircuitState.OPEN
    assert cb.failure_count == 2

    # Call while OPEN -> CircuitBreakerError
    with pytest.raises(CircuitBreakerError):
        await cb.call(success_func)

    # Wait for recovery timeout
    await asyncio.sleep(0.15)

    # Next call should transition to HALF_OPEN then CLOSED on success
    result = await cb.call(success_func)
    assert result == "ok"
    assert cb.state == CircuitState.CLOSED
    assert cb.failure_count == 0


@pytest.mark.asyncio
async def test_circuit_breaker_half_open_to_open():
    cb = CircuitBreaker(name="test", error_threshold=1, recovery_timeout=0.1)

    async def failing_func():
        raise ValueError("fail")

    # Failure -> OPEN
    with pytest.raises(ValueError):
        await cb.call(failing_func)
    assert cb.state == CircuitState.OPEN

    # Wait for recovery timeout
    await asyncio.sleep(0.15)

    # Call while HALF_OPEN fails -> back to OPEN
    with pytest.raises(ValueError):
        await cb.call(failing_func)
    assert cb.state == CircuitState.OPEN
    assert cb.failure_count == 2
