"""Small coordinator joining ready handoffs to the shared Telegram Iris."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Protocol

from .models import ConversationHandoff
from .state import HandoffState

LOGGER = logging.getLogger(__name__)

QUIET_WINDOW_SECONDS = 120.0
HANDOFF_BATCH_LIMIT = 8
COORDINATOR_POLL_SECONDS = 1.0


class HandoffTurnTarget(Protocol):
    """The minimal shared-conversation surface needed by the coordinator."""

    @property
    def proactive_handoff_blocked(self) -> bool: ...

    async def present_handoffs(
        self, handoffs: tuple[ConversationHandoff, ...]
    ) -> None: ...


class HandoffCoordinator:
    """Batch ready context into a human or quiet-window shared turn."""

    def __init__(
        self,
        state: HandoffState,
        *,
        clock: Callable[[], float] = time.monotonic,
        quiet_window_seconds: float = QUIET_WINDOW_SECONDS,
        batch_limit: int = HANDOFF_BATCH_LIMIT,
        poll_seconds: float = COORDINATOR_POLL_SECONDS,
    ) -> None:
        if quiet_window_seconds < 0:
            raise ValueError("The handoff quiet window cannot be negative.")
        if poll_seconds <= 0:
            raise ValueError("The handoff poll interval must be positive.")
        self.state = state
        self.state.initialize()
        self._clock = clock
        self._quiet_window_seconds = quiet_window_seconds
        self._batch_limit = batch_limit
        self._poll_seconds = poll_seconds
        self._last_activity = clock()
        self._stop = asyncio.Event()

    def note_activity(self) -> None:
        """Record human or Iris conversational activity."""
        self._last_activity = self._clock()

    def claim_for_direct_turn(self) -> tuple[ConversationHandoff, ...]:
        """Give waiting context to the next direct human turn immediately."""
        return self.state.claim_ready(limit=self._batch_limit)

    def complete(self, handoffs: tuple[ConversationHandoff, ...]) -> None:
        self.state.complete(tuple(handoff.id for handoff in handoffs))

    def retry(
        self,
        handoffs: tuple[ConversationHandoff, ...],
        error: str | None = None,
    ) -> None:
        self.state.retry(tuple(handoff.id for handoff in handoffs), error)

    async def process_ready(self, target: HandoffTurnTarget) -> bool:
        """Run one quiet-window proactive batch when the conversation is free."""
        if target.proactive_handoff_blocked:
            return False
        if self._clock() - self._last_activity < self._quiet_window_seconds:
            return False
        handoffs = self.state.claim_ready(limit=self._batch_limit)
        if not handoffs:
            return False
        try:
            await target.present_handoffs(handoffs)
        except asyncio.CancelledError:
            self.retry(handoffs, "Shared conversation was interrupted")
            raise
        except Exception as error:
            self.retry(handoffs, str(error))
            raise
        else:
            self.complete(handoffs)
            self.note_activity()
        return True

    def stop(self) -> None:
        self._stop.set()

    async def run_forever(self, target: HandoffTurnTarget) -> None:
        while not self._stop.is_set():
            try:
                await self.process_ready(target)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Conversational handoff turn failed; retaining batch")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass
