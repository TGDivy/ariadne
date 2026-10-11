import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from ariadne.conversation.log import ConversationLog
from ariadne.conversation.models import MessageDraft
from ariadne.conversation.questions import Question, QuestionStore
from ariadne.conversation.shares import SharedFiles
from ariadne.mail import MailState
from ariadne.mcp import mcp
from ariadne.mcp.conversation import ask_question, read_recent_conversation, share_files
from ariadne.mcp.errors import DIAGNOSTIC_PREFIX
from ariadne.mcp.mail import record_current_mail_decision


@pytest.fixture
def state(monkeypatch, tmp_path: Path) -> Path:
    path = tmp_path / "conversation.sqlite3"
    monkeypatch.setenv("ARIADNE_CONVERSATION_STATE", str(path))
    return path


async def presented_question(store: QuestionStore) -> Question:
    """Play the conversation core's part: present the waiting question."""
    for _ in range(200):
        question = store.pending()
        if question is not None:
            presented = store.attach_message(question.question_id, 1)
            assert presented is not None
            return presented
        await asyncio.sleep(0.01)
    raise AssertionError("The question was never created")


async def test_fastmcp_lists_every_capability_ariadne_offers() -> None:
    tools = await mcp.list_tools()

    assert [tool.name for tool in tools] == [
        "read_recent_conversation",
        "ask_question",
        "share_files",
        "hand_off_to_conversation",
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
        "record_stewardship_outcome",
    ]


def test_a_normal_turn_has_no_mail_authority(monkeypatch) -> None:
    monkeypatch.delenv("ARIADNE_MAIL_JOB_ID", raising=False)
    monkeypatch.delenv("ARIADNE_MAIL_STATE", raising=False)

    with pytest.raises(ToolError, match="unavailable"):
        record_current_mail_decision("notifications", "important", "keep_in_inbox")


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("share_files", {"paths": []}),
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


def test_recent_conversation_exposes_bounded_filtered_history(state: Path) -> None:
    log = ConversationLog(state)
    start = datetime(2026, 8, 29, 16, tzinfo=UTC)
    log.append(
        MessageDraft(
            speaker="human",
            origin="direct",
            kind="text",
            text="I have packed everything and sorted the bib",
            sent_at=start,
        )
    )
    log.append(
        MessageDraft(
            speaker="iris",
            origin="proactive",
            kind="text",
            text="Train booked.",
            sent_at=start + timedelta(minutes=1),
        )
    )

    result = read_recent_conversation(
        start.isoformat(),
        before=(start + timedelta(hours=1)).isoformat(),
        speakers=["human"],
        origins=["direct"],
        query="PACKED EVERYTHING",
        limit=1,
    )

    assert result == {
        "messages": [
            {
                "message_id": 1,
                "sent_at": start.isoformat(),
                "speaker": "human",
                "origin": "direct",
                "kind": "text",
                "text": "I have packed everything and sorted the bib",
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
def test_recent_conversation_rejects_ambiguous_time_bounds(
    state: Path, since: str, before: str | None, error: str
) -> None:
    with pytest.raises(ToolError, match=error):
        read_recent_conversation(since, before)


async def test_a_typed_answer_resumes_the_waiting_question_tool(state: Path) -> None:
    task = asyncio.create_task(ask_question("Which environment?", ["Local", "Staging"]))
    store = QuestionStore(state)
    await presented_question(store)

    assert store.answer_text("Staging with a canary") is not None

    assert await asyncio.wait_for(task, 2) == {
        "status": "answered",
        "answer": "Staging with a canary",
        "source": "text",
    }


async def test_a_button_answer_resumes_the_waiting_question_tool(state: Path) -> None:
    task = asyncio.create_task(ask_question("Which environment?", ["Local", "Staging"]))
    store = QuestionStore(state)
    question = await presented_question(store)

    assert store.answer_choice(question.question_id, 1).outcome == "accepted"

    assert (await asyncio.wait_for(task, 2))["answer"] == "Staging"


async def test_a_second_question_cannot_wait_beside_the_first(state: Path) -> None:
    first = asyncio.create_task(
        ask_question("Which environment?", ["Local", "Staging"])
    )
    await presented_question(QuestionStore(state))

    with pytest.raises(ToolError, match="already waiting"):
        await ask_question("And which region?", ["EU", "US"])

    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_cancelling_the_tool_cancels_its_pending_question(state: Path) -> None:
    task = asyncio.create_task(ask_question("Which environment?", ["Local", "Staging"]))
    store = QuestionStore(state)
    question = await presented_question(store)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    cancelled = store.get(question.question_id)
    assert cancelled is not None and cancelled.status == "cancelled"


def test_sharing_stages_only_readable_files_under_home(
    state: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    report = tmp_path / "report.md"
    report.write_text("# Report", encoding="utf-8")

    result = share_files([str(report)], caption="Here it is")

    assert result == {
        "status": "shared",
        "files": [{"filename": "report.md", "path": str(report.resolve())}],
    }
    (pending,) = SharedFiles(state).pending()
    assert pending.paths == (report.resolve(),)
    assert pending.caption == "Here it is"
    with pytest.raises(ToolError, match="outside the allowed home"):
        share_files(["/etc/hostname"])
