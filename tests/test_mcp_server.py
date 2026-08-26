import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from telegram.constants import ParseMode
from telegram.error import BadRequest, EndPointNotFound

from ariadne import mcp_server
from ariadne.mail import MailState
from ariadne.mcp_server import (
    ask_telegram_question,
    mcp,
    react,
    read_mail,
    runtime_status,
    search_mail,
    send_telegram_message,
    triage_current_mail,
)
from ariadne.telegram.questions import TelegramQuestion, TelegramQuestionStore


class FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token
        self.reject_rich = False
        self.missing_rich_endpoint = False
        self.sent: list[dict[str, Any]] = []
        self.api_calls: list[tuple[str, dict[str, Any]]] = []
        self.reactions: list[dict[str, Any]] = []

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

    async def set_message_reaction(self, **kwargs: Any) -> bool:
        self.reactions.append(kwargs)
        return True


@pytest.fixture
def telegram(monkeypatch) -> FakeBot:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token-for-test")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "7")
    bot = FakeBot("token-for-test")
    monkeypatch.setattr(mcp_server, "Bot", lambda token: bot)
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
        "react",
        "ask_telegram_question",
        "prepare_files",
        "search_mail",
        "read_mail",
        "read_mail_thread",
        "triage_current_mail",
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
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-appear")

    payload = runtime_status()

    assert payload["vault"] == str(vault)
    assert "must-not-appear" not in json.dumps(payload)


def test_runtime_status_reports_the_current_profiles_capabilities(monkeypatch) -> None:
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


async def test_iris_can_reply_to_the_message_she_is_answering(
    telegram: FakeBot,
) -> None:
    await send_telegram_message("Found it.", reply_to_message_id=42)

    assert telegram.api_calls[0][1]["reply_parameters"] == {
        "message_id": 42,
        "allow_sending_without_reply": True,
    }


async def test_an_empty_message_is_refused(telegram: FakeBot) -> None:
    with pytest.raises(ToolError):
        await send_telegram_message("   ")

    assert telegram.sent == []
    assert telegram.api_calls == []


async def test_rich_delivery_rejection_falls_back_to_classic_html(
    telegram: FakeBot,
) -> None:
    telegram.reject_rich = True

    message_ids = await send_telegram_message("**Still formatted**")

    assert message_ids == [101]
    assert telegram.sent[0]["text"] == "<b>Still formatted</b>"
    assert telegram.sent[0]["parse_mode"] == ParseMode.HTML


async def test_missing_rich_endpoint_falls_back_to_classic_html(
    telegram: FakeBot,
) -> None:
    telegram.missing_rich_endpoint = True

    message_ids = await send_telegram_message("**Still formatted**")

    assert message_ids == [101]
    assert telegram.sent[0]["text"] == "<b>Still formatted</b>"


async def test_a_reaction_sets_it_on_the_telegram_message(telegram: FakeBot) -> None:
    await react(42, "❤️")

    assert telegram.reactions == [{"chat_id": 7, "message_id": 42, "reaction": "❤"}]


async def test_an_unsupported_reaction_is_refused(telegram: FakeBot) -> None:
    with pytest.raises(ToolError):
        await react(42, "🦖")

    assert telegram.reactions == []


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
            reply_to_message_id=42,
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
    assert telegram.api_calls[0][1]["reply_parameters"]["message_id"] == 42


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
