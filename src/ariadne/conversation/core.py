"""The one owner of Ariadne's shared conversation.

Every command and every turn admission passes through one asyncio lock, and no
I/O happens between checking that the conversation is idle and claiming it. A
turn runs outside the lock; its completion re-enters the lock to release the
conversation and start whatever input is waiting.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..codex import (
    CodexConversation,
    CodexModel,
    CodexTurnSettings,
    ConversationEvent,
    TurnInterrupted,
)
from ..handoff import ConversationHandoff, HandoffCoordinator
from ..prompts.activations import (
    EMPTY_REPLY,
    build_direct_turn_with_handoffs,
    build_document_turn_prompt,
    build_image_turn_prompt,
    build_proactive_handoff_turn_prompt,
    build_reply_turn_prompt,
)
from .attachments import AttachmentError, AttachmentStore
from .bus import EventBus
from .events import (
    ConversationReset,
    InputAccepted,
    LiveTurnView,
    MessageCommitted,
    MessageView,
    Notice,
    QuestionAsked,
    QuestionSettled,
    QuestionView,
    ReactionsChanged,
    Snapshot,
    StatusChanged,
    StreamEvent,
    TurnFinished,
    TurnStarted,
    TurnStateChanged,
)
from .inputs import PendingInputs
from .log import ConversationLog
from .models import (
    Attachment,
    ConversationMessage,
    InputDisposition,
    MessageDraft,
    MessageOrigin,
    NotifyLevel,
    TurnKind,
    TurnOutcome,
    TurnState,
)
from .projector import CommitSpeech, TurnProjector
from .push import NullPushGateway, PushGateway, PushNotification, push_body
from .questions import Question, QuestionSelection, QuestionStore
from .shares import SharedFiles

LOGGER = logging.getLogger(__name__)

STEER_TIMEOUT_SECONDS = 15.0
WATCH_INTERVAL_SECONDS = 0.2
DRAIN_INTERVAL_SECONDS = 0.05
TURN_FAILED_MESSAGE = "Something went wrong while I was working on that."
STOP_FAILED_MESSAGE = "I couldn't stop the active turn. Please try again."


class ConversationBusy(RuntimeError):
    """The conversation cannot change while a turn is active."""


class UnknownReference(ValueError):
    """A command referred to a message or attachment that does not exist."""


@dataclass(frozen=True, slots=True)
class SendRequest:
    """One message from the owner, as received from a client."""

    text: str
    client_message_id: str | None = None
    attachment_ids: tuple[str, ...] = ()
    reply_to_id: int | None = None
    voice: bool = False


@dataclass(frozen=True, slots=True)
class SendResult:
    message: ConversationMessage
    disposition: InputDisposition


@dataclass(slots=True)
class _ActiveTurn:
    id: str
    kind: TurnKind
    projector: TurnProjector
    handoffs: tuple[ConversationHandoff, ...] = ()
    state: TurnState = "running"
    presented_question: str | None = None
    task: asyncio.Task[TurnOutcome] | None = field(default=None, repr=False)

    @property
    def stopping(self) -> bool:
        return self.state == "stopping"

    @property
    def origin(self) -> MessageOrigin:
        return "direct" if self.kind == "direct" else "proactive"


class ConversationCore:
    """Serialize every command into one Codex conversation and publish events."""

    def __init__(
        self,
        conversation: CodexConversation,
        *,
        state: Path,
        attachments: AttachmentStore,
        handoffs: HandoffCoordinator,
        bus: EventBus,
        human_name: str,
        agent_name: str,
        push: PushGateway | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.log = ConversationLog(state)
        self.questions = QuestionStore(state)
        self.inputs = PendingInputs(state)
        self.shares = SharedFiles(state)
        self.attachments = attachments
        self.bus = bus
        self.human_name = human_name
        self.agent_name = agent_name
        self._conversation = conversation
        self._handoffs = handoffs
        self._push = push or NullPushGateway()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._gate = asyncio.Lock()
        self._turn: _ActiveTurn | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[None]] = set()
        self._closing = False

    async def start(self) -> None:
        """Recover state a previous process left behind."""
        for store in (self.log, self.questions, self.inputs, self.shares):
            store.initialize()
        self.attachments.initialize()
        self._settle(self.questions.cancel_pending())
        self._deliver_shares(None)
        if self.inputs.count():
            self._ensure_drain()

    async def close(self) -> None:
        self._closing = True
        tasks: list[asyncio.Task[Any]] = [
            task for task in (self._drain_task,) if task is not None
        ]
        turn = self._turn
        if turn is not None and turn.task is not None:
            tasks.append(turn.task)
        tasks.extend(self._background)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def close_codex(self) -> None:
        """Release the shared Codex client after every turn has ended."""
        await self._conversation.close()

    @property
    def busy(self) -> bool:
        return self._turn is not None

    @property
    def turn_kind(self) -> TurnKind | None:
        turn = self._turn
        return turn.kind if turn is not None else None

    @property
    def live_activity(self) -> str | None:
        turn = self._turn
        return turn.projector.activity if turn is not None else None

    @property
    def settings(self) -> CodexTurnSettings:
        return self._conversation.settings

    @property
    def proactive_handoff_blocked(self) -> bool:
        """Keep proactive work behind active or already-accepted human input."""
        return (
            self._turn is not None or self._gate.locked() or bool(self.inputs.count())
        )

    async def present_handoffs(self, handoffs: tuple[ConversationHandoff, ...]) -> None:
        """Run one proactive turn and raise if its context should be offered again."""
        async with self._gate:
            if self._turn is not None or self.inputs.count():
                raise RuntimeError("The conversation became busy.")
            turn = self._begin_turn(
                "proactive", build_proactive_handoff_turn_prompt(handoffs), ()
            )
        assert turn.task is not None
        outcome = await asyncio.shield(turn.task)
        # Anything already said must never be repeated, and an explicit stop
        # means "not now" rather than "say it again in two minutes".
        if outcome == "failed" and not turn.projector.committed:
            raise RuntimeError("The proactive turn failed before saying anything.")

    async def send(self, request: SendRequest) -> SendResult:
        """Commit one owner message, then answer, steer, queue, or start a turn."""
        if request.client_message_id is not None:
            existing = self.log.by_client_id(request.client_message_id)
            if existing is not None:
                return SendResult(existing, "duplicate")
        attachments = self._resolve_attachments(request.attachment_ids)
        reply = None
        if request.reply_to_id is not None:
            reply = self.log.get(request.reply_to_id)
            if reply is None:
                raise UnknownReference("The replied-to message does not exist.")

        async with self._gate:
            message = self.log.append(
                MessageDraft(
                    speaker="human",
                    origin="direct",
                    kind="voice" if request.voice else "text",
                    text=request.text,
                    sent_at=self._clock(),
                    client_message_id=request.client_message_id,
                    reply_to_id=request.reply_to_id,
                    attachments=attachments,
                )
            )
            self._publish_message(message, notify=None)
            self._handoffs.note_activity()
            disposition = await self._route(message, reply)
            self._publish(
                InputAccepted(
                    client_message_id=message.client_message_id,
                    message_id=message.id,
                    disposition=disposition,
                )
            )
        return SendResult(message, disposition)

    async def stop(self) -> bool:
        """Ask Codex to interrupt the active turn; return whether one is stopping."""
        async with self._gate:
            turn = self._turn
            if turn is None:
                return False
            if turn.stopping:
                return True
            self._set_state(turn, "stopping")
        self._settle(self.questions.cancel_pending())
        try:
            interrupted = await asyncio.wait_for(
                self._conversation.interrupt(), timeout=STEER_TIMEOUT_SECONDS
            )
            if not interrupted:
                LOGGER.info("Stop requested before the Codex turn started")
        except Exception:
            LOGGER.exception("Codex turn interruption failed")
            if self._turn is turn and turn.stopping:
                self._set_state(turn, "running")
            self._publish(Notice(level="error", text=STOP_FAILED_MESSAGE))
            return False
        return True

    def answer_question(self, question_id: str, choice_index: int) -> QuestionSelection:
        """Apply the owner's button choice and record it in the conversation."""
        selection = self.questions.answer_choice(question_id, choice_index)
        if selection.outcome == "accepted" and selection.question is not None:
            question = selection.question
            answer = self.log.append(
                MessageDraft(
                    speaker="human",
                    origin="direct",
                    kind="answer",
                    text=question.answer or "",
                    sent_at=self._clock(),
                    reply_to_id=question.message_id,
                    question_id=question.question_id,
                )
            )
            self._publish_message(answer, notify=None)
            self._question_answered(question)
        return selection

    def react(self, message_id: int, reactions: tuple[str, ...]) -> bool:
        if not self.log.set_reactions(message_id, reactions):
            return False
        message = self.log.get(message_id)
        self._publish(
            ReactionsChanged(
                message_id=message_id,
                reactions=message.reactions if message is not None else (),
            )
        )
        return True

    async def available_models(self) -> tuple[CodexModel, ...]:
        return await self._conversation.available_models()

    async def new_conversation(self) -> None:
        async with self._gate:
            if self._turn is not None:
                raise ConversationBusy("A new conversation cannot start mid-turn.")
            self._conversation.reset()
            self._publish(ConversationReset(reason="new"))

    async def update_settings(self, settings: CodexTurnSettings) -> None:
        async with self._gate:
            if self._turn is not None:
                raise ConversationBusy("Settings cannot change mid-turn.")
            self._conversation.set_settings(settings)
            self._publish(ConversationReset(reason="settings"))
            self._publish(StatusChanged())

    def snapshot(self, *, reset: bool = False) -> Snapshot:
        turn = self._turn
        question = self.questions.pending()
        return Snapshot(
            seq=self.bus.latest_seq(),
            boot_id=self.bus.boot_id,
            last_message_id=self.log.latest_id(),
            turn=self._view(turn) if turn is not None else None,
            question=(
                QuestionView.of(question)
                if question is not None and question.message_id is not None
                else None
            ),
            queued_inputs=self.inputs.count(),
            reset=reset,
        )

    async def _route(
        self, message: ConversationMessage, reply: ConversationMessage | None
    ) -> InputDisposition:
        turn = self._turn
        if turn is not None and not message.attachments:
            answered = self.questions.answer_text(message.text)
            if answered is not None:
                self._question_answered(answered)
                return "answered"
        prompt, images = self._prompt(message, reply)
        if turn is not None:
            return await self._steer_or_queue(turn, message.id, prompt, images)
        if self.inputs.count():
            self.inputs.add(message_id=message.id, prompt=prompt, image_paths=images)
            self._ensure_drain()
            return "queued"
        self._begin_direct_turn(prompt, images)
        return "started"

    async def _steer_or_queue(
        self,
        turn: _ActiveTurn,
        message_id: int,
        prompt: str,
        images: tuple[Path, ...],
    ) -> InputDisposition:
        if not self.inputs.count() and not turn.stopping:
            try:
                if await self._steer(prompt, images):
                    return "steered"
            except Exception:
                LOGGER.exception("Steering failed; preserving input for the next turn")
                self.inputs.add(
                    message_id=message_id,
                    prompt=prompt,
                    image_paths=images,
                    force_next_turn=True,
                )
                self._ensure_drain()
                return "queued"
        self.inputs.add(message_id=message_id, prompt=prompt, image_paths=images)
        self._ensure_drain()
        return "queued"

    async def _steer(self, prompt: str, images: tuple[Path, ...]) -> bool:
        return await asyncio.wait_for(
            self._conversation.steer(prompt, image_paths=images),
            timeout=STEER_TIMEOUT_SECONDS,
        )

    def _begin_direct_turn(self, prompt: str, images: tuple[Path, ...]) -> None:
        claimed = self._handoffs.claim_for_direct_turn()
        self._begin_turn(
            "direct",
            build_direct_turn_with_handoffs(prompt, claimed),
            images,
            handoffs=claimed,
        )

    def _begin_turn(
        self,
        kind: TurnKind,
        prompt: str,
        images: tuple[Path, ...],
        *,
        handoffs: tuple[ConversationHandoff, ...] = (),
    ) -> _ActiveTurn:
        turn_id = f"turn_{secrets.token_urlsafe(9)}"
        turn = _ActiveTurn(
            id=turn_id,
            kind=kind,
            projector=TurnProjector(turn_id, kind),
            handoffs=handoffs,
        )
        self._turn = turn
        self._publish(TurnStarted(turn_id=turn_id, kind=kind))
        self._publish(StatusChanged())
        turn.task = asyncio.create_task(self._run_turn(turn, prompt, images))
        return turn

    async def _run_turn(
        self, turn: _ActiveTurn, prompt: str, images: tuple[Path, ...]
    ) -> TurnOutcome:
        outcome: TurnOutcome = "failed"
        error: str | None = None
        watcher = asyncio.create_task(self._watch(turn))
        try:
            try:
                if await self._conversation.prepare_thread():
                    self._publish(ConversationReset(reason="continuity_lost"))
                if turn.stopping:
                    raise TurnInterrupted()
                async for event in self._conversation.stream_turn(
                    prompt,
                    image_paths=images,
                    stop_requested=lambda: turn.stopping,
                ):
                    if not turn.stopping:
                        self._apply(turn, event)
                if turn.stopping:
                    raise TurnInterrupted()
                self._deliver_shares(turn)
                outcome = turn.projector.complete()
            except TurnInterrupted:
                outcome = "stopped"
            except asyncio.CancelledError:
                outcome = "stopped"
                raise
            except Exception:
                if turn.stopping:
                    outcome = "stopped"
                else:
                    LOGGER.exception("Conversation turn failed turn_id=%s", turn.id)
                    error = TURN_FAILED_MESSAGE
            if outcome == "stopped":
                self._keep_partial_speech(turn)
        finally:
            watcher.cancel()
            with suppress(asyncio.CancelledError):
                await watcher
            await self._finish_turn(turn, outcome, error)
        return outcome

    async def _finish_turn(
        self, turn: _ActiveTurn, outcome: TurnOutcome, error: str | None
    ) -> None:
        try:
            if turn.handoffs:
                if outcome in ("completed", "silent") or turn.projector.committed:
                    self._handoffs.complete(turn.handoffs)
                else:
                    self._handoffs.retry(
                        turn.handoffs, f"Direct turn ended with outcome {outcome}"
                    )
        except Exception:
            # Bookkeeping must never leave the conversation claimed forever.
            LOGGER.exception("Handoff bookkeeping failed turn_id=%s", turn.id)
        try:
            self._settle(self.questions.cancel_pending())
        except Exception:
            LOGGER.exception("Question cleanup failed turn_id=%s", turn.id)
        async with self._gate:
            if self._turn is turn:
                self._turn = None
            self._handoffs.note_activity()
            self._publish(
                TurnFinished(
                    turn_id=turn.id, kind=turn.kind, outcome=outcome, error=error
                )
            )
            self._publish(StatusChanged())
            if self.inputs.count():
                self._ensure_drain()

    def _apply(self, turn: _ActiveTurn, event: ConversationEvent) -> None:
        for action in turn.projector.apply(event):
            if isinstance(action, CommitSpeech):
                message = self.log.append(
                    MessageDraft(
                        speaker="iris",
                        origin=turn.origin,
                        kind="text",
                        text=action.text,
                        sent_at=self._clock(),
                        turn_id=turn.id,
                        phase=action.phase,
                    )
                )
                turn.projector.note_committed()
                self._publish_message(message, notify="active")
            else:
                self._publish(action)

    def _keep_partial_speech(self, turn: _ActiveTurn) -> None:
        partial = turn.projector.partial_speech()
        if partial is None:
            return
        message = self.log.append(
            MessageDraft(
                speaker="iris",
                origin=turn.origin,
                kind="text",
                text=partial.text,
                sent_at=self._clock(),
                turn_id=turn.id,
                phase=partial.phase,
                stopped=True,
            )
        )
        turn.projector.note_committed()
        self._publish_message(message, notify=None)

    async def _watch(self, turn: _ActiveTurn) -> None:
        """Surface questions and shared files written by Iris's MCP tools."""
        while True:
            await asyncio.sleep(WATCH_INTERVAL_SECONDS)
            try:
                self._present_question(turn)
                self._deliver_shares(turn)
            except Exception:
                LOGGER.exception("Capability watcher failed turn_id=%s", turn.id)

    def _present_question(self, turn: _ActiveTurn) -> None:
        if turn.presented_question is not None:
            current = self.questions.get(turn.presented_question)
            if current is not None and current.status != "pending":
                self._question_answered(current)
            return
        question = self.questions.pending()
        if question is None or question.message_id is not None:
            return
        message = self.log.append(
            MessageDraft(
                speaker="iris",
                origin=turn.origin,
                kind="question",
                text=question.prompt,
                sent_at=self._clock(),
                turn_id=turn.id,
                question_id=question.question_id,
            )
        )
        presented = self.questions.attach_message(question.question_id, message.id)
        turn.projector.note_committed()
        self._publish_message(message, notify="active")
        if presented is not None:
            turn.presented_question = presented.question_id
            self._publish(QuestionAsked(question=QuestionView.of(presented)))
            self._set_state(turn, "waiting_input")

    def _question_answered(self, question: Question) -> None:
        self._publish(QuestionSettled(question=QuestionView.of(question)))
        turn = self._turn
        if turn is not None and turn.presented_question == question.question_id:
            turn.presented_question = None
            if turn.state == "waiting_input":
                self._set_state(turn, "running")

    def _settle(self, questions: tuple[Question, ...]) -> None:
        for question in questions:
            if question.message_id is not None:
                self._question_answered(question)

    def _deliver_shares(self, turn: _ActiveTurn | None) -> None:
        for share in self.shares.pending():
            try:
                attachments = tuple(
                    self.attachments.copy_in(path) for path in share.paths
                )
            except AttachmentError as error:
                self.shares.finish(share.id, error=str(error))
                continue
            message = self.log.append(
                MessageDraft(
                    speaker="iris",
                    origin=turn.origin if turn is not None else "proactive",
                    kind="text",
                    text=share.caption or "",
                    sent_at=self._clock(),
                    turn_id=turn.id if turn is not None else None,
                    attachments=attachments,
                )
            )
            self.shares.finish(share.id)
            if turn is not None:
                turn.projector.note_committed()
            self._publish_message(message, notify="active")

    def _ensure_drain(self) -> None:
        if self._closing:
            return
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain())

    async def _drain(self) -> None:
        """Deliver every queued input exactly once and in arrival order."""
        while not self._closing:
            async with self._gate:
                pending = self.inputs.first()
                if pending is None:
                    return
                turn = self._turn
                if turn is None:
                    self.inputs.remove(pending.id)
                    self._begin_direct_turn(pending.prompt, pending.image_paths)
                    continue
                if not turn.stopping and not pending.force_next_turn:
                    try:
                        steered = await self._steer(pending.prompt, pending.image_paths)
                    except Exception:
                        LOGGER.exception(
                            "Steering retry failed; moving input to next turn"
                        )
                        self.inputs.require_next_turn(pending.id)
                        steered = False
                    if steered:
                        self.inputs.remove(pending.id)
                        continue
            await asyncio.sleep(DRAIN_INTERVAL_SECONDS)

    def _prompt(
        self, message: ConversationMessage, reply: ConversationMessage | None
    ) -> tuple[str, tuple[Path, ...]]:
        text = message.text
        if message.kind == "voice":
            text = (
                "Voice-note transcript (automatic and possibly imperfect):\n"
                f"<transcript>\n{text}\n</transcript>"
            )
        images = tuple(
            item.path for item in message.attachments if item.kind == "image"
        )
        documents = [
            (item.path, item.mime_type)
            for item in message.attachments
            if item.kind != "image"
        ]
        if documents:
            body = build_document_turn_prompt(text or None, documents)
        elif images and not text:
            body = build_image_turn_prompt(None, image_count=len(images))
        else:
            body = text
        if reply is not None:
            body = build_reply_turn_prompt(
                body,
                quoted_message=reply.text or EMPTY_REPLY,
                quoted_message_id=reply.id,
                quoted_author=self.agent_name
                if reply.speaker == "iris"
                else self.human_name,
            )
        return body, images

    def _resolve_attachments(
        self, identifiers: tuple[str, ...]
    ) -> tuple[Attachment, ...]:
        resolved = []
        for identifier in dict.fromkeys(identifiers):
            attachment = self.attachments.get(identifier)
            if attachment is None:
                raise UnknownReference(f"Unknown attachment: {identifier}")
            resolved.append(attachment)
        return tuple(resolved)

    def _view(self, turn: _ActiveTurn) -> LiveTurnView:
        return turn.projector.view(turn.state)

    def _set_state(self, turn: _ActiveTurn, state: TurnState) -> None:
        turn.state = state
        self._publish(TurnStateChanged(turn_id=turn.id, state=state))

    def _publish(self, event: StreamEvent) -> None:
        self.bus.publish(event)

    def _publish_message(
        self, message: ConversationMessage, *, notify: NotifyLevel | None
    ) -> None:
        self._publish(MessageCommitted(message=MessageView.of(message), notify=notify))
        if notify is None or message.speaker != "iris" or self.bus.has_subscribers:
            return
        body = message.text or ", ".join(item.filename for item in message.attachments)
        notification = PushNotification(
            title=self.agent_name,
            body=push_body(body),
            level=notify,
            message_id=message.id,
        )
        task = asyncio.create_task(self._send_push(notification))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _send_push(self, notification: PushNotification) -> None:
        try:
            await self._push.notify(notification)
        except Exception:
            LOGGER.exception(
                "Push notification failed message_id=%s", notification.message_id
            )
