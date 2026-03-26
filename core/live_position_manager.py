"""Track active live CLOB positions per outcome token and manage open/close actions."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.live_engine import LiveExecutionEngine


@dataclass
class LivePositionState:
    """Store one live position in outcome shares for a single token."""

    signal: str = ""
    size: float = 0.0


class LivePositionManager:
    """Coordinate entry and exit orders for multiple concurrent outcome positions."""

    def __init__(self, engine: LiveExecutionEngine, entry_size: float) -> None:
        """Initialize state keyed by outcome token id."""
        self.engine = engine
        self.entry_size = float(entry_size)
        self.positions: dict[str, LivePositionState] = {}

    async def open_or_flip(self, signal: str, token_id: str) -> None:
        """Open a position or flip direction for the same token when signals conflict."""
        if signal not in {"BUY_UP", "BUY_DOWN"} or not token_id:
            return

        if token_id in self.positions:
            if self.positions[token_id].signal == signal:
                return
            closed = await self.engine.close_position(token_id, self.positions[token_id].size)
            if closed:
                del self.positions[token_id]
            else:
                logging.warning("Flip blocked: failed to close token=%s.", token_id)
                return

        opened = await self.engine.execute(signal, token_id)
        if opened:
            self.positions[token_id] = LivePositionState(signal=signal, size=self.entry_size)
            logging.info("Opened position: %s token=%s size=%.2f", signal, token_id, self.entry_size)

    async def close_if_open(self, reason: str = "signal") -> None:
        """Close all open positions, if any."""
        for token_id in list(self.positions.keys()):
            state = self.positions[token_id]
            closed = await self.engine.close_position(token_id, state.size)
            if closed:
                logging.info(
                    "Closed position: %s token=%s reason=%s",
                    state.signal,
                    token_id,
                    reason,
                )
                del self.positions[token_id]
            else:
                logging.warning("Failed to close token=%s", token_id)
