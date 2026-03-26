"""Track one active live CLOB position and manage open/close actions."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.live_engine import LiveExecutionEngine


@dataclass
class LivePositionState:
    """Store current live position in outcome shares."""

    token_id: str = ""
    signal: str = ""
    size: float = 0.0

    @property
    def is_open(self) -> bool:
        """Return True when a live position is currently open."""
        return bool(self.token_id and self.signal and self.size > 0.0)


class LivePositionManager:
    """Coordinate entry and exit orders for a single active outcome position."""

    def __init__(self, engine: LiveExecutionEngine, entry_size: float) -> None:
        """Initialize state for managing one open position at a time."""
        self.engine = engine
        self.entry_size = float(entry_size)
        self.state = LivePositionState()

    @staticmethod
    def _is_opposite_signal(existing_signal: str, new_signal: str) -> bool:
        """Return True when new entry signal should flip current side."""
        return (
            (existing_signal == "BUY_UP" and new_signal == "BUY_DOWN")
            or (existing_signal == "BUY_DOWN" and new_signal == "BUY_UP")
        )

    async def open_or_flip(self, signal: str, token_id: str) -> None:
        """Open new position or flip existing one when direction changes."""
        if signal not in {"BUY_UP", "BUY_DOWN"} or not token_id:
            return
        if self.state.is_open:
            if self.state.signal == signal and self.state.token_id == token_id:
                return
            if self._is_opposite_signal(self.state.signal, signal):
                closed = await self.engine.close_position(self.state.token_id, self.state.size)
                if closed:
                    logging.info(
                        "Closed previous position before flip: %s token=%s size=%.2f.",
                        self.state.signal,
                        self.state.token_id,
                        self.state.size,
                    )
                    self.state = LivePositionState()
                else:
                    logging.warning("Flip blocked: failed to close existing position.")
                    return
            else:
                return
        opened = await self.engine.execute(signal, token_id)
        if opened:
            self.state = LivePositionState(
                token_id=token_id,
                signal=signal,
                size=self.entry_size,
            )
            logging.info("Opened live position: %s token=%s size=%.2f.", signal, token_id, self.entry_size)

    async def close_if_open(self, reason: str = "signal") -> None:
        """Close the currently open position, if any."""
        if not self.state.is_open:
            return
        closed = await self.engine.close_position(self.state.token_id, self.state.size)
        if closed:
            logging.info(
                "Closed live position: %s token=%s size=%.2f reason=%s.",
                self.state.signal,
                self.state.token_id,
                self.state.size,
                reason,
            )
            self.state = LivePositionState()
        else:
            logging.warning("Close failed for token=%s reason=%s.", self.state.token_id, reason)

