import asyncio
from email.message import EmailMessage
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
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
    MailRoutes,
    MailState,
    backfill_inbox,
    cheap_triage,
    load_routes,
    move_messages,
    parse_metadata,
    render_message,
    restore_folder_to_inbox,
)
from ariadne.profile import MAIL_PROFILE, TELEGRAM_PROFILE

TURN_SETTINGS = CodexTurnSettings("gpt-5.6-luna", ReasoningEffort.low, "disabled")


def routes() -> MailRoutes:
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
                "unmatched_action": "inspect",
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


class DecidingConversation:
    def __init__(self, state: MailState, job_id: str) -> None:
        self.state = state
        self.job_id = job_id
        self.prompts: list[str] = []
        self.closed = False

    async def stream_reply(self, prompt: str):
        self.prompts.append(prompt)
        self.state.record_model_decision(
            self.job_id,
            "notifications",
            "important",
            "flag",
            "A draft, not sent.",
        )
        yield "done"

    async def close(self) -> None:
        self.closed = True


async def test_first_start_baselines_then_new_mail_is_processed(
    tmp_path: Path,
) -> None:
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

    processor = MailProcessor(cast(Any, client), routes(), state, conversation)
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
    assert client.flags == [([3], [b"\\Flagged"]), ([5], [b"\\Flagged"])]
    assert len(conversations) == 2
    assert "Useful body text" in conversations[0].prompts[0]
    assert "ordered route 'important-first'" in conversations[0].prompts[0]
    assert "defaults to staying in INBOX" in conversations[1].prompts[0]
    assert all(conversation.closed for conversation in conversations)
    assert [query for _uid, query in client.fetches].count(FULL_QUERY) == 2
    assert [query for _uid, query in client.fetches].count(HEADER_QUERY) == 4
    assert all(
        state.get(MailState.job_id("INBOX", 10, uid)).status == "done"
        for uid in (2, 3, 4, 5)
    )
    routine = state.get(MailState.job_id("INBOX", 10, 4))
    assert routine is not None
    assert routine.action == "keep"
    assert routine.suggested_action == "keep_in_inbox"
    assert state.get(MailState.job_id("INBOX", 10, 1)) is None


def test_restart_catches_up_mail_received_after_the_saved_watermark(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "mail.sqlite3"
    initial = MailState(state_path)
    initial.initialize()
    initial.catch_up("INBOX", 10, [1, 2])
    assert initial.retryable("INBOX", 10) == ()

    restarted = MailState(state_path)
    restarted.initialize()
    restarted.catch_up("INBOX", 10, [1, 2, 3])

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
        },
        capabilities=("UIDPLUS",),
    )

    updates: list[tuple[int, int]] = []
    preview = backfill_inbox(
        cast(Any, client),
        routes(),
        progress=lambda done, total: updates.append((done, total)),
    )

    assert preview.scanned == 5
    assert preview.move_matches == 3
    assert preview.moved == 0
    assert preview.iris_skipped == 1
    assert preview.unmatched == 1
    assert preview.scanned == (
        preview.move_matches + preview.iris_skipped + preview.unmatched
    )
    assert client.moves == []
    assert updates == [(0, 5), (5, 5)]

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
            username="person@example.com", app_password=SecretStr("password")
        ),
    )
    loop.routes = routes()
    loop.state = MailState(tmp_path / "mail.sqlite3")
    loop.state.initialize()
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

    assert not any("triage_current_mail" in value for value in normal)
    assert any("triage_current_mail" in value for value in mail)
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
