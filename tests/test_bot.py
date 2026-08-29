import asyncio
import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock

import pytest
from openai_codex.generated.v2_all import MessagePhase, ReasoningEffort
from telegram import Message
from telegram.constants import ParseMode
from telegram.error import BadRequest

from ariadne.codex import (
    ActivityUpdated,
    AgentMessageCompleted,
    AgentMessageStarted,
    AgentMessageUpdated,
    CodexConversation,
    CodexModel,
    CodexTurnSettings,
    TurnInterrupted,
    WorkStarted,
    WorkSummaryUpdated,
)
from ariadne.prompts.activations import (
    DOCUMENT_WITHOUT_CAPTION,
    build_document_turn_prompt,
)
from ariadne.telegram import bot as telegram_bot
from ariadne.telegram import live as telegram_live
from ariadne.telegram.bot import (
    BUSY_MESSAGE,
    NEW_CONVERSATION_MESSAGE,
    NOTHING_TO_STOP_MESSAGE,
    SETTINGS_BUSY_MESSAGE,
    STOPPED_MESSAGE,
    turn_text,
)
from ariadne.telegram.bot import AriadneBot as TelegramBot
from ariadne.telegram.history import TelegramMessageStore
from ariadne.telegram.live import FAILURE_MESSAGE, _LiveBubble
from ariadne.telegram.questions import TelegramQuestionStore
from ariadne.telegram.rich import RICH_MESSAGE_LIMIT, RichBotAPI, RichButton

DEFAULT_SETTINGS = CodexTurnSettings(
    model="gpt-5.6-luna",
    effort=ReasoningEffort.low,
    web_search="disabled",
)
DEFAULT_MODELS = (
    CodexModel(
        identifier="gpt-5.6-luna",
        display_name="GPT-5.6 Luna",
        default_effort=ReasoningEffort.low,
        supported_efforts=(ReasoningEffort.low, ReasoningEffort.medium),
    ),
)


def AriadneBot(
    allowed_user_id: int,
    conversation: CodexConversation,
    *,
    question_state: Path | None = None,
) -> TelegramBot:
    """Build the bot with the required test credential."""
    bot = TelegramBot(
        allowed_user_id,
        conversation,
        bot_token="token-for-test",
        question_state=question_state,
    )
    bot._rich_api = cast(RichBotAPI, FakeRichAPI())
    return bot


class FakeMessage:
    def __init__(self, message_id: int = 11) -> None:
        self.chat_id = 7
        self.message_id = message_id
        self.message_thread_id: int | None = None
        self.media_group_id: str | None = None
        self.text: str | None = None
        self.caption: str | None = None
        self.reply_to_message: FakeMessage | None = None
        self.replies: list[str] = []
        self.reply_parse_modes: list[ParseMode | None] = []
        self.reply_markups: list[object | None] = []
        self.edits: list[str] = []
        self.edit_parse_modes: list[ParseMode | None] = []
        self.edit_markups: list[object | None] = []
        self.drafts: list[str | None] = []
        self.draft_ids: list[int] = []
        self.deleted = False

    async def reply_text(
        self,
        text: str,
        *,
        parse_mode: ParseMode | None = None,
        reply_markup: object | None = None,
    ) -> "FakeMessage":
        self.replies.append(text)
        self.reply_parse_modes.append(parse_mode)
        self.reply_markups.append(reply_markup)
        return self

    async def reply_text_draft(self, draft_id: int, text: str | None = None) -> bool:
        self.draft_ids.append(draft_id)
        self.drafts.append(text)
        return True

    async def edit_text(
        self,
        text: str,
        *,
        parse_mode: ParseMode | None = None,
        reply_markup: object | None = None,
    ) -> "FakeMessage":
        self.edits.append(text)
        self.edit_parse_modes.append(parse_mode)
        self.edit_markups.append(reply_markup)
        return self

    async def delete(self) -> bool:
        self.deleted = True
        return True


class FakeConversation:
    def __init__(
        self,
        responses: list[str],
        *,
        failures: int = 0,
        activities: list[str] | None = None,
        events: list[object] | None = None,
        models: tuple[CodexModel, ...] = DEFAULT_MODELS,
    ) -> None:
        self._responses = responses
        self._failures = failures
        self._activities = activities or []
        self._events = events
        self._models = models
        self.settings = DEFAULT_SETTINGS
        self.prompts: list[str] = []
        self.reset_calls = 0
        self.set_settings_calls = 0
        self.interrupt_calls = 0

    async def stream_turn(
        self,
        prompt: str,
        *,
        image_paths=(),
        stop_requested=None,
    ):
        self.prompts.append(prompt)
        if self._failures:
            self._failures -= 1
            raise RuntimeError("Codex failed")
        if self._events is not None:
            for event in self._events:
                yield event
            return
        for update in self._activities:
            yield ActivityUpdated(update)
        yield AgentMessageStarted("final", MessagePhase.final_answer)
        for response in self._responses:
            yield AgentMessageUpdated("final", MessagePhase.final_answer, response)
        if self._responses:
            yield AgentMessageCompleted(
                "final", MessagePhase.final_answer, self._responses[-1]
            )

    def reset(self) -> None:
        self.reset_calls += 1

    def set_settings(self, settings: CodexTurnSettings) -> None:
        self.settings = settings
        self.set_settings_calls += 1
        self.reset()

    async def available_models(self) -> tuple[CodexModel, ...]:
        return self._models

    async def interrupt(self) -> bool:
        self.interrupt_calls += 1
        return False


class BlockingConversation:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.interrupted = False
        self.interrupt_calls = 0
        self.settings = DEFAULT_SETTINGS
        self.reset_calls = 0
        self.steered: list[str] = []
        self.steer_error: Exception | None = None
        self.steer_accepted = True
        self.failure_after_release: Exception | None = None

    async def stream_turn(
        self,
        _: str,
        *,
        image_paths=(),
        stop_requested=None,
    ):
        self.started.set()
        yield AgentMessageStarted("final", MessagePhase.final_answer)
        yield AgentMessageUpdated("final", MessagePhase.final_answer, "Working")
        await self.release.wait()
        if self.failure_after_release is not None:
            raise self.failure_after_release
        if self.interrupted:
            raise TurnInterrupted()
        yield AgentMessageUpdated("final", MessagePhase.final_answer, "Finished")
        yield AgentMessageCompleted("final", MessagePhase.final_answer, "Finished")

    def reset(self) -> None:
        self.reset_calls += 1

    def set_settings(self, settings: CodexTurnSettings) -> None:
        self.settings = settings
        self.reset()

    async def available_models(self) -> tuple[CodexModel, ...]:
        return DEFAULT_MODELS

    async def steer(self, text: str, *, image_paths=()) -> bool:
        if self.steer_error is not None:
            raise self.steer_error
        if not self.steer_accepted:
            return False
        self.steered.append(text)
        return True

    async def interrupt(self) -> bool:
        self.interrupt_calls += 1
        self.interrupted = True
        self.release.set()
        return True


class FakeRichAPI:
    def __init__(self) -> None:
        self.messages: list[FakeMessage] = []
        self.sent: list[dict[str, object]] = []
        self.edits: list[tuple[str, tuple[RichButton, ...]]] = []
        self.edit_message_ids: list[int] = []
        self.edit_interactions_disabled: list[bool] = []

    @property
    def live_message(self) -> FakeMessage:
        return self.messages[0]

    async def send(self, **kwargs: object) -> FakeMessage:
        self.sent.append(kwargs)
        message = FakeMessage(message_id=90 + len(self.messages))
        self.messages.append(message)
        return message

    async def edit(
        self,
        message: Message,
        markdown: str,
        *,
        buttons: tuple[RichButton, ...] = (),
        disable_interactions: bool = False,
    ) -> FakeMessage:
        self.edit_message_ids.append(message.message_id)
        self.edits.append((markdown, buttons))
        self.edit_interactions_disabled.append(disable_interactions)
        return message


class RejectingRichAPI:
    async def send(self, **_: object) -> FakeMessage:
        raise BadRequest("Rich Messages unavailable")


@pytest.fixture
def message() -> FakeMessage:
    return FakeMessage()


@pytest.fixture
def unthrottled_live_edits(monkeypatch) -> None:
    monkeypatch.setattr(telegram_live, "LIVE_EDIT_INTERVAL_SECONDS", 0.0)


@pytest.fixture
def history_store(tmp_path: Path) -> TelegramMessageStore:
    return TelegramMessageStore(tmp_path / "telegram.sqlite3")


@pytest.fixture(autouse=True)
def isolated_question_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        telegram_bot,
        "default_question_state_path",
        lambda: tmp_path / "telegram.sqlite3",
    )


async def test_unauthorized_message_is_ignored(message: FakeMessage, caplog) -> None:
    conversation = FakeConversation(["This should not be sent"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 8, "Hello")

    assert conversation.prompts == []
    assert message.replies == []
    assert bot._history.read(7, since=datetime.min.replace(tzinfo=UTC)).total == 0
    assert "Ignoring message from unauthorized Telegram user id=8" in caplog.text


async def test_file_delivery_uses_the_configured_bot_token(
    message: FakeMessage,
) -> None:
    delivery = AsyncMock()
    delivery.approve.return_value = [object()]
    bot = TelegramBot(
        7,
        cast(CodexConversation, FakeConversation([])),
        bot_token="configured-token",
    )
    bot._file_delivery = delivery

    await bot._approve_staged_files(cast(Message, message), 7, "approval-id")

    delivery.approve.assert_awaited_once_with(
        "approval-id", token="configured-token", chat_id=7
    )


async def test_a_streamed_reply_edits_one_persistent_message(
    message: FakeMessage, caplog
) -> None:
    caplog.set_level(logging.INFO)
    conversation = FakeConversation(["Hello", "Hello, Ariadne!"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Say hello")

    rich = cast(FakeRichAPI, bot._rich_api)
    assert conversation.prompts == [turn_text("Say hello")]
    assert rich.sent[0]["markdown"] == "> ✦ _Thinking…_"
    assert "reply_to_message_id" not in rich.sent[0]
    assert rich.edits[-1][0] == "Hello, Ariadne!"
    assert message.draft_ids == []
    assert "Telegram message received message_id=11" in caplog.text
    assert "Telegram turn started message_id=11" in caplog.text
    assert "Telegram turn finished message_id=11 status=success" in caplog.text
    assert "Say hello" not in caplog.text


async def test_bound_bot_streams_and_finalizes_native_rich_messages(
    message: FakeMessage, unthrottled_live_edits: None
) -> None:
    conversation = FakeConversation(["## Heading", "## Heading\n\n| A | B |"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    rich = FakeRichAPI()
    bot._rich_api = cast(RichBotAPI, rich)

    await bot.handle_text(cast(Message, message), 7, "Use native formatting")

    assert message.replies == []
    assert rich.sent[0]["markdown"] == "> ✦ _Thinking…_"
    assert "reply_to_message_id" not in rich.sent[0]
    assert rich.sent[0]["buttons"]
    assert rich.sent[0]["disable_interactions"] is True
    assert [edit[0] for edit in rich.edits] == [
        "## Heading\n\n> ✦ _Writing…_",
        "## Heading\n\n> ✦ _Building a table…_",
        "## Heading\n\n| A | B |",
    ]
    assert rich.edit_interactions_disabled == [True, True, False]
    assert rich.edits[-1][1] == ()


async def test_reasoning_summaries_are_replaced_by_separate_native_messages(
    message: FakeMessage, unthrottled_live_edits: None
) -> None:
    events = [
        WorkStarted("reasoning-1", "Analysing…"),
        WorkSummaryUpdated("reasoning-1", 0, "**Checking the growth claim**"),
        AgentMessageStarted("commentary", MessagePhase.commentary),
        AgentMessageUpdated(
            "commentary",
            MessagePhase.commentary,
            "The stated growth rate does not reconcile.",
        ),
        AgentMessageCompleted(
            "commentary",
            MessagePhase.commentary,
            "The stated growth rate does not reconcile.",
        ),
        WorkStarted("reasoning-2", "Analysing…"),
        WorkSummaryUpdated("reasoning-2", 0, "**Choosing the next action**"),
        AgentMessageStarted("final", MessagePhase.final_answer),
        AgentMessageUpdated(
            "final", MessagePhase.final_answer, "Correct it before the meeting."
        ),
        AgentMessageCompleted(
            "final", MessagePhase.final_answer, "Correct it before the meeting."
        ),
    ]
    conversation = FakeConversation([], events=events)
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Audit the board pack")

    rich = cast(FakeRichAPI, bot._rich_api)
    assert len(rich.sent) == 2
    assert all("reply_to_message_id" not in sent for sent in rich.sent)
    first_edits = [
        markdown
        for message_id, (markdown, _buttons) in zip(
            rich.edit_message_ids, rich.edits, strict=True
        )
        if message_id == 90
    ]
    second_edits = [
        markdown
        for message_id, (markdown, _buttons) in zip(
            rich.edit_message_ids, rich.edits, strict=True
        )
        if message_id == 91
    ]
    assert any("Checking the growth claim" in markdown for markdown in first_edits)
    assert first_edits[-1] == "The stated growth rate does not reconcile."
    assert "Checking the growth claim" not in first_edits[-1]
    assert any("Choosing the next action" in markdown for markdown in second_edits)
    assert second_edits[-1] == "Correct it before the meeting."
    assert "Choosing the next action" not in second_edits[-1]

    history = bot._history.read(7, since=datetime.min.replace(tzinfo=UTC))
    assert [(item.speaker, item.text) for item in history.messages] == [
        ("human", "Audit the board pack"),
        ("iris", "The stated growth rate does not reconcile."),
        ("iris", "Correct it before the meeting."),
    ]
    assert all("Thinking" not in item.text for item in history.messages)
    assert all("growth claim" not in item.text for item in history.messages)
    assert all("next action" not in item.text for item in history.messages)


async def test_live_activity_coexists_with_body_and_resolves_in_the_same_message(
    message: FakeMessage,
    unthrottled_live_edits: None,
    history_store: TelegramMessageStore,
) -> None:
    rich = FakeRichAPI()
    live = _LiveBubble(cast(Message, message), cast(RichBotAPI, rich), history_store)
    await live.start()

    await live.show_message("## Finding\n\nThe first result is useful.")
    await live.show_activity("Reading mail…")
    await live.show_message(
        "## Finding\n\nThe first result is useful. The second confirms it."
    )
    await live.finish(
        "## Finding\n\nThe first result is useful. The second confirms it."
    )

    assert len(rich.sent) == 1
    assert rich.edits == [
        (
            "## Finding\n\nThe first result is useful.\n\n> ✦ _Writing…_",
            (_LiveBubble._stop_button,),
        ),
        (
            "## Finding\n\nThe first result is useful.\n\n> ✦ _Reading mail…_",
            (_LiveBubble._stop_button,),
        ),
        (
            "## Finding\n\nThe first result is useful. The second confirms it."
            "\n\n> ✦ _Writing…_",
            (_LiveBubble._stop_button,),
        ),
        (
            "## Finding\n\nThe first result is useful. The second confirms it.",
            (),
        ),
    ]
    assert rich.edit_interactions_disabled == [True, True, True, False]


async def test_live_preview_keeps_partial_advanced_block_behind_status(
    message: FakeMessage,
    unthrottled_live_edits: None,
    history_store: TelegramMessageStore,
) -> None:
    rich = FakeRichAPI()
    live = _LiveBubble(cast(Message, message), cast(RichBotAPI, rich), history_store)
    await live.start()

    await live.show_message(
        "## Options\n\n| Choice | Cost |\n|---|---|\n| Local | Low |\n| Cloud"
    )

    assert rich.edits[-1][0] == (
        "## Options\n\n| Choice | Cost |\n|---|---|\n| Local | Low |"
        "\n\n> ✦ _Building a table…_"
    )


async def test_live_edit_throttle_publishes_the_latest_real_state(
    message: FakeMessage,
    monkeypatch: pytest.MonkeyPatch,
    history_store: TelegramMessageStore,
) -> None:
    monkeypatch.setattr(telegram_live, "LIVE_EDIT_INTERVAL_SECONDS", 0.01)
    rich = FakeRichAPI()
    live = _LiveBubble(cast(Message, message), cast(RichBotAPI, rich), history_store)
    await live.start()

    await live.show_message("A useful partial answer.")
    await live.show_activity("Searching the web…")
    await asyncio.sleep(0.03)

    assert [edit[0] for edit in rich.edits] == [
        "A useful partial answer.\n\n> ✦ _Searching the web…_"
    ]
    await live.finish("A useful partial answer.")


async def test_stopping_drops_an_unsafe_tail_without_raw_markup(
    message: FakeMessage,
    unthrottled_live_edits: None,
    history_store: TelegramMessageStore,
) -> None:
    rich = FakeRichAPI()
    live = _LiveBubble(cast(Message, message), cast(RichBotAPI, rich), history_store)
    await live.start()
    partial = "Useful result.\n\n<details><summary>Logs</summary>unfinished"

    await live.stopped(partial)

    assert rich.edits[-1][0] == f"Useful result.\n\n_{STOPPED_MESSAGE}_"
    assert "<details>" not in rich.edits[-1][0]
    assert history_store.read(7, since=datetime.min.replace(tzinfo=UTC)).messages == ()


async def test_rich_message_rejection_is_not_replaced_with_classic_text(
    message: FakeMessage,
    history_store: TelegramMessageStore,
) -> None:
    live = _LiveBubble(
        cast(Message, message),
        cast(RichBotAPI, RejectingRichAPI()),
        history_store,
    )

    with pytest.raises(BadRequest, match="Rich Messages unavailable"):
        await live.start()

    assert message.replies == []


async def test_stopping_wins_over_a_racing_rich_completion(
    message: FakeMessage,
    history_store: TelegramMessageStore,
) -> None:
    rich = FakeRichAPI()
    live = _LiveBubble(cast(Message, message), cast(RichBotAPI, rich), history_store)
    await live.start()

    await live.stopping()

    with pytest.raises(TurnInterrupted):
        await live.finish("Finished too late")
    assert rich.edits[-1][1][0].kind == "disabled"


async def test_stopping_preserves_rich_partial_output_beyond_one_message(
    message: FakeMessage,
    history_store: TelegramMessageStore,
) -> None:
    rich = FakeRichAPI()
    live = _LiveBubble(cast(Message, message), cast(RichBotAPI, rich), history_store)
    await live.start()
    partial = "A" * RICH_MESSAGE_LIMIT + "\n\n" + "B" * 100

    await live.stopped(partial)

    assert rich.edits[-1][0] == "A" * RICH_MESSAGE_LIMIT
    assert rich.edits[-1][1][0].kind == "disabled"
    assert rich.sent[1]["markdown"].endswith(f"_{STOPPED_MESSAGE}_")


async def test_telegram_reply_includes_the_replied_message_text() -> None:
    conversation = FakeConversation(["It means the cache was effective."])
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    replied_message = FakeMessage(message_id=10)
    replied_message.text = "The cache hit rate was 96%."
    message = FakeMessage(message_id=11)
    message.reply_to_message = replied_message

    await bot.handle_text(cast(Message, message), 7, "What does this mean?")

    assert conversation.prompts == [
        "Telegram reply context:\n"
        "<quoted_message>\n"
        "The cache hit rate was 96%.\n"
        "</quoted_message>\n\n"
        "What does this mean?"
    ]


def test_telegram_reply_uses_the_replied_message_caption() -> None:
    replied_message = FakeMessage(message_id=10)
    replied_message.caption = "The first dashboard after setup"

    prompt = turn_text("Why is this empty?", cast(Message, replied_message))

    assert "The first dashboard after setup" in prompt
    assert "Telegram reply context:" in prompt


def test_plain_telegram_input_has_no_storage_instruction_suffix() -> None:
    assert turn_text("How was your day?") == "How was your day?"


async def test_streamed_text_is_shown_as_formatted_message_edits(
    message: FakeMessage, unthrottled_live_edits: None
) -> None:
    conversation = FakeConversation(["Hello", "Hello, Ariadne!"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Say hello")

    rich = cast(FakeRichAPI, bot._rich_api)
    assert rich.edits[0][0].startswith("Hello\n\n")
    assert "Writing…" in rich.edits[0][0]
    assert rich.edits[-1][0] == "Hello, Ariadne!"
    assert message.drafts == []


async def test_final_response_uses_rich_telegram_formatting(
    message: FakeMessage,
) -> None:
    conversation = FakeConversation(["**C++** with `std::vector`"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Format this")

    rich = cast(FakeRichAPI, bot._rich_api)
    assert rich.edits[-1][0] == "**C++** with `std::vector`"


async def test_safe_activity_status_is_shown_before_the_answer(
    message: FakeMessage, unthrottled_live_edits: None
) -> None:
    conversation = FakeConversation(
        ["The answer"],
        activities=["Searching the web…"],
    )
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "Research this")

    rich = cast(FakeRichAPI, bot._rich_api)
    assert any("Searching the web…" in edit for edit, _ in rich.edits)
    assert rich.edits[-1][0] == "The answer"


async def test_new_starts_a_fresh_codex_session(message: FakeMessage) -> None:
    conversation = FakeConversation([])
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_new(cast(Message, message), 7)

    assert conversation.reset_calls == 1
    assert message.replies == [NEW_CONVERSATION_MESSAGE]


async def test_message_during_an_active_turn_steers_it_instead_of_being_rejected() -> (
    None
):
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    first_message = FakeMessage()
    second_message = FakeMessage()

    first_turn = asyncio.create_task(
        bot.handle_text(cast(Message, first_message), 7, "Review the vault")
    )
    await conversation.started.wait()
    await bot.handle_text(
        cast(Message, second_message), 7, "Actually check the other file too"
    )
    conversation.release.set()
    await first_turn

    assert conversation.steered == [turn_text("Actually check the other file too")]
    assert conversation.interrupt_calls == 0
    assert second_message.replies == []
    rich = cast(FakeRichAPI, bot._rich_api)
    assert rich.edits[-1][0] == "Finished"


async def test_multiple_live_followups_keep_arrival_order_without_ack_messages() -> (
    None
):
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    first_message = FakeMessage(message_id=10)
    second_message = FakeMessage(message_id=11)
    third_message = FakeMessage(message_id=12)

    turn = asyncio.create_task(
        bot.handle_text(cast(Message, first_message), 7, "Review the vault")
    )
    await conversation.started.wait()
    await bot.handle_text(cast(Message, second_message), 7, "Check tests too")
    await bot.handle_text(cast(Message, third_message), 7, "Focus on races")
    conversation.release.set()
    await turn

    assert conversation.steered == [
        turn_text("Check tests too"),
        turn_text("Focus on races"),
    ]
    assert second_message.replies == []
    assert third_message.replies == []


async def test_typed_question_answer_resumes_the_turn_without_steering(
    tmp_path: Path,
) -> None:
    state = tmp_path / "questions.sqlite3"
    store = TelegramQuestionStore(state)
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation), question_state=state)
    first_message = FakeMessage(message_id=10)
    answer_message = FakeMessage(message_id=11)

    turn = asyncio.create_task(
        bot.handle_text(cast(Message, first_message), 7, "Plan the deployment")
    )
    await conversation.started.wait()
    question = store.create(7, "Which environment?", ["Staging", "Production"])
    store.attach_message(question.question_id, 90)

    await bot.handle_text(cast(Message, answer_message), 7, "Use canary production")

    answered = store.get(question.question_id)
    assert answered is not None
    assert answered.status == "answered"
    assert answered.answer == "Use canary production"
    assert answered.answer_source == "text"
    assert conversation.steered == []

    conversation.release.set()
    await turn


async def test_question_button_is_atomic_and_double_taps_are_idempotent(
    tmp_path: Path,
) -> None:
    state = tmp_path / "questions.sqlite3"
    store = TelegramQuestionStore(state)
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation), question_state=state)
    first_message = FakeMessage(message_id=10)
    question_message = FakeMessage(message_id=90)

    turn = asyncio.create_task(
        bot.handle_text(cast(Message, first_message), 7, "Deploy this")
    )
    await conversation.started.wait()
    question = store.create(7, "Where?", ["Staging", "Production"])
    store.attach_message(question.question_id, 90)

    selected = bot.handle_question_selection(
        cast(Message, question_message), 7, question.callback_data(1)
    )
    duplicate = bot.handle_question_selection(
        cast(Message, question_message), 7, question.callback_data(1)
    )

    assert selected.outcome == "accepted"
    assert selected.question is not None
    assert selected.question.answer == "Production"
    assert duplicate.outcome == "already_answered"
    assert conversation.steered == []

    conversation.release.set()
    await turn


async def test_stop_cancels_a_question_waiting_inside_the_turn(
    tmp_path: Path,
) -> None:
    state = tmp_path / "questions.sqlite3"
    store = TelegramQuestionStore(state)
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation), question_state=state)
    active_message = FakeMessage(message_id=10)
    stop_message = FakeMessage(message_id=11)

    turn = asyncio.create_task(
        bot.handle_text(cast(Message, active_message), 7, "Deploy this")
    )
    await conversation.started.wait()
    question = store.create(7, "Where?", ["Staging", "Production"])
    store.attach_message(question.question_id, 90)

    await bot.handle_stop(cast(Message, stop_message), 7)
    await turn

    cancelled = store.get(question.question_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert conversation.interrupt_calls == 1


async def test_reply_context_is_preserved_when_steering_an_active_turn() -> None:
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    first_message = FakeMessage(message_id=10)
    second_message = FakeMessage(message_id=11)
    second_message.reply_to_message = first_message
    first_message.text = "Review the vault"

    first_turn = asyncio.create_task(
        bot.handle_text(cast(Message, first_message), 7, first_message.text)
    )
    await conversation.started.wait()
    await bot.handle_text(cast(Message, second_message), 7, "Focus on this request")
    conversation.release.set()
    await first_turn

    assert len(conversation.steered) == 1
    assert "Review the vault" in conversation.steered[0]
    assert "Telegram reply context:" in conversation.steered[0]


async def test_steering_failure_preserves_the_message_as_the_next_turn() -> None:
    conversation = BlockingConversation()
    conversation.steer_error = RuntimeError("Codex refused the steering input")
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    first_message = FakeMessage()
    second_message = FakeMessage()

    first_turn = asyncio.create_task(
        bot.handle_text(cast(Message, first_message), 7, "Review the vault")
    )
    await conversation.started.wait()
    await bot.handle_text(cast(Message, second_message), 7, "One more thing")
    conversation.release.set()
    await first_turn
    assert bot._pending_task is not None
    await asyncio.wait_for(bot._pending_task, timeout=1)

    rich = cast(FakeRichAPI, bot._rich_api)
    assert [edit[0] for edit in rich.edits if edit[0] == "Finished"] == [
        "Finished",
        "Finished",
    ]


async def test_message_sent_before_codex_accepts_the_turn_is_not_lost() -> None:
    conversation = BlockingConversation()
    conversation.steer_accepted = False
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    first_message = FakeMessage()
    second_message = FakeMessage()

    first_turn = asyncio.create_task(
        bot.handle_text(cast(Message, first_message), 7, "Review the vault")
    )
    await conversation.started.wait()
    await bot.handle_text(cast(Message, second_message), 7, "One more thing")
    conversation.release.set()
    await first_turn
    assert bot._pending_task is not None
    await asyncio.wait_for(bot._pending_task, timeout=1)

    assert conversation.steered == []
    rich = cast(FakeRichAPI, bot._rich_api)
    assert [edit[0] for edit in rich.edits if edit[0] == "Finished"] == [
        "Finished",
        "Finished",
    ]


async def test_new_does_not_interrupt_an_active_turn() -> None:
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    active_message = FakeMessage()
    new_message = FakeMessage()

    turn = asyncio.create_task(
        bot.handle_text(cast(Message, active_message), 7, "First")
    )
    await conversation.started.wait()
    await bot.handle_new(cast(Message, new_message), 7)
    conversation.release.set()
    await turn

    assert conversation.reset_calls == 0
    assert new_message.replies == [BUSY_MESSAGE]


async def test_stop_interrupts_the_active_turn_and_frees_the_conversation() -> None:
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    active_message = FakeMessage()
    stop_message = FakeMessage()
    new_message = FakeMessage()

    turn = asyncio.create_task(
        bot.handle_text(cast(Message, active_message), 7, "First")
    )
    await conversation.started.wait()
    await bot.handle_stop(cast(Message, stop_message), 7)
    await turn
    await bot.handle_new(cast(Message, new_message), 7)

    assert conversation.interrupt_calls == 1
    assert stop_message.replies == []
    rich = cast(FakeRichAPI, bot._rich_api)
    assert "Working" in rich.edits[-1][0]
    assert STOPPED_MESSAGE in rich.edits[-1][0]
    assert new_message.replies == [NEW_CONVERSATION_MESSAGE]


async def test_stop_state_wins_when_question_cancellation_races_a_tool_error() -> None:
    conversation = BlockingConversation()
    conversation.failure_after_release = RuntimeError("question tool cancelled")
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    active_message = FakeMessage()
    stop_message = FakeMessage()

    turn = asyncio.create_task(
        bot.handle_text(cast(Message, active_message), 7, "First")
    )
    await conversation.started.wait()
    await bot.handle_stop(cast(Message, stop_message), 7)
    await turn

    rich = cast(FakeRichAPI, bot._rich_api)
    assert STOPPED_MESSAGE in rich.edits[-1][0]
    assert all(FAILURE_MESSAGE not in edit for edit, _ in rich.edits)


async def test_stop_when_idle_has_a_clear_response(message: FakeMessage) -> None:
    bot = AriadneBot(7, cast(CodexConversation, FakeConversation([])))

    await bot.handle_stop(cast(Message, message), 7)

    assert message.replies == [NOTHING_TO_STOP_MESSAGE]


async def test_settings_change_model_effort_and_web_mode(message: FakeMessage) -> None:
    fast_model = CodexModel(
        identifier="gpt-fast",
        display_name="GPT Fast",
        default_effort=ReasoningEffort.low,
        supported_efforts=(ReasoningEffort.low,),
    )
    thorough_model = CodexModel(
        identifier="gpt-thorough",
        display_name="GPT Thorough",
        default_effort=ReasoningEffort.high,
        supported_efforts=(ReasoningEffort.medium, ReasoningEffort.high),
    )
    conversation = FakeConversation([], models=(fast_model, thorough_model))
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_settings(cast(Message, message), 7)
    await bot.handle_settings_callback(
        cast(Message, message),
        7,
        "settings:model:gpt-thorough",
    )
    await bot.handle_settings_callback(
        cast(Message, message),
        7,
        "settings:effort:high",
    )
    await bot.handle_settings_callback(
        cast(Message, message),
        7,
        "settings:web:live",
    )

    assert message.replies[0].startswith("Ariadne settings")
    assert message.reply_markups[0] is not None
    assert conversation.settings == CodexTurnSettings(
        model="gpt-thorough",
        effort=ReasoningEffort.high,
        web_search="live",
    )
    assert conversation.set_settings_calls == 3
    assert conversation.reset_calls == 3
    assert "Web research: Live" in message.edits[-1]


async def test_settings_rejects_changes_during_an_active_turn() -> None:
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    active_message = FakeMessage()
    settings_message = FakeMessage()

    turn = asyncio.create_task(
        bot.handle_text(cast(Message, active_message), 7, "First")
    )
    await conversation.started.wait()
    await bot.handle_settings_callback(
        cast(Message, settings_message),
        7,
        "settings:web:live",
    )
    conversation.release.set()
    await turn

    assert conversation.settings.web_search == "disabled"
    assert settings_message.edits == [SETTINGS_BUSY_MESSAGE]


async def test_response_beyond_the_rich_limit_keeps_rich_chunks(
    message: FakeMessage,
) -> None:
    response = "A" * RICH_MESSAGE_LIMIT + "\n\n" + "B" * 100
    conversation = FakeConversation([response])
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    rich = FakeRichAPI()
    bot._rich_api = cast(RichBotAPI, rich)

    await bot.handle_text(cast(Message, message), 7, "Long rich answer")

    assert len(rich.sent) == 2
    assert rich.edits[-1][0] == "A" * RICH_MESSAGE_LIMIT
    assert rich.sent[1]["markdown"] == "B" * 100


async def test_failed_turn_replies_and_allows_the_next_turn(
    message: FakeMessage,
) -> None:
    conversation = FakeConversation(["Recovered"], failures=1)
    bot = AriadneBot(7, cast(CodexConversation, conversation))

    await bot.handle_text(cast(Message, message), 7, "First")

    next_message = FakeMessage()
    await bot.handle_text(cast(Message, next_message), 7, "Second")

    rich = cast(FakeRichAPI, bot._rich_api)
    assert [edit[0] for edit in rich.edits[-2:]] == [FAILURE_MESSAGE, "Recovered"]
    assert conversation.prompts == [turn_text("First"), turn_text("Second")]


def test_document_message_uses_the_caption_as_the_request(tmp_path) -> None:
    path = tmp_path / "cv.pdf"

    text = build_document_turn_prompt(
        "compare this with the one in my repo", [(path, "application/pdf")]
    )

    assert text.startswith("compare this with the one in my repo")
    assert f"Attached file: {path} (application/pdf)" in text


def test_document_message_without_a_caption_invents_no_task(tmp_path) -> None:
    path = tmp_path / "rows.csv"

    text = build_document_turn_prompt(None, [(path, None)])

    assert text == f"{DOCUMENT_WITHOUT_CAPTION}\n\nAttached file: {path}"


async def test_document_sent_during_a_turn_steers_it_and_is_kept(
    tmp_path,
) -> None:
    conversation = BlockingConversation()
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    attachment = tmp_path / "sent" / "rows.csv"
    attachment.parent.mkdir()
    attachment.write_text("a,b\n1,2\n", encoding="utf-8")

    first_turn = asyncio.create_task(
        bot.handle_text(cast(Message, FakeMessage()), 7, "Review the vault")
    )
    await conversation.started.wait()
    await bot.handle_document(
        cast(Message, FakeMessage()), 7, attachment, caption="what looks odd here?"
    )

    assert conversation.steered == [
        turn_text(
            build_document_turn_prompt("what looks odd here?", [(attachment, None)])
        )
    ]

    conversation.release.set()
    await first_turn

    assert attachment.exists()


async def test_document_is_kept_after_the_turn_it_started(tmp_path) -> None:
    conversation = FakeConversation(["Looked at it"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    attachment = tmp_path / "sent" / "notes.txt"
    attachment.parent.mkdir()
    attachment.write_text("hello", encoding="utf-8")

    await bot.handle_document(cast(Message, FakeMessage()), 7, attachment)

    assert conversation.prompts == [
        turn_text(build_document_turn_prompt(None, [(attachment, None)]))
    ]
    assert attachment.exists()
    history = bot._history.read(7, since=datetime.min.replace(tzinfo=UTC))
    assert history.messages[0].content_type == "document"
    assert history.messages[0].text == "[Document: notes.txt]"
    assert str(attachment) not in history.messages[0].text


async def test_a_media_group_becomes_one_turn(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(telegram_bot, "ALBUM_DEBOUNCE_SECONDS", 0.01)
    conversation = FakeConversation(["Looked at them"])
    bot = AriadneBot(7, cast(CodexConversation, conversation))
    message = FakeMessage()
    message.media_group_id = "album-1"
    paths = []
    for name in ("first.csv", "second.csv"):
        path = tmp_path / name.removesuffix(".csv") / name
        path.parent.mkdir()
        path.write_text("a,b\n", encoding="utf-8")
        paths.append(path)

    await bot.handle_document(
        cast(Message, message), 7, paths[0], caption="what looks odd here?"
    )
    await bot.handle_document(cast(Message, message), 7, paths[1])

    assert conversation.prompts == []

    await asyncio.sleep(0.05)

    assert conversation.prompts == [
        turn_text(
            build_document_turn_prompt(
                "what looks odd here?", [(paths[0], None), (paths[1], None)]
            )
        )
    ]


def test_attachments_are_kept_under_a_folder_for_today(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(telegram_bot, "ATTACHMENT_ROOT", tmp_path)

    first = telegram_bot.attachment_path("cv.pdf")
    first.write_bytes(b"one")
    second = telegram_bot.attachment_path("cv.pdf")

    assert first.parent == tmp_path / date.today().isoformat()
    assert first.name == "cv.pdf"
    assert second.name == "cv-2.pdf"


def test_attachment_path_reserves_names_before_download(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(telegram_bot, "ATTACHMENT_ROOT", tmp_path)

    first = telegram_bot.attachment_path("photo.jpg")
    second = telegram_bot.attachment_path("photo.jpg")

    assert first.name == "photo.jpg"
    assert second.name == "photo-2.jpg"
    assert first.exists()
    assert second.exists()
