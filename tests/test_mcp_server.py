import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from telegram.error import BadRequest, EndPointNotFound

from ariadne.mail import MailState
from ariadne.mcp import mcp
from ariadne.mcp import telegram as telegram_tools
from ariadne.mcp.errors import DIAGNOSTIC_PREFIX
from ariadne.mcp.mail import record_current_mail_decision
from ariadne.mcp.telegram import (
    ask_telegram_question,
    read_recent_telegram_messages,
    send_telegram_message,
)
from ariadne.telegram import outbound
from ariadne.telegram.history import TelegramHistoryMessage, TelegramMessageStore
from ariadne.telegram.questions import TelegramQuestion, TelegramQuestionStore


class FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token
        self.reject_rich = False
        self.missing_rich_endpoint = False
        self.sent: list[dict[str, Any]] = []
        self.api_calls: list[tuple[str, dict[str, Any]]] = []

    async def __aenter__(self) -> "FakeBot":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def send_message(self, **kwargs: Any) -> SimpleNamespace:
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def do_api_request(
        self,
        endpoint: str,
        api_kwargs: dict[str, Any] | None = None,
        **_: object,
    ) -> SimpleNamespace:
        arguments = api_kwargs or {}
        if endpoint == "sendRichMessage" and self.missing_rich_endpoint:
            raise EndPointNotFound("Rich Messages unavailable")
        if endpoint == "sendRichMessage" and self.reject_rich:
            raise BadRequest("Rich Messages unavailable")
        self.api_calls.append((endpoint, arguments))
        return SimpleNamespace(message_id=100 + len(self.api_calls))


@pytest.fixture
def telegram(monkeypatch, tmp_path: Path) -> FakeBot:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token-for-test")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "7")
    monkeypatch.setenv("ARIADNE_PROFILE", "mail")
    monkeypatch.setenv("ARIADNE_TELEGRAM_STATE", str(tmp_path / "telegram.sqlite3"))
    bot = FakeBot("token-for-test")
    monkeypatch.setattr(telegram_tools, "Bot", lambda token: bot)
    monkeypatch.setattr(outbound, "Bot", lambda token: bot)
    return bot


async def wait_for_question(store: TelegramQuestionStore) -> TelegramQuestion:
    for _ in range(100):
        question = store.pending(7)
        if question is not None and question.message_id is not None:
            return question
        await asyncio.sleep(0.01)
    raise AssertionError("The question card was not sent")


async def test_fastmcp_lists_every_capability_ariadne_offers() -> None:
    tools = await mcp.list_tools()

    assert [tool.name for tool in tools] == [
        "send_telegram_message",
        "read_recent_telegram_messages",
        "ask_telegram_question",
        "request_telegram_file_delivery",
        "record_current_mail_decision",
        "search_knowledge",
        "list_knowledge",
        "read_knowledge",
        "create_knowledge",
        "update_knowledge",
        "archive_knowledge",
        "schedule_wakeup",
        "list_wakeups",
        "update_wakeup",
        "cancel_wakeup",
    ]


def test_a_normal_turn_has_no_mail_authority(monkeypatch) -> None:
    monkeypatch.delenv("ARIADNE_MAIL_JOB_ID", raising=False)
    monkeypatch.delenv("ARIADNE_MAIL_STATE", raising=False)

    with pytest.raises(ToolError, match="unavailable"):
        record_current_mail_decision("notifications", "important", "keep_in_inbox")


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("send_telegram_message", {"text": " "}),
        ("tool_that_does_not_exist", {}),
    ],
)
async def test_every_raised_mcp_tool_failure_has_a_complete_diagnostic(
    operation: str, arguments: dict[str, Any]
) -> None:
    async with Client(mcp) as client:
        result = await client.call_tool_mcp(operation, arguments)

    assert result.isError is True
    text = result.content[0].text
    diagnostic = json.loads(text.split(DIAGNOSTIC_PREFIX, 1)[1])
    assert diagnostic.keys() == {
        "exception_type",
        "operation",
        "http_status",
        "provider_response_body",
    }
    assert diagnostic["exception_type"]
    assert diagnostic["operation"] == operation
    assert diagnostic["http_status"] is None
    assert diagnostic["provider_response_body"] is None


def test_a_mail_turn_can_record_but_not_execute_its_decision(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "mail.sqlite3"
    state = MailState(path)
    state.initialize()
    state.discover("INBOX", 1, [2])
    job_id = MailState.job_id("INBOX", 1, 2)
    state.start(job_id)
    monkeypatch.setenv("ARIADNE_MAIL_JOB_ID", job_id)
    monkeypatch.setenv("ARIADNE_MAIL_STATE", str(path))

    result = record_current_mail_decision(
        "career", "important", "flag", "Thanks — I am interested."
    )

    job = state.get(job_id)
    assert result == {"status": "recorded", "job_id": job_id}
    assert job is not None
    assert job.suggested_action == "flag"
    assert job.draft_reply == "Thanks — I am interested."
    assert job.action is None


async def test_a_message_from_iris_is_sent_as_rich_markdown(
    telegram: FakeBot, tmp_path: Path
) -> None:
    message_ids = await send_telegram_message("The **latest** one is from June.")

    assert message_ids == [101]
    assert telegram.api_calls == [
        (
            "sendRichMessage",
            {
                "chat_id": 7,
                "rich_message": {"markdown": "The **latest** one is from June."},
            },
        )
    ]
    history = TelegramMessageStore(tmp_path / "telegram.sqlite3").read(
        7, since=datetime.min.replace(tzinfo=UTC)
    )
    assert [(item.speaker, item.source, item.text) for item in history.messages] == [
        ("iris", "mail", "The **latest** one is from June.")
    ]


async def test_revisit_delivery_is_identified_as_a_wakeup(
    telegram: FakeBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARIADNE_PROFILE", "revisit-light")

    await send_telegram_message("Still worth packing your gels.")

    history = TelegramMessageStore(tmp_path / "telegram.sqlite3").read(
        7, since=datetime.min.replace(tzinfo=UTC)
    )
    assert history.messages[0].source == "wakeup"


def test_recent_telegram_messages_exposes_bounded_filtered_history(
    telegram: FakeBot, tmp_path: Path
) -> None:
    del telegram
    state = TelegramMessageStore(tmp_path / "telegram.sqlite3")
    start = datetime(2026, 8, 29, 16, tzinfo=UTC)
    state.record(
        TelegramHistoryMessage(
            chat_id=7,
            message_id=1,
            sent_at=start,
            speaker="human",
            source="telegram",
            content_type="text",
            text="I have packed everything and sorted the bib",
        )
    )
    state.record(
        TelegramHistoryMessage(
            chat_id=7,
            message_id=2,
            sent_at=start + timedelta(minutes=1),
            speaker="iris",
            source="mail",
            content_type="text",
            text="Train booked.",
            reply_to_message_id=1,
        )
    )

    result = read_recent_telegram_messages(
        start.isoformat(),
        before=(start + timedelta(hours=1)).isoformat(),
        speakers=["human"],
        sources=["telegram"],
        query="PACKED EVERYTHING",
        limit=1,
    )

    assert result == {
        "messages": [
            {
                "message_id": 1,
                "sent_at": start.isoformat(),
                "speaker": "human",
                "source": "telegram",
                "content_type": "text",
                "text": "I have packed everything and sorted the bib",
                "reply_to_message_id": None,
            }
        ],
        "total": 1,
        "truncated": False,
        "earliest_available_at": start.isoformat(),
    }


@pytest.mark.parametrize(
    ("since", "before", "error"),
    [
        ("2026-08-29T10:00:00", None, "timezone offset"),
        ("not-a-time", None, "valid ISO 8601"),
        (
            "2026-08-29T11:00:00+00:00",
            "2026-08-29T10:00:00+00:00",
            "later than since",
        ),
    ],
)
def test_recent_telegram_messages_rejects_ambiguous_time_bounds(
    telegram: FakeBot, since: str, before: str | None, error: str
) -> None:
    del telegram
    with pytest.raises(ToolError, match=error):
        read_recent_telegram_messages(since, before)


async def test_a_message_beyond_the_classic_limit_stays_one_rich_message(
    telegram: FakeBot,
) -> None:
    text = "x" * 4_097

    message_ids = await send_telegram_message(text)

    assert message_ids == [101]
    assert telegram.api_calls[0][1]["rich_message"] == {"markdown": text}


async def test_an_empty_message_is_refused(telegram: FakeBot) -> None:
    with pytest.raises(ToolError):
        await send_telegram_message("   ")

    assert telegram.sent == []
    assert telegram.api_calls == []


async def test_telegram_delivery_requires_an_explicit_destination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_ALLOWED_USER_ID", raising=False)

    with pytest.raises(ToolError, match="not reachable"):
        await send_telegram_message("Hello")


async def test_rich_delivery_rejection_has_no_classic_transport_fallback(
    telegram: FakeBot,
) -> None:
    telegram.reject_rich = True

    with pytest.raises(ToolError, match="could not deliver"):
        await send_telegram_message("**Still formatted**")

    assert telegram.sent == []


async def test_missing_rich_endpoint_has_no_classic_transport_fallback(
    telegram: FakeBot,
) -> None:
    telegram.missing_rich_endpoint = True

    with pytest.raises(ToolError, match="could not deliver"):
        await send_telegram_message("**Still formatted**")

    assert telegram.sent == []


async def test_delivery_history_failure_reports_that_message_was_already_sent(
    telegram: FakeBot, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FailingHistory:
        def __init__(self, _path: Path) -> None:
            pass

        def initialize(self) -> None:
            pass

        def record(self, _message: TelegramHistoryMessage) -> None:
            raise sqlite3.OperationalError("disk became read-only")

    monkeypatch.setattr(telegram_tools, "TelegramMessageStore", FailingHistory)

    with pytest.raises(
        ToolError, match=r"delivered message ID\(s\) 101.*Do not resend"
    ):
        await send_telegram_message("Already sent once")

    assert telegram.api_calls[0][0] == "sendRichMessage"


async def test_a_typed_answer_resumes_the_waiting_question_tool(
    telegram: FakeBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "questions.sqlite3"
    monkeypatch.setenv("ARIADNE_TELEGRAM_STATE", str(state))
    store = TelegramQuestionStore(state)

    waiting = asyncio.create_task(
        ask_telegram_question(
            "Which environment should I use?",
            ["Staging", "Production"],
        )
    )
    question = await wait_for_question(store)
    store.answer_text(7, "Production with a canary")

    result = await asyncio.wait_for(waiting, timeout=1)

    assert result == {
        "status": "answered",
        "answer": "Production with a canary",
        "source": "text",
    }
    assert question.message_id == 101
    assert telegram.api_calls[0][0] == "sendRichMessage"
    history = TelegramMessageStore(state).read(
        7, since=datetime.min.replace(tzinfo=UTC)
    )
    assert [(item.speaker, item.text) for item in history.messages] == [
        ("iris", "Which environment should I use?")
    ]


async def test_a_button_answer_resumes_the_waiting_question_tool(
    telegram: FakeBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "questions.sqlite3"
    monkeypatch.setenv("ARIADNE_TELEGRAM_STATE", str(state))
    store = TelegramQuestionStore(state)

    waiting = asyncio.create_task(
        ask_telegram_question("Choose one", ["Local", "Staging", "Production"])
    )
    question = await wait_for_question(store)
    selection = store.answer_choice(
        question.question_id,
        chat_id=7,
        message_id=question.message_id or 0,
        choice_index=2,
    )

    result = await asyncio.wait_for(waiting, timeout=1)

    assert selection.outcome == "accepted"
    assert result == {
        "status": "answered",
        "answer": "Production",
        "source": "button",
    }


async def test_cancelling_the_tool_disables_its_pending_question(
    telegram: FakeBot, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "questions.sqlite3"
    monkeypatch.setenv("ARIADNE_TELEGRAM_STATE", str(state))
    store = TelegramQuestionStore(state)

    waiting = asyncio.create_task(ask_telegram_question("Choose", ["A", "B"]))
    question = await wait_for_question(store)
    waiting.cancel()

    with pytest.raises(asyncio.CancelledError):
        await waiting

    cancelled = store.get(question.question_id)
    assert cancelled is not None
    assert cancelled.status == "cancelled"
    assert telegram.api_calls[-1][0] == "editMessageText"
