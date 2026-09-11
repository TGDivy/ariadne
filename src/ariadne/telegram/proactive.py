"""Quiet delivery for a shared proactive Codex turn."""

from __future__ import annotations

from collections.abc import Callable

from openai_codex.generated.v2_all import MessagePhase

from ..codex import (
    AgentMessageCompleted,
    AgentMessageStarted,
    AgentMessageUpdated,
    ConversationEvent,
)
from ..prompts.activations import SILENT_HANDOFF_RESPONSE
from .history import TelegramHistoryMessage, TelegramMessageStore, telegram_message_time
from .rich import RichBotAPI, split_rich_markdown


class ProactiveTurn:
    """Send only settled shared-conversation speech, with no fake input bubble."""

    def __init__(
        self,
        rich_api: RichBotAPI,
        history: TelegramMessageStore,
        *,
        chat_id: int,
        activity: Callable[[], None] | None = None,
    ) -> None:
        self._rich_api = rich_api
        self._history = history
        self._chat_id = chat_id
        self._activity = activity
        self._active_message: tuple[str, MessagePhase] | None = None
        self._delivered = 0
        self._silent = False

    @property
    def delivered_messages(self) -> int:
        return self._delivered

    async def apply(self, event: ConversationEvent) -> None:
        """Ignore private work and deliver each completed conversational beat."""
        if isinstance(event, AgentMessageStarted):
            if self._active_message is not None:
                raise RuntimeError("Codex started overlapping proactive speech.")
            self._active_message = (event.item_id, event.phase)
        elif isinstance(event, AgentMessageUpdated):
            self._require_active(event.item_id, event.phase)
        elif isinstance(event, AgentMessageCompleted):
            self._require_active(event.item_id, event.phase)
            self._active_message = None
            if event.text.strip() == SILENT_HANDOFF_RESPONSE:
                if self._delivered or event.phase != MessagePhase.final_answer:
                    raise RuntimeError(
                        "A silent handoff response must be the only speech."
                    )
                self._silent = True
                return
            if self._silent:
                raise RuntimeError("Codex spoke after a silent handoff response.")
            for chunk in split_rich_markdown(event.text):
                message = await self._rich_api.send(
                    chat_id=self._chat_id,
                    markdown=chunk,
                )
                self._history.record(
                    TelegramHistoryMessage(
                        chat_id=self._chat_id,
                        message_id=message.message_id,
                        sent_at=telegram_message_time(message),
                        speaker="iris",
                        source="telegram",
                        content_type="text",
                        text=chunk,
                    )
                )
                self._delivered += 1
                if self._activity is not None:
                    self._activity()

    def complete(self) -> None:
        if self._active_message is not None:
            raise RuntimeError("Codex ended with incomplete proactive speech.")
        if not self._delivered and not self._silent:
            raise RuntimeError("Codex completed without a proactive response.")

    def _require_active(self, item_id: str, phase: MessagePhase) -> None:
        if self._active_message != (item_id, phase):
            raise RuntimeError("Codex updated proactive speech outside its phase.")
