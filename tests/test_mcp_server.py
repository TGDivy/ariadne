import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from caldav.lib.error import PutError
from fastmcp import Client
from fastmcp.exceptions import ToolError
from telegram.error import BadRequest, EndPointNotFound

from ariadne.mail import MailState
from ariadne.mcp import calendar as calendar_tools
from ariadne.mcp import mcp
from ariadne.mcp import telegram as telegram_tools
from ariadne.mcp.calendar import (
    create_calendar_event,
    delete_calendar_event,
    list_calendars,
)
from ariadne.mcp.errors import DIAGNOSTIC_PREFIX
from ariadne.mcp.mail import read_mail, search_mail, triage_current_mail
from ariadne.mcp.runtime import runtime_status
from ariadne.mcp.telegram import ask_telegram_question, send_telegram_message
from ariadne.telegram import outbound
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
def telegram(monkeypatch) -> FakeBot:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token-for-test")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "7")
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
        "runtime_status",
        "send_telegram_message",
        "ask_telegram_question",
        "prepare_files",
        "search_mail",
        "read_mail",
        "read_mail_thread",
        "triage_current_mail",
        "list_calendars",
        "search_calendar",
        "read_calendar_event",
        "calendar_free_busy",
        "create_calendar_event",
        "update_calendar_event",
        "delete_calendar_event",
        "respond_to_calendar_invitation",
        "search_knowledge",
        "browse_knowledge",
        "read_knowledge",
        "create_knowledge",
        "update_knowledge",
        "archive_knowledge",
    ]


def test_a_normal_turn_has_no_mail_authority(monkeypatch) -> None:
    monkeypatch.delenv("ARIADNE_MAIL_JOB_ID", raising=False)
    monkeypatch.delenv("ARIADNE_MAIL_STATE", raising=False)

    with pytest.raises(ToolError, match="unavailable"):
        triage_current_mail("notifications", "important", "keep_in_inbox")


def test_mail_reading_requires_toml_derived_credentials(monkeypatch) -> None:
    monkeypatch.delenv("ARIADNE_MAIL_USERNAME", raising=False)
    monkeypatch.delenv("ARIADNE_MAIL_APP_PASSWORD", raising=False)

    with pytest.raises(ToolError, match="not configured"):
        search_mail("Example Sender")
    with pytest.raises(ToolError, match="not configured"):
        read_mail("mail:anything")


def test_calendar_requires_toml_derived_credentials(monkeypatch) -> None:
    for name in (
        "ARIADNE_ICLOUD_USERNAME",
        "ARIADNE_ICLOUD_APP_PASSWORD",
        "ARIADNE_CALENDAR_TIMEZONE",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ToolError, match="not configured"):
        list_calendars()
    with pytest.raises(ToolError, match="not configured"):
        create_calendar_event("Title", "2026-09-01", "2026-09-02")


def test_calendar_mutations_use_only_configured_account_and_calendar(
    monkeypatch,
) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeCalendar:
        def __init__(self, username: str, password: str, **kwargs: object) -> None:
            assert username == "person@example.com"
            assert password == "app-password"
            assert kwargs == {
                "timezone": "Europe/London",
                "default_calendar": "Personal",
            }

        def __enter__(self) -> "FakeCalendar":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def create_event(self, **kwargs: Any) -> dict[str, Any]:
            calls.append(("create", kwargs))
            return {"id": "calendar-event:new"}

        def delete_event(self, value: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(("delete", {"id": value, **kwargs}))
            return {"status": "deleted"}

    monkeypatch.setenv("ARIADNE_ICLOUD_USERNAME", "person@example.com")
    monkeypatch.setenv("ARIADNE_ICLOUD_APP_PASSWORD", "app-password")
    monkeypatch.setenv("ARIADNE_CALENDAR_TIMEZONE", "Europe/London")
    monkeypatch.setenv("ARIADNE_CALENDAR_DEFAULT", "Personal")
    monkeypatch.setattr(calendar_tools, "ICloudCalendar", FakeCalendar)

    created = create_calendar_event(
        "Review", "2026-09-01T09:00:00", "2026-09-01T10:00:00"
    )
    deleted = delete_calendar_event("calendar-event:new", scope="series")

    assert created == {"id": "calendar-event:new"}
    assert deleted == {"status": "deleted"}
    assert calls[0][0] == "create"
    assert calls[0][1]["title"] == "Review"
    assert calls[0][1]["timezone"] is None
    assert calls[1] == (
        "delete",
        {"id": "calendar-event:new", "scope": "series", "expected_etag": None},
    )


async def test_calendar_write_failure_exposes_redacted_provider_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingCalendar:
        def __init__(self, username: str, password: str, **_: object) -> None:
            assert username == "person@example.com"
            assert password == "calendar-password"

        def __enter__(self) -> "FailingCalendar":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def create_event(self, **_: object) -> dict[str, Any]:
            raise PutError(
                "400 Bad Request\n\n"
                "<error><message>Invalid iCalendar data</message>"
                "<password>calendar-password</password>"
                "<username>person@example.com</username>"
                "<authorization>Basic dGVzdDpzZWNyZXQ=</authorization></error>"
            )

    monkeypatch.setenv("ARIADNE_ICLOUD_USERNAME", "person@example.com")
    monkeypatch.setenv("ARIADNE_ICLOUD_APP_PASSWORD", "calendar-password")
    monkeypatch.setenv("ARIADNE_CALENDAR_TIMEZONE", "Europe/London")
    monkeypatch.setattr(calendar_tools, "ICloudCalendar", FailingCalendar)

    async with Client(mcp) as client:
        result = await client.call_tool_mcp(
            "create_calendar_event",
            {
                "title": "Review",
                "start": "2026-09-01T09:00:00",
                "end": "2026-09-01T10:00:00",
            },
        )

    assert result.isError is True
    text = result.content[0].text
    diagnostic = json.loads(text.split(DIAGNOSTIC_PREFIX, 1)[1])
    assert "iCloud Calendar could not complete that operation." in text
    assert diagnostic == {
        "exception_type": "PutError",
        "operation": "create_calendar_event",
        "http_status": 400,
        "provider_response_body": (
            "<error><message>Invalid iCalendar data</message>"
            "<password>[REDACTED]</password>"
            "<username>[REDACTED]</username>"
            "<authorization>[REDACTED]</authorization></error>"
        ),
    }
    assert "calendar-password" not in text
    assert "person@example.com" not in text
    assert "dGVzdDpzZWNyZXQ=" not in text


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("send_telegram_message", {"text": " "}),
        ("create_calendar_event", {"title": "Missing dates"}),
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

    result = triage_current_mail(
        "career", "important", "flag", "Thanks — I am interested."
    )

    job = state.get(job_id)
    assert result == {"status": "recorded", "job_id": job_id}
    assert job is not None
    assert job.suggested_action == "flag"
    assert job.draft_reply == "Thanks — I am interested."
    assert job.action is None


def test_runtime_status_never_returns_environment_values(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("ARIADNE_VAULT", str(vault))
    monkeypatch.setenv("ARIADNE_PROFILE", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-appear")

    payload = runtime_status()

    assert payload["vault"] == str(vault)
    assert payload["git"] == {
        "root": str(vault),
        "available": False,
        "reason": "not_a_repository",
    }
    assert "must-not-appear" not in json.dumps(payload)


@pytest.mark.parametrize("missing", ["ARIADNE_VAULT", "ARIADNE_PROFILE"])
def test_runtime_status_requires_explicit_context(
    missing: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARIADNE_VAULT", str(tmp_path))
    monkeypatch.setenv("ARIADNE_PROFILE", "telegram")
    monkeypatch.delenv(missing)

    with pytest.raises(ToolError, match=f"missing {missing}"):
        runtime_status()


def test_runtime_status_rejects_an_unknown_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARIADNE_VAULT", str(tmp_path))
    monkeypatch.setenv("ARIADNE_PROFILE", "unknown")

    with pytest.raises(ToolError, match="not recognized"):
        runtime_status()


def test_runtime_status_reports_the_current_profiles_capabilities(monkeypatch) -> None:
    monkeypatch.setenv("ARIADNE_VAULT", str(Path.cwd()))
    monkeypatch.setenv("ARIADNE_PROFILE", "mail")

    payload = runtime_status()

    assert payload["capabilities"][-1] == "triage_current_mail"


async def test_a_message_from_iris_is_sent_as_rich_markdown(telegram: FakeBot) -> None:
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
