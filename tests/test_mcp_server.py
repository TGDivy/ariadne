import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastmcp.exceptions import ToolError
from telegram.constants import ParseMode

from ariadne import mcp_server
from ariadne.mail import MailState
from ariadne.mcp_server import (
    mcp,
    react,
    runtime_status,
    send_message,
    triage_current_mail,
)
from ariadne.telegram.format import TELEGRAM_MESSAGE_LIMIT


class FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token
        self.sent: list[dict[str, Any]] = []
        self.reactions: list[dict[str, Any]] = []

    async def __aenter__(self) -> "FakeBot":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def send_message(self, **kwargs: Any) -> SimpleNamespace:
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=100 + len(self.sent))

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


async def test_fastmcp_lists_every_capability_ariadne_offers() -> None:
    tools = await mcp.list_tools()

    assert [tool.name for tool in tools] == [
        "runtime_status",
        "send_message",
        "react",
        "prepare_files",
        "triage_current_mail",
    ]


def test_a_normal_turn_has_no_mail_authority(monkeypatch) -> None:
    monkeypatch.delenv("ARIADNE_MAIL_JOB_ID", raising=False)
    monkeypatch.delenv("ARIADNE_MAIL_STATE", raising=False)

    with pytest.raises(ToolError, match="unavailable"):
        triage_current_mail("notifications", "important", "keep_in_inbox")


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


async def test_a_message_from_iris_is_sent_as_telegram_html(telegram: FakeBot) -> None:
    message_ids = await send_message("The **latest** one is from June.")

    assert message_ids == [101]
    assert telegram.sent == [
        {
            "chat_id": 7,
            "text": "The <b>latest</b> one is from June.",
            "parse_mode": ParseMode.HTML,
            "reply_parameters": None,
        }
    ]


async def test_a_long_message_from_iris_is_split_at_telegrams_limit(
    telegram: FakeBot,
) -> None:
    text = "x" * (TELEGRAM_MESSAGE_LIMIT + 1)

    message_ids = await send_message(text)

    assert message_ids == [101, 102]
    assert [sent["text"] for sent in telegram.sent] == [
        "x" * TELEGRAM_MESSAGE_LIMIT,
        "x",
    ]
    assert [sent["parse_mode"] for sent in telegram.sent] == [None, None]


async def test_iris_can_reply_to_the_message_she_is_answering(
    telegram: FakeBot,
) -> None:
    await send_message("Found it.", reply_to_message_id=42)

    assert telegram.sent[0]["reply_parameters"].message_id == 42


async def test_an_empty_message_is_refused(telegram: FakeBot) -> None:
    with pytest.raises(ToolError):
        await send_message("   ")

    assert telegram.sent == []


async def test_a_reaction_sets_it_on_the_telegram_message(telegram: FakeBot) -> None:
    await react(42, "❤️")

    assert telegram.reactions == [{"chat_id": 7, "message_id": 42, "reaction": "❤"}]


async def test_an_unsupported_reaction_is_refused(telegram: FakeBot) -> None:
    with pytest.raises(ToolError):
        await react(42, "🦖")

    assert telegram.reactions == []
