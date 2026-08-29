import asyncio
import logging
from collections.abc import Callable
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal, cast

import pytest
from imapclient.exceptions import IMAPClientError
from openai_codex.generated.v2_all import ReasoningEffort
from pydantic import SecretStr
from pypdf import PdfWriter

from ariadne.codex import CodexConversation, _mcp_config_overrides
from ariadne.codex.models import CodexTurnSettings
from ariadne.codex.resolver import resolve_profile
from ariadne.mail import (
    FULL_QUERY,
    HEADER_QUERY,
    MailLoop,
    MailProcessor,
    MailRoute,
    MailRoutes,
    MailState,
    SuggestedAction,
    backfill_inbox,
    cheap_triage,
    lint_mail_routes,
    load_routes,
    move_messages,
    parse_metadata,
    render_message,
    restore_folder_to_inbox,
)
from ariadne.mail.runtime import _enter_idle
from ariadne.profile import MAIL_PROFILE, TELEGRAM_PROFILE
from ariadne.scripts.mail_route_lint import render_report
from ariadne.telemetry import Telemetry

TURN_SETTINGS = CodexTurnSettings("gpt-5.6-luna", ReasoningEffort.low, "disabled")


def routes(
    unmatched_action: Literal["inspect", "cheap_triage"] = "inspect",
) -> MailRoutes:
    return MailRoutes.model_validate(
        {
            "version": 1,
            "folders": {
                "newsletters": "Newsletters",
                "promotions": "Promotions",
                "receipts": "Receipts",
                "travel": "Travel",
                "notifications": "Notifications",
            },
            "defaults": {
                "unmatched_action": unmatched_action,
                "unmatched_keep_in_inbox": True,
            },
            "rules": [
                {
                    "id": "important-first",
                    "match": {
                        "from": ["same@example.com"],
                        "subject_contains_any": ["Action needed"],
                    },
                    "classification": "notifications",
                    "action": "iris",
                },
                {
                    "id": "bulk-second",
                    "match": {"from": ["same@example.com"]},
                    "classification": "promotions",
                    "action": "move",
                },
                {
                    "id": "receipts",
                    "match": {"from": ["shop@example.com"]},
                    "classification": "receipts",
                    "action": "move",
                },
                {
                    "id": "review-then-file",
                    "match": {"from": ["review@example.com"]},
                    "classification": "travel",
                    "action": "iris_then_move",
                },
            ],
        }
    )


def test_checked_in_routes_example_matches_the_runtime_schema() -> None:
    example = Path(__file__).parents[1] / "mail-routes.example.yaml"

    configured = load_routes(example)

    assert len(configured.rules) == 2
    assert configured.folders["newsletters"] == "Newsletters"
    assert configured.defaults.unmatched_action == "inspect"
    assert configured.defaults.unmatched_keep_in_inbox is True


def message(
    sender: str,
    subject: str,
    *,
    message_id: str,
    list_unsubscribe: bool = False,
) -> bytes:
    value = EmailMessage()
    value["From"] = sender
    value["To"] = "person@example.com"
    value["Subject"] = subject
    value["Message-ID"] = message_id
    value["Date"] = "Sun, 23 Aug 2026 10:00:00 +0000"
    if list_unsubscribe:
        value["List-Unsubscribe"] = "<https://example.com/unsubscribe>"
    value.set_content("Useful body text.")
    return value.as_bytes()


def test_ordered_routes_use_the_first_complete_match() -> None:
    metadata = parse_metadata(
        message("Same <same@example.com>", "Action Needed today", message_id="<1>")
    )

    matched = routes().match(metadata)

    assert matched is not None
    assert matched.id == "important-first"
    assert matched.action == "iris"


def test_route_ids_must_be_unique() -> None:
    data = routes().model_dump(by_alias=True)
    data["rules"] = [*data["rules"], data["rules"][0]]

    with pytest.raises(ValueError, match="Mail route ids must be unique"):
        MailRoutes.model_validate(data)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            message(
                "alerts@example.com",
                "Security alert",
                message_id="<1>",
                list_unsubscribe=True,
            ),
            "important",
        ),
        (
            message(
                "list@example.com",
                "Weekly digest",
                message_id="<2>",
                list_unsubscribe=True,
            ),
            "routine",
        ),
        (message("person@example.com", "Project update", message_id="<3>"), "inspect"),
    ],
)
def test_cheap_triage_only_short_circuits_clearly_routine_mail(
    raw: bytes, expected: str
) -> None:
    assert cheap_triage(parse_metadata(raw)) == expected


class FakeIMAP:
    def __init__(
        self,
        messages: dict[int, bytes],
        uidvalidity: int = 10,
        capabilities: tuple[str, ...] = ("MOVE",),
    ) -> None:
        self.messages = messages
        self.uidvalidity = uidvalidity
        self.capabilities = capabilities
        self.fetches: list[tuple[int, bytes]] = []
        self.moves: list[tuple[tuple[int, ...], str]] = []
        self.copies: list[tuple[tuple[int, ...], str]] = []
        self.deletions: list[tuple[tuple[int, ...], bool]] = []
        self.uid_expunges: list[tuple[int, ...]] = []
        self.move_actions: list[str] = []
        self.flags: list[tuple[list[int], list[bytes]]] = []
        self.created: list[str] = []
        self.selections: list[tuple[str, bool]] = []

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    def select_folder(self, mailbox: str, readonly: bool = False) -> dict[bytes, int]:
        self.selections.append((mailbox, readonly))
        return {
            b"UIDVALIDITY": self.uidvalidity,
            b"UIDNEXT": max(self.messages, default=0) + 1,
        }

    def search(self, criteria: list[str]) -> list[int]:
        assert criteria == ["ALL"]
        return list(self.messages)

    def fetch(
        self, uids: list[int], queries: list[bytes]
    ) -> dict[int, dict[bytes, bytes]]:
        query = queries[0]
        key = b"BODY[]" if query == FULL_QUERY else b"BODY[HEADER.FIELDS (...)]"
        response = {}
        for uid in uids:
            self.fetches.append((uid, query))
            raw = self.messages.get(uid)
            if raw is not None:
                response[uid] = {key: raw}
        return response

    def move(self, uids: tuple[int, ...], destination: str) -> None:
        self.move_actions.append("move")
        self.moves.append((uids, destination))

    def copy(self, uids: tuple[int, ...], destination: str) -> None:
        self.move_actions.append("copy")
        self.copies.append((uids, destination))

    def delete_messages(self, uids: tuple[int, ...], silent: bool = False) -> None:
        self.move_actions.append("delete")
        self.deletions.append((uids, silent))

    def uid_expunge(self, uids: tuple[int, ...]) -> None:
        self.move_actions.append("uid_expunge")
        self.uid_expunges.append(uids)

    def add_flags(self, uids: list[int], flags: list[bytes]) -> None:
        self.flags.append((uids, flags))

    def list_folders(self) -> list[tuple[tuple[bytes, ...], bytes, str]]:
        return [((b"\\HasNoChildren",), b"/", "Newsletters")]

    def create_folder(self, folder: str) -> None:
        self.created.append(folder)


class IdlingIMAP(FakeIMAP):
    def __init__(self) -> None:
        super().__init__({})
        self.calls: list[str] = []

    def login(self, username: str, password: str) -> None:
        assert (username, password) == ("person@example.com", "password")
        self.calls.append("login")

    def select_folder(self, mailbox: str, readonly: bool = False) -> dict[bytes, int]:
        self.calls.append("select")
        return super().select_folder(mailbox, readonly)

    def search(self, criteria: list[str]) -> list[int]:
        self.calls.append("search")
        return super().search(criteria)

    def idle(self) -> None:
        self.calls.append("idle")

    def idle_check(self, timeout: int) -> None:
        assert timeout == 30
        self.calls.append("idle_check")
        raise RuntimeError("disconnect")

    def idle_done(self) -> None:
        self.calls.append("idle_done")

    def logout(self) -> None:
        self.calls.append("logout")


class _IdleProtocol:
    def __init__(self, responses: list[bytes | None]) -> None:
        self.responses = responses
        self.tagged_commands = {b"IDLE-1": None}

    def _get_response(self) -> bytes | None:
        return self.responses.pop(0)


class UnsolicitedIdleIMAP:
    def __init__(self, responses: list[bytes | None]) -> None:
        self._idle_tag = b"IDLE-1"
        self._imap = _IdleProtocol(responses)

    def idle(self) -> None:
        raise IMAPClientError(
            "Unexpected IDLE response: b'* 1228 FETCH (UID 7003 FLAGS (\\Seen))'"
        )


class FailingMoveIMAP(FakeIMAP):
    def __init__(self, messages: dict[int, bytes], failures: int) -> None:
        super().__init__(messages)
        self.failures = failures
        self.move_attempts: list[tuple[tuple[int, ...], str]] = []

    def move(self, uids: tuple[int, ...], destination: str) -> None:
        self.move_attempts.append((uids, destination))
        if self.failures:
            self.failures -= 1
            raise RuntimeError("move failed")
        super().move(uids, destination)


class DecidingConversation:
    def __init__(
        self,
        state: MailState,
        job_id: str,
        suggested_action: SuggestedAction = "flag",
    ) -> None:
        self.state = state
        self.job_id = job_id
        self.suggested_action = suggested_action
        self.prompts: list[str] = []
        self.closed = False

    async def stream_turn(self, prompt: str):
        self.prompts.append(prompt)
        self.state.record_model_decision(
            self.job_id,
            "notifications",
            "important",
            self.suggested_action,
            "A draft, not sent.",
        )
        yield "done"

    async def close(self) -> None:
        self.closed = True


class FailingConversation:
    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.closed = False

    async def stream_turn(self, prompt: str):
        self.prompts.append(prompt)
        if False:
            yield ""
        raise RuntimeError("Iris failed")

    async def close(self) -> None:
        self.closed = True


def queued_processor(
    tmp_path: Path,
    client: FakeIMAP,
    conversation_factory: Callable[[str], CodexConversation],
) -> tuple[MailProcessor, MailState]:
    state = MailState(tmp_path / "mail.sqlite3")
    state.initialize()
    state.discover("INBOX", 10, client.messages)
    processor = MailProcessor(
        cast(Any, client),
        routes(),
        state,
        conversation_factory,
    )
    processor.uidvalidity = 10
    return processor, state


async def test_first_start_baselines_then_new_mail_is_processed(
    tmp_path: Path, caplog
) -> None:
    caplog.set_level(logging.INFO)
    client = FakeIMAP(
        {
            1: message("shop@example.com", "Receipt", message_id="<receipt>"),
        }
    )
    state = MailState(tmp_path / "mail.sqlite3")
    state.initialize()
    conversations: list[DecidingConversation] = []

    def conversation(job_id: str) -> CodexConversation:
        value = DecidingConversation(state, job_id)
        conversations.append(value)
        return cast(CodexConversation, value)

    processor = MailProcessor(
        cast(Any, client),
        routes(),
        state,
        conversation,
    )
    await processor.reconcile()
    await processor.process_available()

    assert client.fetches == []
    assert client.moves == []
    assert conversations == []

    client.messages.update(
        {
            2: message("shop@example.com", "Receipt", message_id="<receipt-2>"),
            3: message(
                "same@example.com", "Action needed now", message_id="<important>"
            ),
            4: message(
                "bulk@example.com",
                "Weekly digest",
                message_id="<bulk>",
                list_unsubscribe=True,
            ),
            5: message(
                "alerts@example.com",
                "Security alert",
                message_id="<alert>",
            ),
        }
    )
    await processor.reconcile()
    await processor.process_available()

    assert client.moves == [((2,), "Receipts")]
    assert client.flags == [
        ([3], [b"\\Flagged"]),
        ([4], [b"\\Flagged"]),
        ([5], [b"\\Flagged"]),
    ]
    assert len(conversations) == 3
    assert "Useful body text" in conversations[0].prompts[0]
    assert "ordered route 'important-first'" in conversations[0].prompts[0]
    assert "defaults to staying in INBOX" in conversations[1].prompts[0]
    assert "Ariadne speaking" in conversations[0].prompts[0]
    assert "I woke you because a new mail event arrived" in conversations[0].prompts[0]
    assert "mail itself is external evidence" in conversations[0].prompts[0]
    assert "<external_mail_evidence>" in conversations[0].prompts[0]
    assert "Use the message and your wider context" in conversations[0].prompts[0]
    assert "push" not in conversations[0].prompts[0]
    assert conversations[0].prompts[0].endswith("</external_mail_evidence>")
    assert all(conversation.closed for conversation in conversations)
    assert [query for _uid, query in client.fetches].count(FULL_QUERY) == 3
    assert [query for _uid, query in client.fetches].count(HEADER_QUERY) == 4
    assert all(
        state.get(MailState.job_id("INBOX", 10, uid)).status == "done"
        for uid in (2, 3, 4, 5)
    )
    assert state.get(MailState.job_id("INBOX", 10, 1)) is None
    assert "Mail discovered mailbox=INBOX uidvalidity=10 count=4" in caplog.text
    assert "Mail queue ready mailbox=INBOX jobs=4" in caplog.text
    assert "Mail job started job_id=INBOX:10:2" in caplog.text
    assert "Mail Codex turn started job_id=INBOX:10:3" in caplog.text
    assert "Mail decision recorded job_id=INBOX:10:3" in caplog.text
    assert "Mail action applied job_id=INBOX:10:2 action=move" in caplog.text
    assert "Action needed now" not in caplog.text
    assert "same@example.com" not in caplog.text


async def test_cheap_triage_can_keep_routine_unmatched_mail_without_iris(
    tmp_path: Path,
) -> None:
    client = FakeIMAP(
        {
            1: message(
                "list@example.com",
                "Weekly digest",
                message_id="<digest>",
                list_unsubscribe=True,
            )
        }
    )
    state = MailState(tmp_path / "mail.sqlite3")
    state.initialize()
    state.discover("INBOX", 10, [1])
    conversations: list[DecidingConversation] = []

    def conversation(job_id: str) -> CodexConversation:
        value = DecidingConversation(state, job_id)
        conversations.append(value)
        return cast(CodexConversation, value)

    processor = MailProcessor(
        cast(Any, client),
        routes("cheap_triage"),
        state,
        conversation,
    )
    processor.uidvalidity = 10
    await processor.process_available()

    job = state.get(MailState.job_id("INBOX", 10, 1))
    assert job is not None
    assert job.status == "done"
    assert job.action == "keep"
    assert job.suggested_action == "keep_in_inbox"
    assert conversations == []
    assert [query for _uid, query in client.fetches] == [HEADER_QUERY]


async def test_iris_then_move_honors_iris_flag_and_leaves_mail_in_inbox(
    tmp_path: Path,
) -> None:
    client = FakeIMAP(
        {1: message("review@example.com", "Trip review", message_id="<review>")}
    )
    conversations: list[DecidingConversation] = []
    state: MailState

    def conversation(job_id: str) -> CodexConversation:
        value = DecidingConversation(state, job_id)
        conversations.append(value)
        return cast(CodexConversation, value)

    processor, state = queued_processor(tmp_path, client, conversation)

    await processor.process_available()

    job = state.get(MailState.job_id("INBOX", 10, 1))
    assert job is not None
    assert job.status == "done"
    assert job.route_id == "review-then-file"
    assert job.action == "flag"
    assert job.destination is None
    assert client.moves == []
    assert client.flags == [([1], [b"\\Flagged"])]
    assert len(conversations) == 1
    assert (
        "if Iris keeps it in INBOX, move it to 'Travel'" in conversations[0].prompts[0]
    )
    assert "/config/mail-routes.yaml" not in conversations[0].prompts[0]


async def test_iris_then_move_honors_iris_destination_without_duplicate_move(
    tmp_path: Path,
) -> None:
    client = FakeIMAP(
        {1: message("review@example.com", "Trip review", message_id="<review>")}
    )
    conversations: list[DecidingConversation] = []
    state: MailState

    def conversation(job_id: str) -> CodexConversation:
        value = DecidingConversation(state, job_id, "move_to_notifications")
        conversations.append(value)
        return cast(CodexConversation, value)

    processor, state = queued_processor(tmp_path, client, conversation)

    await processor.process_available()

    job = state.get(MailState.job_id("INBOX", 10, 1))
    assert job is not None
    assert job.status == "done"
    assert job.action == "move"
    assert job.destination == "Notifications"
    assert client.moves == [((1,), "Notifications")]
    assert len(conversations) == 1


async def test_iris_then_move_uses_route_destination_when_iris_keeps_inbox(
    tmp_path: Path,
) -> None:
    client = FakeIMAP(
        {1: message("review@example.com", "Trip review", message_id="<review>")}
    )
    conversations: list[DecidingConversation] = []
    state: MailState

    def conversation(job_id: str) -> CodexConversation:
        value = DecidingConversation(state, job_id, "keep_in_inbox")
        conversations.append(value)
        return cast(CodexConversation, value)

    processor, state = queued_processor(tmp_path, client, conversation)

    await processor.process_available()

    job = state.get(MailState.job_id("INBOX", 10, 1))
    assert job is not None
    assert job.status == "done"
    assert job.action == "move"
    assert job.destination == "Travel"
    assert client.moves == [((1,), "Travel")]
    assert len(conversations) == 1


async def test_iris_then_move_does_not_move_when_iris_fails(tmp_path: Path) -> None:
    client = FakeIMAP(
        {1: message("review@example.com", "Trip review", message_id="<review>")}
    )
    conversations: list[FailingConversation] = []

    def conversation(_job_id: str) -> CodexConversation:
        value = FailingConversation()
        conversations.append(value)
        return cast(CodexConversation, value)

    processor, state = queued_processor(tmp_path, client, conversation)

    await processor.process_available()

    job = state.get(MailState.job_id("INBOX", 10, 1))
    assert job is not None
    assert job.status == "failed"
    assert job.action == "iris_then_move"
    assert job.destination == "Travel"
    assert client.moves == []
    assert len(conversations) == 1
    assert conversations[0].closed


async def test_iris_then_move_retries_iris_after_iris_failure(tmp_path: Path) -> None:
    client = FakeIMAP(
        {1: message("review@example.com", "Trip review", message_id="<review>")}
    )
    attempts: list[FailingConversation | DecidingConversation] = []
    state: MailState

    def conversation(job_id: str) -> CodexConversation:
        if not attempts:
            value: FailingConversation | DecidingConversation = FailingConversation()
        else:
            value = DecidingConversation(state, job_id, "keep_in_inbox")
        attempts.append(value)
        return cast(CodexConversation, value)

    processor, state = queued_processor(tmp_path, client, conversation)

    await processor.process_available()
    await processor.process_available()

    job = state.get(MailState.job_id("INBOX", 10, 1))
    assert job is not None
    assert job.status == "done"
    assert client.moves == [((1,), "Travel")]
    assert len(attempts) == 2


async def test_iris_then_move_records_move_failure_for_retry(tmp_path: Path) -> None:
    client = FailingMoveIMAP(
        {1: message("review@example.com", "Trip review", message_id="<review>")},
        failures=2,
    )
    conversations: list[DecidingConversation] = []
    state: MailState

    def conversation(job_id: str) -> CodexConversation:
        value = DecidingConversation(state, job_id, "keep_in_inbox")
        conversations.append(value)
        return cast(CodexConversation, value)

    processor, state = queued_processor(tmp_path, client, conversation)

    await processor.process_available()

    job = state.get(MailState.job_id("INBOX", 10, 1))
    assert job is not None
    assert job.status == "failed"
    assert job.action == "move"
    assert job.destination == "Travel"
    assert client.move_attempts == [((1,), "Travel")]
    assert len(conversations) == 1


async def test_iris_then_move_retries_only_the_failed_move(tmp_path: Path) -> None:
    client = FailingMoveIMAP(
        {1: message("review@example.com", "Trip review", message_id="<review>")},
        failures=1,
    )
    conversations: list[DecidingConversation] = []
    state: MailState

    def conversation(job_id: str) -> CodexConversation:
        value = DecidingConversation(state, job_id, "keep_in_inbox")
        conversations.append(value)
        return cast(CodexConversation, value)

    processor, state = queued_processor(tmp_path, client, conversation)

    await processor.process_available()
    await processor.process_available()

    job = state.get(MailState.job_id("INBOX", 10, 1))
    assert job is not None
    assert job.status == "done"
    assert client.move_attempts == [((1,), "Travel"), ((1,), "Travel")]
    assert client.moves == [((1,), "Travel")]
    assert len(conversations) == 1


async def test_iris_then_move_skips_duplicate_message_id(tmp_path: Path) -> None:
    client = FakeIMAP(
        {2: message("review@example.com", "Trip review", message_id="<duplicate>")}
    )
    conversations: list[DecidingConversation] = []
    state = MailState(tmp_path / "mail.sqlite3")
    state.initialize()
    state.discover("INBOX", 10, [1])
    original = MailState.job_id("INBOX", 10, 1)
    state.start(original)
    state.identify(original, "<duplicate>")
    state.finish(original)
    state.discover("INBOX", 10, [2])

    def conversation(job_id: str) -> CodexConversation:
        value = DecidingConversation(state, job_id)
        conversations.append(value)
        return cast(CodexConversation, value)

    processor = MailProcessor(
        cast(Any, client),
        routes(),
        state,
        conversation,
    )
    processor.uidvalidity = 10

    await processor.process_available()

    duplicate = state.get(MailState.job_id("INBOX", 10, 2))
    assert duplicate is not None
    assert duplicate.status == "done"
    assert duplicate.action is None
    assert client.moves == []
    assert conversations == []


def test_restart_catches_up_mail_received_after_the_saved_watermark(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "mail.sqlite3"
    initial = MailState(state_path)
    initial.initialize()
    assert initial.catch_up("INBOX", 10, [1, 2]) == ()
    assert initial.retryable("INBOX", 10) == ()

    restarted = MailState(state_path)
    restarted.initialize()
    assert restarted.catch_up("INBOX", 10, [1, 2, 3]) == (3,)

    assert [job.uid for job in restarted.retryable("INBOX", 10)] == [3]


async def test_restart_recovers_running_jobs_and_deduplicates_by_message_id(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "mail.sqlite3"
    state = MailState(state_path)
    state.initialize()
    state.discover("INBOX", 10, [1])
    first = MailState.job_id("INBOX", 10, 1)
    state.start(first)
    state.identify(first, "<same-delivery>")
    state.finish(first)
    state.discover("INBOX", 10, [2])
    interrupted = MailState.job_id("INBOX", 10, 2)
    state.start(interrupted)

    restarted = MailState(state_path)
    restarted.initialize()
    assert [job.job_id for job in restarted.retryable("INBOX", 10)] == [interrupted]
    restarted.discover("INBOX", 11, [50])
    duplicate = MailState.job_id("INBOX", 11, 50)
    restarted.start(duplicate)

    assert restarted.identify(duplicate, "<same-delivery>")
    restarted.finish(duplicate)
    assert restarted.get(duplicate).status == "done"
    assert restarted.retryable("INBOX", 11) == ()


async def test_mail_loop_ensures_every_configured_folder() -> None:
    client = FakeIMAP({})
    loop = cast(MailLoop, object.__new__(MailLoop))
    loop.routes = routes()

    await loop._ensure_folders(cast(Any, client))

    assert client.created == [
        "Promotions",
        "Receipts",
        "Travel",
        "Notifications",
    ]


def test_backfill_previews_and_bulk_applies_deterministic_moves() -> None:
    client = FakeIMAP(
        {
            1: message("shop@example.com", "Receipt", message_id="<receipt>"),
            2: message(
                "same@example.com", "Action needed now", message_id="<important>"
            ),
            3: message(
                "other@example.com",
                "Weekly digest",
                message_id="<other>",
                list_unsubscribe=True,
            ),
            4: message("shop@example.com", "Another receipt", message_id="<second>"),
            5: message("same@example.com", "Special offer", message_id="<offer>"),
            6: message("review@example.com", "Trip review", message_id="<review>"),
        },
        capabilities=("UIDPLUS",),
    )

    updates: list[tuple[int, int]] = []
    preview = backfill_inbox(
        cast(Any, client),
        routes(),
        progress=lambda done, total: updates.append((done, total)),
    )

    assert preview.scanned == 6
    assert preview.move_matches == 3
    assert preview.moved == 0
    assert preview.iris_skipped == 2
    assert preview.unmatched == 1
    assert preview.scanned == (
        preview.move_matches + preview.iris_skipped + preview.unmatched
    )
    assert client.moves == []
    assert updates == [(0, 6), (6, 6)]

    applied = backfill_inbox(cast(Any, client), routes(), apply=True)

    assert applied.moved == 3
    assert applied.scanned == preview.scanned
    assert applied.move_matches == preview.move_matches
    assert applied.iris_skipped == preview.iris_skipped
    assert applied.unmatched == preview.unmatched
    assert client.moves == []
    assert client.copies == [((1, 4), "Receipts"), ((5,), "Promotions")]
    assert client.deletions == [((1, 4), True), ((5,), True)]
    assert client.uid_expunges == [(1, 4), (5,)]


def test_route_lint_reports_counts_overlaps_shadowing_and_five_samples() -> None:
    configured = routes()
    shadowed = MailRoute.model_validate(
        {
            "id": "shadowed-same-sender",
            "match": {"from": ["same@example.com"]},
            "classification": "notifications",
            "action": "iris",
        }
    )
    configured = configured.model_copy(update={"rules": (*configured.rules, shadowed)})
    messages = {
        uid: message("same@example.com", f"Action needed {uid}", message_id=f"<{uid}>")
        for uid in range(1, 7)
    }
    messages[7] = message("same@example.com", "Special offer", message_id="<7>")
    messages[8] = message("other@example.com", "Hello", message_id="<8>")
    client = FakeIMAP(messages)
    updates: list[tuple[int, int]] = []

    report = lint_mail_routes(
        cast(Any, client),
        configured,
        batch_size=3,
        progress=lambda done, total: updates.append((done, total)),
    )

    assert report.scanned == 8
    assert report.unmatched == 1
    by_id = {rule.route_id: rule for rule in report.rules}
    assert (by_id["important-first"].matches, by_id["important-first"].selected) == (
        6,
        6,
    )
    assert (by_id["bulk-second"].matches, by_id["bulk-second"].shadowed) == (
        7,
        6,
    )
    assert by_id["shadowed-same-sender"].selected == 0
    assert by_id["shadowed-same-sender"].shadowed == 7
    assert len(by_id["important-first"].sample_subjects) == 5
    assert [
        (item.earlier_route_id, item.later_route_id, item.matches)
        for item in report.overlaps
    ] == [
        ("important-first", "bulk-second", 6),
        ("important-first", "shadowed-same-sender", 6),
        ("bulk-second", "shadowed-same-sender", 7),
    ]
    assert client.selections == [("INBOX", True)]
    assert updates == [(0, 8), (3, 8), (6, 8), (8, 8)]
    rendered = render_report(report)
    assert "shadowed-same-sender [iris]: matches=7, selected=0, shadowed=7" in rendered
    assert "fully_shadowed=yes" in rendered
    assert "important-first + bulk-second: 6" in rendered


def test_move_messages_uses_iclouds_uidplus_fallback_without_global_expunge() -> None:
    client = FakeIMAP({}, capabilities=("UIDPLUS",))

    move_messages(cast(Any, client), [4, 7], "Receipts")

    assert client.moves == []
    assert client.copies == [((4, 7), "Receipts")]
    assert client.deletions == [((4, 7), True)]
    assert client.uid_expunges == [(4, 7)]
    assert client.move_actions == ["copy", "delete", "uid_expunge"]


def test_move_messages_refuses_an_unsafe_mailbox_wide_expunge() -> None:
    client = FakeIMAP({}, capabilities=())

    with pytest.raises(RuntimeError, match="neither MOVE nor safe UIDPLUS"):
        move_messages(cast(Any, client), [4], "Receipts")

    assert client.moves == []
    assert client.copies == []
    assert client.deletions == []
    assert client.uid_expunges == []


def test_restore_folder_previews_then_moves_every_message_to_inbox() -> None:
    client = FakeIMAP(
        {4: b"first", 7: b"second", 9: b"third"}, capabilities=("UIDPLUS",)
    )
    updates: list[tuple[int, int]] = []

    preview = restore_folder_to_inbox(
        cast(Any, client),
        "Receipts",
        progress=lambda done, total: updates.append((done, total)),
    )

    assert preview.found == 3
    assert preview.moved == 0
    assert client.selections == [("Receipts", True)]
    assert client.copies == []
    assert updates == [(0, 3), (3, 3)]

    applied = restore_folder_to_inbox(
        cast(Any, client), "Receipts", apply=True, batch_size=2
    )

    assert applied.found == 3
    assert applied.moved == 3
    assert client.selections[-1] == ("Receipts", False)
    assert client.copies == [((4, 7), "INBOX"), ((9,), "INBOX")]
    assert client.deletions == [((4, 7), True), ((9,), True)]
    assert client.uid_expunges == [(4, 7), (9,)]


def test_restore_folder_refuses_inbox_as_its_source() -> None:
    client = FakeIMAP({})

    with pytest.raises(ValueError, match="cannot be INBOX"):
        restore_folder_to_inbox(cast(Any, client), "inbox", apply=True)

    assert client.selections == []


async def test_each_connection_catches_up_before_entering_idle(tmp_path: Path) -> None:
    client = IdlingIMAP()
    loop = cast(MailLoop, object.__new__(MailLoop))
    loop.settings = cast(
        Any,
        SimpleNamespace(
            username="person@example.com",
            app_password=SecretStr("password"),
            routes=Path("/config/mail-routes.yaml"),
        ),
    )
    loop.routes = routes()
    loop.state = MailState(tmp_path / "mail.sqlite3")
    loop.state.initialize()
    loop.telemetry = Telemetry()
    loop._stop = asyncio.Event()
    loop._client_factory = cast(Any, lambda: client)

    with pytest.raises(RuntimeError, match="disconnect"):
        await loop._session()

    assert client.calls == [
        "login",
        "select",
        "search",
        "idle",
        "idle_check",
        "idle_done",
        "logout",
    ]


def test_enter_idle_accepts_unsolicited_updates_before_continuation() -> None:
    client = UnsolicitedIdleIMAP([b"* 1229 EXISTS", None])

    _enter_idle(cast(Any, client))

    assert client._imap.responses == []


def test_enter_idle_preserves_genuine_failures() -> None:
    client = UnsolicitedIdleIMAP([])
    client._imap.tagged_commands[client._idle_tag] = ("BAD", [b"rejected"])

    with pytest.raises(IMAPClientError, match="Unexpected IDLE response"):
        _enter_idle(cast(Any, client))


def test_mail_tool_is_enabled_only_for_job_scoped_conversations(tmp_path: Path) -> None:
    normal = _mcp_config_overrides(
        resolve_profile(
            TELEGRAM_PROFILE,
            vault=tmp_path,
            settings=TURN_SETTINGS,
            human="Example User",
        )
    )
    mail = _mcp_config_overrides(
        resolve_profile(
            MAIL_PROFILE,
            vault=tmp_path,
            settings=TURN_SETTINGS,
            human="Example User",
            mcp_environment={
                "ARIADNE_MAIL_JOB_ID": "INBOX:1:2",
                "ARIADNE_MAIL_STATE": str(tmp_path / "mail.sqlite3"),
            },
        )
    )

    assert not any("record_current_mail_decision" in value for value in normal)
    assert any("record_current_mail_decision" in value for value in mail)
    assert any("ARIADNE_MAIL_JOB_ID" in value for value in mail)


def test_calendar_attachments_are_interpreted_for_the_mail_turn() -> None:
    value = EmailMessage()
    value["From"] = "host@example.com"
    value["To"] = "person@example.com"
    value["Subject"] = "Invitation"
    value["Message-ID"] = "<calendar>"
    value.set_content("Please join.")
    value.add_attachment(
        b"BEGIN:VCALENDAR\nSUMMARY:Interview\nEND:VCALENDAR",
        maintype="text",
        subtype="calendar",
        filename="invite.ics",
    )
    raw = value.as_bytes()

    rendered = render_message(raw, parse_metadata(raw))

    assert "invite.ics (text/calendar)" in rendered
    assert "SUMMARY:Interview" in rendered


def test_pdf_attachments_are_opened_for_the_mail_turn() -> None:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(output)
    value = EmailMessage()
    value["From"] = "recruiter@example.com"
    value["To"] = "person@example.com"
    value["Subject"] = "Role"
    value["Message-ID"] = "<pdf>"
    value.set_content("See the role profile.")
    value.add_attachment(
        output.getvalue(),
        maintype="application",
        subtype="pdf",
        filename="role.pdf",
    )
    raw = value.as_bytes()

    rendered = render_message(raw, parse_metadata(raw))

    assert "role.pdf (application/pdf)" in rendered
    assert "PDF role.pdf:" in rendered
