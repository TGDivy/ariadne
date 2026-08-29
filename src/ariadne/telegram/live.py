"""Live multi-bubble rendering for one Telegram-triggered Codex turn."""

import asyncio
import logging
import time
from collections.abc import Sequence
from typing import Literal

from openai_codex.generated.v2_all import MessagePhase
from telegram import Message
from telegram.error import TelegramError

from ..codex import (
    ActivityUpdated,
    AgentMessageCompleted,
    AgentMessageStarted,
    AgentMessageUpdated,
    ConversationEvent,
    TurnInterrupted,
    WorkStarted,
    WorkSummaryUpdated,
)
from .rich import (
    RICH_MESSAGE_LIMIT,
    RichBotAPI,
    RichButton,
    close_unterminated_fence,
    split_rich_markdown,
    streaming_rich_preview,
)

LOGGER = logging.getLogger(__name__)

LIVE_EDIT_INTERVAL_SECONDS = 1.0
FAILURE_MESSAGE = "I ran into a problem while working on that. Please try again."
STOPPING_MESSAGE = "Stopping…"
STOPPED_MESSAGE = "Stopped."
THINKING_MESSAGE = "Thinking…"
LIVE_ACTIVITY_MARKER = "✦"
TURN_STOP_CALLBACK = "turn:stop"


class _LiveBubble:
    """One persistent Rich Message edited until a speech phase settles."""

    _stop_button = RichButton(
        "Stop", "callback_data", TURN_STOP_CALLBACK, style="danger"
    )
    _stopping_button = RichButton("Stopping…", "disabled", style="danger")
    _stopped_button = RichButton("Stopped", "disabled", style="danger")

    def __init__(self, source: Message, rich_api: RichBotAPI) -> None:
        self._source = source
        self._rich_api = rich_api
        self._message: Message | None = None
        self._markdown = ""
        self._body = ""
        self._activity = THINKING_MESSAGE
        self._activity_before_stopping = THINKING_MESSAGE
        self._working_note = False
        self._sent_at = 0.0
        self._phase: Literal["running", "stopping", "terminal"] = "running"
        self._edit_lock = asyncio.Lock()
        self._scheduled_edit: asyncio.Task[None] | None = None

    @property
    def message_id(self) -> int | None:
        return self._message.message_id if self._message is not None else None

    async def start(self) -> None:
        markdown = self._live_markdown()
        self._message = await self._rich_api.send(
            chat_id=self._source.chat_id,
            markdown=markdown,
            message_thread_id=getattr(self._source, "message_thread_id", None),
            buttons=(self._stop_button,),
            disable_interactions=True,
        )
        self._markdown = markdown
        self._sent_at = time.monotonic()

    async def begin_work(self, activity: str) -> None:
        if self._phase != "running":
            return
        self._body = ""
        self._working_note = True
        self._activity = activity
        await self._request_live_edit(force=True)

    async def show_work_summary(self, markdown: str) -> None:
        if self._phase != "running":
            return
        preview = streaming_rich_preview(markdown)
        self._body = preview.markdown
        self._working_note = True
        await self._request_live_edit()

    async def begin_message(self) -> None:
        if self._phase != "running":
            return
        self._body = ""
        self._working_note = False
        self._activity = "Writing…"

    async def show_message(self, markdown: str) -> None:
        if self._phase != "running":
            return
        preview = streaming_rich_preview(markdown)
        self._body = preview.markdown
        self._working_note = False
        self._activity = preview.activity
        await self._request_live_edit()

    async def show_activity(self, activity: str) -> None:
        if self._phase != "running" or activity == self._activity:
            return
        self._activity = activity
        await self._request_live_edit()

    async def finish(self, markdown: str) -> None:
        if self._phase == "stopping":
            raise TurnInterrupted()
        if self._phase == "terminal":
            return
        self._phase = "terminal"
        self._cancel_scheduled_edit()
        await self._finalize(markdown, buttons=())

    async def stopping(self) -> None:
        if self._phase != "running":
            return
        self._activity_before_stopping = self._activity
        self._phase = "stopping"
        self._cancel_scheduled_edit()
        self._activity = STOPPING_MESSAGE
        await self._edit(
            self._live_markdown(),
            buttons=(self._stopping_button,),
            disable_interactions=True,
        )

    async def resume(self) -> None:
        if self._phase != "stopping":
            return
        self._phase = "running"
        self._activity = self._activity_before_stopping
        await self._request_live_edit(force=True)

    async def stopped(self, partial_message: str) -> None:
        self._phase = "terminal"
        self._cancel_scheduled_edit()
        content = partial_message.strip()
        if content:
            content = close_unterminated_fence(content)
            content = streaming_rich_preview(content).markdown
            if content:
                content += f"\n\n_{STOPPED_MESSAGE}_"
        if not content:
            content = STOPPED_MESSAGE
        await self._finalize(content, buttons=(self._stopped_button,))

    async def fail(self) -> None:
        self._phase = "terminal"
        self._cancel_scheduled_edit()
        await self._edit(FAILURE_MESSAGE, buttons=(), required=True)

    async def discard(self) -> None:
        self._phase = "terminal"
        self._cancel_scheduled_edit()
        if self._message is None:
            return
        async with self._edit_lock:
            try:
                await self._message.delete()
            except TelegramError:
                LOGGER.exception("Telegram live placeholder deletion failed")

    async def _request_live_edit(self, *, force: bool = False) -> None:
        markdown = self._live_markdown()
        if markdown == self._markdown:
            return
        remaining = LIVE_EDIT_INTERVAL_SECONDS - (time.monotonic() - self._sent_at)
        if force or remaining <= 0:
            self._cancel_scheduled_edit()
            await self._edit(
                markdown,
                buttons=(self._stop_button,),
                disable_interactions=True,
                only_while_running=True,
            )
            return
        if self._scheduled_edit is None or self._scheduled_edit.done():
            self._scheduled_edit = asyncio.create_task(self._edit_live_after(remaining))

    async def _edit_live_after(self, delay: float) -> None:
        task = asyncio.current_task()
        try:
            await asyncio.sleep(delay)
            await self._edit(
                self._live_markdown(),
                buttons=(self._stop_button,),
                disable_interactions=True,
                only_while_running=True,
            )
        except asyncio.CancelledError:
            pass
        finally:
            if self._scheduled_edit is task:
                self._scheduled_edit = None

    def _live_markdown(self) -> str:
        status = f"> {LIVE_ACTIVITY_MARKER} _{self._activity}_"
        if not self._body:
            return status
        preview = split_rich_markdown(self._body, RICH_MESSAGE_LIMIT - 1_024)[0]
        if self._working_note:
            preview = "\n".join(
                ">" if not line else f"> {line}" for line in preview.splitlines()
            )
        return f"{preview}\n\n{status}"

    def _cancel_scheduled_edit(self) -> None:
        task = self._scheduled_edit
        self._scheduled_edit = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _finalize(self, markdown: str, *, buttons: Sequence[RichButton]) -> None:
        chunks = split_rich_markdown(markdown)
        if not chunks:
            raise ValueError("A final Telegram response cannot be empty.")
        await self._edit(chunks[0], buttons=buttons, required=True)
        for chunk in chunks[1:]:
            await self._rich_api.send(
                chat_id=self._source.chat_id,
                markdown=chunk,
                message_thread_id=getattr(self._source, "message_thread_id", None),
            )

    async def _edit(
        self,
        markdown: str,
        *,
        buttons: Sequence[RichButton],
        required: bool = False,
        disable_interactions: bool = False,
        only_while_running: bool = False,
    ) -> None:
        async with self._edit_lock:
            if only_while_running and self._phase != "running":
                return
            if self._message is None:
                return
            try:
                self._message = await self._rich_api.edit(
                    self._message,
                    markdown,
                    buttons=buttons,
                    disable_interactions=disable_interactions,
                )
            except TelegramError:
                self._sent_at = time.monotonic()
                if required:
                    raise
                LOGGER.warning(
                    "Telegram Rich Message preview update failed; retaining preview"
                )
                return
            self._markdown = markdown
            self._sent_at = time.monotonic()


class LiveTurn:
    """Render explicit Codex work and speech phases into Telegram bubbles."""

    def __init__(self, source: Message, rich_api: RichBotAPI) -> None:
        self._source = source
        self._rich_api = rich_api
        self._bubble: _LiveBubble | None = None
        self._active_message: tuple[str, MessagePhase] | None = None
        self._partial_message = ""
        self._delivered_messages = 0
        self._phase: Literal["running", "stopping", "terminal"] = "running"

    @property
    def message_id(self) -> int | None:
        return self._bubble.message_id if self._bubble is not None else None

    async def start(self) -> None:
        await self._open_bubble()

    async def apply(self, event: ConversationEvent) -> None:
        if isinstance(event, WorkStarted):
            bubble = await self._ensure_bubble()
            await bubble.begin_work(event.activity)
        elif isinstance(event, WorkSummaryUpdated):
            bubble = await self._ensure_bubble()
            await bubble.show_work_summary(event.text)
        elif isinstance(event, ActivityUpdated):
            bubble = await self._ensure_bubble()
            await bubble.show_activity(event.text)
        elif isinstance(event, AgentMessageStarted):
            if self._active_message is not None:
                raise RuntimeError("Codex started overlapping speech items.")
            self._active_message = (event.item_id, event.phase)
            self._partial_message = ""
            bubble = await self._ensure_bubble()
            await bubble.begin_message()
        elif isinstance(event, AgentMessageUpdated):
            self._require_active_message(event.item_id, event.phase)
            self._partial_message = event.text
            bubble = await self._ensure_bubble()
            await bubble.show_message(event.text)
        elif isinstance(event, AgentMessageCompleted):
            self._require_active_message(event.item_id, event.phase)
            if self._phase == "stopping":
                raise TurnInterrupted()
            bubble = await self._ensure_bubble()
            await bubble.finish(event.text)
            self._delivered_messages += 1
            self._active_message = None
            self._partial_message = ""
            self._bubble = None
            if event.phase == MessagePhase.commentary:
                await self._open_bubble()
            else:
                self._phase = "terminal"

    async def complete(self) -> None:
        if self._phase == "terminal":
            return
        if self._active_message is not None:
            raise RuntimeError("Codex ended with an incomplete speech item.")
        if self._delivered_messages == 0:
            raise RuntimeError("Codex completed without an agent response.")
        self._phase = "terminal"
        if self._bubble is not None:
            await self._bubble.discard()
            self._bubble = None

    async def stopping(self) -> None:
        if self._phase != "running":
            return
        self._phase = "stopping"
        if self._bubble is not None:
            await self._bubble.stopping()

    async def resume(self) -> None:
        if self._phase != "stopping":
            return
        self._phase = "running"
        if self._bubble is not None:
            await self._bubble.resume()

    async def stopped(self) -> None:
        if self._phase == "terminal":
            return
        self._phase = "terminal"
        bubble = await self._ensure_bubble()
        await bubble.stopped(self._partial_message)

    async def fail(self) -> None:
        if self._phase == "terminal":
            return
        self._phase = "terminal"
        bubble = await self._ensure_bubble()
        await bubble.fail()

    async def _open_bubble(self) -> _LiveBubble:
        if self._bubble is not None:
            return self._bubble
        bubble = _LiveBubble(self._source, self._rich_api)
        self._bubble = bubble
        await bubble.start()
        return bubble

    async def _ensure_bubble(self) -> _LiveBubble:
        return self._bubble or await self._open_bubble()

    def _require_active_message(self, item_id: str, phase: MessagePhase) -> None:
        if self._active_message != (item_id, phase):
            raise RuntimeError("Codex updated speech outside its declared phase.")
