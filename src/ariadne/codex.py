"""A small in-memory Codex conversation for Ariadne."""

from collections.abc import AsyncIterator
from pathlib import Path

from openai_codex import AsyncCodex, AsyncThread, Sandbox
from openai_codex.generated.v2_all import (
    AgentMessageDeltaNotification,
    AgentMessageThreadItem,
    ItemCompletedNotification,
    MessagePhase,
    TurnCompletedNotification,
)

from .the_thread import build_developer_instructions


class CodexConversation:
    """Reuse one Codex client and one thread for the lifetime of the process."""

    def __init__(self, vault: Path, *, client: AsyncCodex | None = None) -> None:
        self._vault = vault
        self._client = client if client is not None else AsyncCodex()
        self._thread: AsyncThread | None = None

    async def stream_reply(self, message: str) -> AsyncIterator[str]:
        """Yield the complete accumulated agent message as each delta arrives."""
        thread = await self._thread_for_conversation()
        turn = await thread.turn(
            message,
            cwd=str(self._vault),
            sandbox=Sandbox.workspace_write,
        )

        response = ""
        final_answer: str | None = None
        async for event in turn.stream():
            if isinstance(event.payload, AgentMessageDeltaNotification):
                if event.payload.delta:
                    response += event.payload.delta
                    yield response
            elif isinstance(event.payload, ItemCompletedNotification):
                item = event.payload.item.root
                if (
                    isinstance(item, AgentMessageThreadItem)
                    and item.phase == MessagePhase.final_answer
                ):
                    final_answer = item.text
            elif isinstance(event.payload, TurnCompletedNotification):
                if event.payload.turn.error is not None:
                    detail = event.payload.turn.error.message or "Codex turn failed."
                    raise RuntimeError(detail)
                if event.payload.turn.status.value == "failed":
                    raise RuntimeError("Codex turn failed.")

        if final_answer is not None and final_answer != response:
            response = final_answer
            yield response

        if not response:
            raise RuntimeError("Codex completed without an agent response.")

    async def close(self) -> None:
        """Release the process-wide Codex client during shutdown."""
        await self._client.close()

    def reset(self) -> None:
        """Discard the in-memory thread while retaining The Thread vault."""
        self._thread = None

    async def _thread_for_conversation(self) -> AsyncThread:
        if self._thread is None:
            self._thread = await self._client.thread_start(
                cwd=str(self._vault),
                developer_instructions=build_developer_instructions(self._vault),
                sandbox=Sandbox.workspace_write,
            )
        return self._thread
