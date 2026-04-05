"""Tests for utils.resilience module: safe_task decorator and TaskMonitor."""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest

from utils.resilience import (TaskMetrics, TaskMonitor, get_monitor, safe_task,
                              wrap_existing_task)


class TestTaskMonitor:
    """Test suite for TaskMonitor class."""

    @pytest.fixture
    def monitor(self) -> TaskMonitor:
        """Create a fresh TaskMonitor for each test."""
        return TaskMonitor()

    def test_register_task(self, monitor: TaskMonitor) -> None:
        """Test task registration creates metrics."""
        metrics = monitor.register_task("test_task")
        assert metrics.name == "test_task"
        assert monitor.get_metrics("test_task") is metrics

    def test_unregister_task(self, monitor: TaskMonitor) -> None:
        """Test task unregistration removes metrics."""
        monitor.register_task("test_task")
        monitor.unregister_task("test_task")
        assert monitor.get_metrics("test_task") is None

    def test_get_all_metrics(self, monitor: TaskMonitor) -> None:
        """Test retrieving all metrics returns a copy."""
        monitor.register_task("task1")
        monitor.register_task("task2")
        all_metrics = monitor.get_all_metrics()
        assert set(all_metrics.keys()) == {"task1", "task2"}
        # Ensure it's a copy - modify the copy and ensure original unchanged
        all_metrics["new_task"] = TaskMetrics(name="new_task")
        assert "new_task" not in monitor.get_all_metrics()

    @pytest.mark.asyncio
    async def test_mark_task_start(self, monitor: TaskMonitor) -> None:
        """Test marking task start updates metrics."""
        metrics = monitor.register_task("test_task")
        await monitor.mark_task_start("test_task")
        assert metrics.is_running is True
        assert metrics.start_time > 0

    @pytest.mark.asyncio
    async def test_mark_task_end_success(self, monitor: TaskMonitor) -> None:
        """Test marking successful task completion."""
        metrics = monitor.register_task("test_task")
        await monitor.mark_task_start("test_task")
        await monitor.mark_task_end("test_task", success=True)
        assert metrics.is_running is False
        assert metrics.last_run > 0
        assert metrics.total_runs == 1
        assert metrics.total_errors == 0

    @pytest.mark.asyncio
    async def test_mark_task_end_failure(self, monitor: TaskMonitor) -> None:
        """Test marking failed task completion."""
        metrics = monitor.register_task("test_task")
        await monitor.mark_task_start("test_task")
        await monitor.mark_task_end("test_task", success=False)
        assert metrics.is_running is False
        assert metrics.total_runs == 1
        assert metrics.total_errors == 1

    @pytest.mark.asyncio
    async def test_mark_task_error(self, monitor: TaskMonitor) -> None:
        """Test recording an error for a task."""
        metrics = monitor.register_task("test_task")
        await monitor.mark_task_error("test_task", "Test error")
        assert metrics.total_errors == 1
        assert metrics.last_error == "Test error"
        assert metrics.last_error_time > 0
        assert metrics.is_running is False

    @pytest.mark.asyncio
    async def test_alert_callbacks(self, monitor: TaskMonitor) -> None:
        """Test alert callbacks are invoked on errors."""
        callback = Mock()
        monitor.add_alert_callback(callback)
        monitor.register_task("test_task")
        await monitor.mark_task_error("test_task", "Error occurred")
        callback.assert_called_once_with("test_task", "Task error: Error occurred")

    @pytest.mark.asyncio
    async def test_check_stalled_tasks(self, monitor: TaskMonitor) -> None:
        """Test detection of stalled tasks."""
        monitor.set_stall_threshold(0.01)  # Very short threshold
        metrics = monitor.register_task("stalled_task")
        await monitor.mark_task_start("stalled_task")
        # Wait longer than threshold
        await asyncio.sleep(0.05)
        stalled = await monitor.check_stalled_tasks()
        assert "stalled_task" in stalled

    @pytest.mark.asyncio
    async def test_check_error_rates(self, monitor: TaskMonitor) -> None:
        """Test detection of high error rate tasks."""
        monitor.set_error_rate_threshold(0.3)
        metrics = monitor.register_task("error_task")
        # Simulate 10 runs with 4 errors (40% error rate)
        for i in range(10):
            await monitor.mark_task_start("error_task")
            success = i >= 6  # 6 successes, 4 failures
            if success:
                await monitor.mark_task_end("error_task", success=True)
            else:
                await monitor.mark_task_error("error_task", "test error")
        high_error = await monitor.check_error_rates()
        assert "error_task" in high_error

    def test_threshold_setters(self, monitor: TaskMonitor) -> None:
        """Test setting threshold values."""
        monitor.set_stall_threshold(120.0)
        assert monitor._stall_threshold_sec == 120.0
        monitor.set_error_rate_threshold(0.75)
        assert monitor._error_rate_threshold == 0.75

        # Test clamping
        monitor.set_error_rate_threshold(1.5)
        assert monitor._error_rate_threshold == 1.0
        monitor.set_error_rate_threshold(-0.1)
        assert monitor._error_rate_threshold == 0.0


class TestSafeTask:
    """Test suite for safe_task decorator."""

    @pytest.fixture
    def monitor(self) -> TaskMonitor:
        """Create a fresh TaskMonitor for each test."""
        return TaskMonitor()

    @pytest.mark.asyncio
    async def test_successful_task(self, monitor: TaskMonitor) -> None:
        """Test successful task execution without errors."""

        @safe_task(monitor=monitor, task_name="success_task")
        async def success_task() -> str:
            await asyncio.sleep(0.01)
            return "success"

        result = await success_task()
        assert result == "success"
        metrics = monitor.get_metrics("success_task")
        assert metrics is not None
        assert metrics.total_runs == 1
        assert metrics.total_errors == 0
        assert metrics.is_running is False

    @pytest.mark.asyncio
    async def test_exception_caught_and_logged(self, monitor: TaskMonitor) -> None:
        """Test exceptions are caught, logged, and don't propagate."""

        @safe_task(monitor=monitor, task_name="failing_task")
        async def failing_task() -> None:
            raise ValueError("Test error")

        with patch("utils.resilience.logger.log") as mock_log:
            result = await failing_task()
            assert result is None  # safe_task returns None on error
            mock_log.assert_called_once()
            # Check that error message contains task name and exception
            call_args = mock_log.call_args[0]
            assert "failing_task" in call_args[0] if isinstance(call_args[0], str) else "failing_task" in str(call_args)
            assert "Test error" in str(call_args)

        metrics = monitor.get_metrics("failing_task")
        assert metrics is not None
        assert metrics.total_errors == 1
        assert "ValueError: Test error" in metrics.last_error

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self, monitor: TaskMonitor) -> None:
        """Test that asyncio.CancelledError is re-raised (not swallowed)."""

        @safe_task(monitor=monitor, task_name="cancel_task")
        async def cancel_task() -> None:
            await asyncio.sleep(10)  # Long sleep to ensure cancellation

        task = asyncio.create_task(cancel_task())
        await asyncio.sleep(0.01)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        metrics = monitor.get_metrics("cancel_task")
        assert metrics is not None
        assert metrics.is_running is False

    @pytest.mark.asyncio
    async def test_task_name_defaults_to_function_name(self) -> None:
        """Test that task_name defaults to function __name__."""

        @safe_task
        async def my_background_task() -> None:
            pass

        monitor = get_monitor()
        metrics = monitor.get_metrics("my_background_task")
        assert metrics is not None

    @pytest.mark.asyncio
    async def test_metadata_preservation(self) -> None:
        """Test that decorator preserves function metadata."""

        @safe_task(task_name="preserved_task")
        async def documented_task() -> str:
            """This is a docstring."""
            return "result"

        assert documented_task.__name__ == "documented_task"
        assert documented_task.__doc__ == "This is a docstring."
        assert documented_task.__qualname__ == "TestSafeTask.test_metadata_preservation.<locals>.documented_task"

    @pytest.mark.asyncio
    async def test_multiple_exceptions_in_sequence(self, monitor: TaskMonitor) -> None:
        """Test multiple consecutive exceptions are all recorded."""

        @safe_task(monitor=monitor, task_name="flaky_task")
        async def flaky_task() -> None:
            raise RuntimeError("Random error")

        # Run multiple times, all failing
        for _ in range(5):
            await flaky_task()

        metrics = monitor.get_metrics("flaky_task")
        assert metrics.total_runs == 5
        assert metrics.total_errors == 5
        # Check that last_error was updated each time
        assert "RuntimeError: Random error" in metrics.last_error

    @pytest.mark.asyncio
    async def test_mixed_success_and_failure(self, monitor: TaskMonitor) -> None:
        """Test tracking of mixed success/failure runs."""
        call_count = 0

        @safe_task(monitor=monitor, task_name="mixed_task")
        async def mixed_task() -> str:
            nonlocal call_count
            call_count += 1
            if call_count % 2 == 0:
                return "success"
            else:
                raise ValueError("Even calls fail")

        results = []
        for i in range(4):
            result = await mixed_task()
            results.append(result)

        assert results == [None, "success", None, "success"]
        metrics = monitor.get_metrics("mixed_task")
        assert metrics.total_runs == 4
        assert metrics.total_errors == 2

    @pytest.mark.asyncio
    async def test_alert_on_error_flag(self, monitor: TaskMonitor) -> None:
        """Test that alert_on_error controls callback triggering."""
        callback = Mock()
        monitor.add_alert_callback(callback)

        @safe_task(monitor=monitor, task_name="alert_task", alert_on_error=True)
        async def alert_task() -> None:
            raise ValueError("Error with alert")

        await alert_task()
        callback.assert_called_once()

        # Reset and test with alert_on_error=False
        callback.reset_mock()

        @safe_task(monitor=monitor, task_name="no_alert_task", alert_on_error=False)
        async def no_alert_task() -> None:
            raise ValueError("Error without alert")

        await no_alert_task()
        callback.assert_not_called()


class TestWrapExistingTask:
    """Test suite for wrap_existing_task function."""

    @pytest.mark.asyncio
    async def test_wrap_existing_task_success(self) -> None:
        """Test wrapping an existing successful task."""
        monitor = TaskMonitor()

        async def success_coro() -> str:
            await asyncio.sleep(0.01)
            return "success"

        task = asyncio.create_task(success_coro())
        wrapped = wrap_existing_task(task, "wrapped_task", monitor)

        # The wrapped task should complete successfully
        result = await wrapped
        assert result == "success"
        assert wrapped.done()
        assert not wrapped.cancelled()

        metrics = monitor.get_metrics("wrapped_task")
        assert metrics is not None
        # total_runs should be incremented via the done callback
        await asyncio.sleep(0.01)  # Let callback complete
        assert metrics.total_runs == 1
        assert metrics.total_errors == 0

    @pytest.mark.asyncio
    async def test_wrap_existing_task_exception(self) -> None:
        """Test wrapping an existing task that raises an exception."""
        monitor = TaskMonitor()

        async def failing_coro() -> None:
            raise ValueError("Original task error")

        task = asyncio.create_task(failing_coro())
        wrapped = wrap_existing_task(task, "wrapped_failing", monitor)

        with pytest.raises(ValueError):
            await wrapped

        metrics = monitor.get_metrics("wrapped_failing")
        assert metrics is not None
        # Error should be recorded via callback
        await asyncio.sleep(0.01)
        assert metrics.total_errors == 1
        assert "ValueError: Original task error" in metrics.last_error

    @pytest.mark.asyncio
    async def test_wrap_cancels_original_task(self) -> None:
        """Test that the original task is NOT cancelled when wrapped (we don't cancel it)."""
        monitor = TaskMonitor()

        async def long_task() -> None:
            await asyncio.sleep(100)

        task = asyncio.create_task(long_task())
        wrapped = wrap_existing_task(task, "wrapped_long", monitor)

        # Original task should NOT be cancelled by wrap_existing_task
        assert not task.cancelled()
        # Cancel the wrapped task to clean up
        wrapped.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wrapped


class TestIntegration:
    """Integration tests for safe_task with asyncio event loop."""

    @pytest.mark.asyncio
    async def test_multiple_concurrent_safe_tasks(self) -> None:
        """Test multiple safe tasks running concurrently without interference."""
        monitor = TaskMonitor()

        @safe_task(monitor=monitor, task_name="concurrent_1")
        async def task1() -> int:
            await asyncio.sleep(0.05)
            return 1

        @safe_task(monitor=monitor, task_name="concurrent_2")
        async def task2() -> int:
            await asyncio.sleep(0.05)
            return 2

        @safe_task(monitor=monitor, task_name="concurrent_3")
        async def task3() -> int:
            await asyncio.sleep(0.05)
            return 3

        results = await asyncio.gather(task1(), task2(), task3())
        assert results == [1, 2, 3]

        all_metrics = monitor.get_all_metrics()
        for name in ["concurrent_1", "concurrent_2", "concurrent_3"]:
            assert all_metrics[name].total_runs == 1
            assert all_metrics[name].total_errors == 0

    @pytest.mark.asyncio
    async def test_safe_task_with_infinite_loop(self) -> None:
        """Test safe_task with a long-running infinite loop that gets cancelled."""
        monitor = TaskMonitor()
        run_count = 0

        @safe_task(monitor=monitor, task_name="infinite_loop")
        async def infinite_loop() -> None:
            nonlocal run_count
            while True:
                run_count += 1
                await asyncio.sleep(0.01)

        task = asyncio.create_task(infinite_loop())
        await asyncio.sleep(0.05)  # Let it run a few iterations
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert run_count > 0  # Task did run before cancellation
        metrics = monitor.get_metrics("infinite_loop")
        assert metrics is not None
        # Task was cancelled, not marked as error
        assert metrics.total_errors == 0

    @pytest.mark.asyncio
    async def test_exception_traceback_captured(self) -> None:
        """Test that full traceback is captured in error logs."""
        monitor = TaskMonitor()

        def nested_function() -> None:
            raise ValueError("Deep error")

        @safe_task(monitor=monitor, task_name="nested_error")
        async def nested_task() -> None:
            nested_function()

        with patch("utils.resilience.logger.log") as mock_log:
            await nested_task()
            # Check that logger.log was called with the error and traceback
            assert mock_log.called
            # logger.log is called as: logger.log(level, "Task '%s' failed: %s", name, error_msg)
            # So call_args[0] = (level, format_str, name, error_msg)
            call_args = mock_log.call_args
            if call_args[0]:
                format_str = call_args[0][1]
                name_arg = call_args[0][2]
                error_msg_arg = call_args[0][3]
                # Reconstruct the full logged message
                log_message = format_str % (name_arg, error_msg_arg)
            else:
                # Use keyword args
                log_message = call_args[1].get("msg", "") % call_args[1]

            # Check that traceback includes nested_function or test file
            assert (
                "nested_function" in log_message or "test_resilience.py" in log_message
            ), f"Traceback missing in: {log_message[:200]}"
            assert "ValueError: Deep error" in log_message

        metrics = monitor.get_metrics("nested_error")
        assert metrics.last_error is not None
        assert "ValueError: Deep error" in metrics.last_error


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
