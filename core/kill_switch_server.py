"""Emergency kill-switch server for HFT bot.

Provides a simple HTTP endpoint to trigger immediate shutdown:
- POST /kill — sets global shutdown flag and cancels all orders

Uses aiohttp for consistency with existing async dependencies.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from core.live_engine import LiveExecutionEngine

# Global shutdown flag — set by /kill endpoint
_shutdown_requested = False

# Global reference to the LiveExecutionEngine (set by bot_main_loop.py)
_engine: "LiveExecutionEngine | None" = None


def set_engine(engine: "LiveExecutionEngine") -> None:
    """Register the LiveExecutionEngine instance for order cancellation."""
    global _engine
    _engine = engine


def is_shutdown_requested() -> bool:
    """Return True if kill-switch has been activated."""
    return _shutdown_requested


async def handle_kill(request: web.Request) -> web.Response:
    """Handle POST /kill — trigger emergency shutdown."""
    global _shutdown_requested
    logging.critical("🛑 KILL-SWITCH ACTIVATED via /kill endpoint")
    _shutdown_requested = True

    # Cancel all active orders if engine is available
    if _engine is not None:
        try:
            await _engine.cancel_all_orders()
            logging.critical("✅ All orders cancelled by kill-switch")
        except Exception as exc:
            logging.error("❌ Kill-switch failed to cancel orders: %s", exc, exc_info=True)
    else:
        logging.warning("⚠️ Kill-switch: no engine registered — skipping order cancellation")

    return web.json_response(
        {
            "status": "shutdown_initiated",
            "shutdown_requested": _shutdown_requested,
            "orders_cancelled": _engine is not None,
        }
    )


async def health_check(request: web.Request) -> web.Response:
    """Simple health check endpoint."""
    return web.json_response(
        {
            "status": "healthy",
            "shutdown_requested": _shutdown_requested,
        }
    )


def create_app() -> web.Application:
    """Create and configure the kill-switch web application."""
    app = web.Application()
    app.router.add_post("/kill", handle_kill)
    app.router.add_get("/health", health_check)
    return app


async def run_kill_server(host: str = "0.0.0.0", port: int = 8001) -> None:
    """Run the kill-switch server as a background task.

    This function is intended to be called from bot_main_loop.py via
    asyncio.create_task().
    """
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    logging.info("🛡️ Kill-switch server listening on http://%s:%d", host, port)
    await site.start()

    # Keep running until shutdown is requested
    while not is_shutdown_requested():
        await asyncio.sleep(1.0)

    logging.info("🛡️ Kill-switch server shutting down...")
    await runner.cleanup()
    logging.info("🛡️ Kill-switch server stopped")
