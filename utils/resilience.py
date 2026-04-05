"""Resilience utilities for HFT bot: safe_task decorator and TaskMonitor."""

from __future__ import annotations

import asyncio
import logging
import time
import traceback
from typing import Any, Callable, Coroutine, Optional
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TaskMetrics:
    """Metrics for a single background task."""
    name: str
    start_time: float = 0.0
    last_run: float = 0.0
    total_runs: int = 0
    total_errors: int = 0
    last_error: Optional[str] = None
    last_error_time: float = 0.0
    is_running: bool = False


class TaskMonitor:
    """Monitors health of background tasks and provides alerting capabilities."""

    def __init__(self) -> None:
        self._tasks: dict[str, TaskMetrics] = {}
        self._lock = asyncio.Lock()
        self._alert_callbacks: list[Callable[[str, str], None]] = []
        self._stall_threshold_sec: float = 60.0
        self._error_rate_threshold: float = 0.5

    def register_task(self, name: str) -> TaskMetrics:
        """Register a task for monitoring."""
        metrics = TaskMetrics(name=name)
        self._tasks[name] = metrics
        return metrics

    def unregister_task(self, name: str) -> None:
        """Remove a task from monitoring."""
        self._tasks.pop(name, None)

    def get_metrics(self, name: str) -> Optional[TaskMetrics]:
        """Get metrics for a specific task."""
        return self._tasks.get(name)

    def get_all_metrics(self) -> dict[str, TaskMetrics]:
        """Get metrics for all registered tasks."""
        return self._tasks.copy()

    def add_alert_callback(self, callback: Callable[[str, str], None]) -> None:
        """Add a callback to be invoked on task failures or stalls."""
        self._alert_callbacks.append(callback)

    async def mark_task_start(self, name: str) -> None:
        """Mark that a task has started running."""
        async with self._lock:
            if metrics := self._tasks.get(name):
                metrics.start_time = time.time()
                metrics.is_running = True

    async def mark_task_end(self, name: str, success: bool = True) -> None:
        """Mark that a task has completed."""
        async with self._lock:
            if metrics := self._tasks.get(name):
                metrics.is_running = False
                metrics.last_run = time.time()
                metrics.total_runs += 1
                if not success:
                    metrics.total_errors += 1

    async def mark_task_error(self, name: str, error: str, trigger_alerts: bool = True) -> None:
        """Record an error for a task and mark the run as completed.
        
        Args:
            name: Task name
            error: Error message
            trigger_alerts: Whether to trigger alert callbacks
        """
        async with self._lock:
            if metrics := self._tasks.get(name):
                metrics.total_runs += 1
                metrics.total_errors += 1
                metrics.last_error = error
                metrics.last_error_time = time.time()
                metrics.is_running = False
                if trigger_alerts:
                    for callback in self._alert_callbacks:
                        try:
                            callback(name, f"Task error: {error}")
                        except Exception as e:
                            logger.warning("Alert callback failed: %s", e)

    async def check_stalled_tasks(self) -> list[str]:
        """Check for tasks that appear stalled (running but no recent completion)."""
        now = time.time()
        stalled = []
        async with self._lock:
            for name, metrics in self._tasks.items():
                if metrics.is_running and now - metrics.start_time > self._stall_threshold_sec:
                    stalled.append(name)
        return stalled

    async def check_error_rates(self) -> list[str]:
        """Check for tasks with high error rates."""
        high_error = []
        async with self._lock:
            for name, metrics in self._tasks.items():
                if metrics.total_runs >= 10:
                    error_rate = metrics.total_errors / metrics.total_runs
                    if error_rate > self._error_rate_threshold:
                        high_error.append(name)
        return high_error

    def set_stall_threshold(self, seconds: float) -> None:
        """Set the threshold for considering a task stalled."""
        self._stall_threshold_sec = seconds

    def set_error_rate_threshold(self, rate: float) -> None:
        """Set the error rate threshold (0.0-1.0)."""
        self._error_rate_threshold = max(0.0, min(1.0, rate))


# Global monitor instance
_global_monitor: TaskMonitor | None = None


def get_monitor() -> TaskMonitor:
    """Get or create the global TaskMonitor instance."""
    global _global_monitor
    if _global_monitor is None:
        _global_monitor = TaskMonitor()
    return _global_monitor


def safe_task(
    coro_func: Optional[Callable[..., Coroutine[Any, Any, Any]]] = None,
    *,
    monitor: Optional[TaskMonitor] = None,
    task_name: Optional[str] = None,
    alert_on_error: bool = True,
    log_level: int = logging.ERROR,
) -> Callable[..., Coroutine[Any, Any, Any]]:
    """
    Decorator/wrapper for async coroutines to catch and log exceptions.

    Ensures any exception in a coroutine is caught, logged with full traceback,
    and optionally triggers a callback or alert via TaskMonitor.

    Args:
        coro_func: The async function to wrap (used as decorator)
        monitor: TaskMonitor instance to use (defaults to global)
        task_name: Name to register the task with (defaults to function name)
        alert_on_error: Whether to trigger alert callbacks on error
        log_level: Logging level for errors

    Returns:
        Wrapped coroutine function
    """
    def decorator(func: Callable[..., Coroutine[Any, Any, Any]]) -> Callable[..., Coroutine[Any, Any, Any]]:
        name = task_name or func.__name__
        mon = monitor or get_monitor()
        metrics = mon.register_task(name)

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            await mon.mark_task_start(name)
            try:
                result = await func(*args, **kwargs)
                await mon.mark_task_end(name, success=True)
                return result
            except asyncio.CancelledError:
                metrics.is_running = False
                raise
            except Exception as e:
                metrics.is_running = False
                error_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                logger.log(log_level, "Task '%s' failed: %s", name, error_msg, exc_info=False)
                # Record error and optionally alert
                await mon.mark_task_error(name, error_msg, trigger_alerts=alert_on_error)
                return None

        # Preserve metadata
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        wrapper.__qualname__ = func.__qualname__
        wrapper.__module__ = func.__module__

        return wrapper

    if coro_func is not None:
        return decorator(coro_func)
    else:
        return decorator


def wrap_existing_task(
    task: asyncio.Task,
    name: str,
    monitor: Optional[TaskMonitor] = None,
) -> asyncio.Task:
    """
    Wrap an existing asyncio.Task to catch and log its exceptions.

    This function adds a done callback to the task to update metrics.
    It does NOT modify the task's exception handling; exceptions will still
    propagate to whoever awaits the task. For new tasks, use @safe_task.

    Args:
        task: The asyncio task to monitor
        name: Name for monitoring
        monitor: TaskMonitor instance (defaults to global)

    Returns:
        The same task with added monitoring
    """
    mon = monitor or get_monitor()
    metrics = mon.register_task(name)
    if not task.done():
        metrics.is_running = True
        metrics.start_time = time.time()

    def _on_done(fut: asyncio.Task) -> None:
        """Callback when task completes."""
        try:
            exc = fut.exception()
            if exc is None:
                # Success
                metrics.is_running = False
                metrics.last_run = time.time()
                metrics.total_runs += 1
            else:
                # Exception (including CancelledError)
                if isinstance(exc, asyncio.CancelledError):
                    metrics.is_running = False
                    # Don't count cancellation as a run or error
                else:
                    metrics.is_running = False
                    metrics.total_errors += 1
                    metrics.last_error = f"{type(exc).__name__}: {exc}"
                    metrics.last_error_time = time.time()
                    error_msg = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
                    logger.error("Task '%s' failed: %s", name, error_msg, exc_info=False)
        except Exception as e:
            logger.warning("Error in task done callback for %s: %s", name, e)

    task.add_done_callback(_on_done)
    return task
