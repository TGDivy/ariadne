from __future__ import annotations

import email
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from email import policy
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from ariadne.config import MailAccountSettings
from ariadne.mail import MailAccountRegistry, MailDrafts, build_message, build_reply
from ariadne.mail.drafts import QUOTE_LIMIT, drafts_folder
from ariadne.mail.reader import MailReference, encode_mail_id

ICLOUD = MailAccountSettings(
    key="icloud",
    label="iCloud",
    provider="icloud",
    address="divy@icloud.example",
    host="imap.mail.me.com",
    password=SecretStr("app-password"),
)
OUTLOOK = MailAccountSettings(
    key="outlook",
    label="Outlook",
    provider="outlook",
    address="divy@outlook.example",
    host="outlook.office365.com",
    client_id="client",
    token_cache=Path("/private/outlook.json"),
)


def original_message(
    *,
    sender: str = "Alex Fenn <alex@example.com>",
    to: str = "Divy <divy@icloud.example>, Team <team@example.com>",
    cc: str = "Sam <sam@example.com>",
    subject: str = "Contract review",
    body: str = "Are you free Tuesday afternoon?\n\nIt should take an hour.",
    message_id: str = "<original@example.com>",
    references: str | None = None,
    reply_to: str | None = None,
    date: str = "Wed, 09 Sep 2026 10:12:00 +0000",
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    if cc:
        message["Cc"] = cc
    message["Subject"] = subject
    message["Date"] = date
    message["Message-ID"] = message_id
    if references is not None:
        message["References"] = references
    if reply_to is not None:
        message["Reply-To"] = reply_to
    message.set_content(body)
    return message


class FakeIMAP:
    """A mailbox that records every command so writes can be asserted exactly."""

    def __init__(
        self,
        *,
        folders: list[tuple[tuple[bytes, ...], bytes, str]] | None = None,
        stored: dict[int, bytes] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.appended: list[tuple[str, bytes, tuple[bytes, ...]]] = []
        self.current = ""
        self.folders = folders or [
            ((), b"/", "INBOX"),
            ((b"\\Drafts",), b"/", "Drafts"),
            ((b"\\Sent",), b"/", "Sent Messages"),
        ]
        self.stored = stored or {7: original_message().as_bytes()}

    def list_folders(self) -> list[tuple[tuple[bytes, ...], bytes, str]]:
        self.calls.append("list_folders")
        return self.folders

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, int]:
        self.calls.append(f"select:{folder}:{readonly}")
        self.current = folder
        return {b"UIDVALIDITY": 42}

    def search(self, criteria: list[object]) -> list[int]:
        self.calls.append("search")
        return sorted(self.stored)

    def fetch(self, uids: list[int], query: list[bytes]) -> dict[int, dict[bytes, Any]]:
        self.calls.append(f"fetch:{tuple(query)!r}")
        return {
            uid: {b"BODY[]": self.stored[uid]} for uid in uids if uid in self.stored
        }

    def append(
        self,
        folder: str,
        message: bytes,
        flags: tuple[bytes, ...] = (),
        msg_time: datetime | None = None,
    ) -> bytes:
        self.calls.append(f"append:{folder}")
        self.appended.append((folder, message, tuple(flags)))
        return b"OK"


def registry_for(account: MailAccountSettings, client: FakeIMAP) -> MailAccountRegistry:
    @contextmanager
    def connector(_account: MailAccountSettings) -> Iterator[FakeIMAP]:
        yield client

    return MailAccountRegistry([account], connector=connector)


def parsed(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=policy.default)  # type: ignore[return-value]


def test_reply_threads_under_the_original_and_keeps_one_re_prefix() -> None:
    content = build_reply(
        original_message(references="<root@example.com> <second@example.com>"),
        body="Tuesday at 14:00 works.",
        from_address="divy@icloud.example",
    )

    assert content.subject == "Re: Contract review"
    assert content.in_reply_to == "<original@example.com>"
    assert content.message["References"] == (
        "<root@example.com> <second@example.com> <original@example.com>"
    )
    assert content.message["Message-ID"] != "<original@example.com>"


def test_reply_to_a_reply_does_not_accumulate_re_prefixes() -> None:
    content = build_reply(
        original_message(subject="Re: Contract review"),
        body="Still fine.",
        from_address="divy@icloud.example",
    )

    assert content.subject == "Re: Contract review"


def test_reply_is_addressed_to_everyone_except_the_account_itself() -> None:
    content = build_reply(
        original_message(),
        body="Confirmed.",
        from_address="divy@icloud.example",
    )

    assert content.to == ("Alex Fenn <alex@example.com>",)
    assert content.cc == ("Team <team@example.com>", "Sam <sam@example.com>")


def test_reply_prefers_reply_to_and_never_copies_the_sender_twice() -> None:
    content = build_reply(
        original_message(reply_to="Alex Fenn <alex@example.com>"),
        body="Confirmed.",
        from_address="divy@icloud.example",
    )

    assert content.to == ("Alex Fenn <alex@example.com>",)
    assert "alex@example.com" not in " ".join(content.cc)


def test_reply_falls_back_to_the_recipients_when_the_sender_is_the_account() -> None:
    content = build_reply(
        original_message(sender="Divy <divy@icloud.example>"),
        body="Following up on my own note.",
        from_address="divy@icloud.example",
    )

    assert content.to == ("Team <team@example.com>",)


def test_a_message_with_no_reachable_reply_address_is_refused() -> None:
    with pytest.raises(ValueError, match="no address to reply to"):
        build_reply(
            original_message(sender="Divy <divy@icloud.example>", to="", cc=""),
            body="Nowhere to send this.",
            from_address="divy@icloud.example",
        )


def test_reply_quotes_the_original_under_an_attribution_line() -> None:
    content = build_reply(
        original_message(),
        body="Tuesday at 14:00 works.",
        from_address="divy@icloud.example",
    )

    text = content.message.get_content()
    assert text.startswith("Tuesday at 14:00 works.\n\n")
    assert "On Wed, 09 Sep 2026 at 10:12, Alex Fenn wrote:" in text
    assert "> Are you free Tuesday afternoon?" in text
    assert content.quoted is True
    assert content.quote_truncated is False


def test_a_long_original_is_quoted_up_to_a_reported_bound() -> None:
    content = build_reply(
        original_message(body="word " * 5_000),
        body="Noted.",
        from_address="divy@icloud.example",
    )

    text = content.message.get_content()
    assert content.quote_truncated is True
    assert "> [truncated]" in text
    assert len(text) < QUOTE_LIMIT + 2_000


def test_html_only_mail_is_quoted_as_readable_text() -> None:
    original = EmailMessage()
    original["From"] = "Alex <alex@example.com>"
    original["To"] = "divy@icloud.example"
    original["Subject"] = "Update"
    original["Date"] = "Wed, 09 Sep 2026 10:12:00 +0000"
    original["Message-ID"] = "<html@example.com>"
    original.set_content("<p>First line</p><p>Second line</p>", subtype="html")

    content = build_reply(original, body="Thanks.", from_address="divy@icloud.example")

    text = content.message.get_content()
    assert "> First line" in text
    assert "> Second line" in text
    assert "<p>" not in text


def test_non_ascii_subjects_and_bodies_survive_the_round_trip() -> None:
    content = build_reply(
        original_message(subject="Réunion café"),
        body="Parfait — à mardi.",
        from_address="divy@icloud.example",
    )

    restored = parsed(content.message.as_bytes())
    assert str(restored["Subject"]) == "Re: Réunion café"
    assert "Parfait — à mardi." in restored.get_content()


def test_a_composed_draft_needs_recipients_and_a_subject() -> None:
    with pytest.raises(ValueError, match="at least one recipient"):
        build_message(
            body="Hello",
            from_address="divy@icloud.example",
            to=[],
            subject="Hello",
        )
    with pytest.raises(ValueError, match="one line of text"):
        build_message(
            body="Hello",
            from_address="divy@icloud.example",
            to=["alex@example.com"],
            subject="   ",
        )


def test_a_composed_draft_carries_no_threading_headers_or_quote() -> None:
    content = build_message(
        body="Are you free on Thursday?",
        from_address="divy@icloud.example",
        to=["Alex <alex@example.com>"],
        cc=["sam@example.com"],
        subject="Thursday",
    )

    assert content.message["In-Reply-To"] is None
    assert content.message["References"] is None
    assert content.in_reply_to is None
    assert content.quoted is False
    assert content.to == ("Alex <alex@example.com>",)
    assert content.cc == ("sam@example.com",)
    assert content.message.get_content().strip() == "Are you free on Thursday?"


@pytest.mark.parametrize(
    "value",
    [
        "alex@example.com\r\nBcc: victim@example.com",
        "not-an-address",
        "alex@example.com, sam@example.com",
        "",
    ],
)
def test_recipient_values_that_could_forge_headers_are_refused(value: str) -> None:
    with pytest.raises(ValueError):
        build_message(
            body="Hello",
            from_address="divy@icloud.example",
            to=[value],
            subject="Hello",
        )


def test_an_empty_body_is_refused() -> None:
    with pytest.raises(ValueError, match="needs a body"):
        build_reply(
            original_message(), body="   \n ", from_address="divy@icloud.example"
        )


def test_the_drafts_folder_is_found_by_special_use_flag() -> None:
    client = FakeIMAP(
        folders=[
            ((), b"/", "INBOX"),
            ((b"\\Drafts",), b"/", "Brouillons"),
        ]
    )

    assert drafts_folder(client) == "Brouillons"


def test_the_drafts_folder_falls_back_to_an_ordinary_name() -> None:
    client = FakeIMAP(
        folders=[
            ((), b"/", "INBOX"),
            ((), b".", "INBOX.Drafts"),
        ]
    )

    assert drafts_folder(client) == "INBOX.Drafts"


def test_an_account_without_a_drafts_folder_is_a_clear_failure() -> None:
    client = FakeIMAP(folders=[((), b"/", "INBOX")])

    with pytest.raises(RuntimeError, match="no Drafts folder"):
        drafts_folder(client)
    assert not client.appended


@pytest.mark.parametrize("account", [ICLOUD, OUTLOOK])
def test_a_reply_draft_is_appended_once_and_nothing_is_sent(
    account: MailAccountSettings,
) -> None:
    client = FakeIMAP()
    reference = MailReference("INBOX", 42, 7, account.key)

    result = MailDrafts(registry_for(account, client)).create_draft(
        body="Tuesday at 14:00 works.",
        reply_to=encode_mail_id(reference),
    )

    assert result["status"] == "drafted"
    assert result["sent"] is False
    assert result["account_key"] == account.key
    assert result["folder"] == "Drafts"
    assert result["in_reply_to"] == "<original@example.com>"
    assert len(client.appended) == 1
    folder, raw, flags = client.appended[0]
    assert folder == "Drafts"
    assert flags == (b"\\Draft", b"\\Seen")
    assert str(parsed(raw)["From"]) == account.address
    assert not any(
        call.startswith(("move", "copy", "delete", "expunge", "add_flags"))
        for call in client.calls
    )
    assert all("readonly" not in call or "True" in call for call in client.calls)


def test_a_composed_draft_reaches_the_selected_account() -> None:
    client = FakeIMAP()

    result = MailDrafts(registry_for(OUTLOOK, client)).create_draft(
        body="Are you free on Thursday?",
        account="outlook",
        to=["alex@example.com"],
        subject="Thursday",
    )

    assert result["account_key"] == "outlook"
    assert result["to"] == ["alex@example.com"]
    assert result["quoted_original"] is False
    assert len(client.appended) == 1


def test_a_reply_cannot_be_redirected_to_another_account() -> None:
    client = FakeIMAP()
    reference = MailReference("INBOX", 42, 7, "icloud")

    with pytest.raises(ValueError, match="different account"):
        MailDrafts(registry_for(ICLOUD, client)).create_draft(
            body="Hello",
            reply_to=encode_mail_id(reference),
            account="outlook",
        )
    assert not client.appended


def test_a_stale_reply_id_does_not_write_anything() -> None:
    client = FakeIMAP()
    reference = MailReference("INBOX", 41, 7, "icloud")

    with pytest.raises(ValueError, match="stale"):
        MailDrafts(registry_for(ICLOUD, client)).create_draft(
            body="Hello", reply_to=encode_mail_id(reference)
        )
    assert not client.appended


def test_a_new_draft_requires_an_explicit_account_when_several_are_enabled() -> None:
    client = FakeIMAP()

    @contextmanager
    def connector(_account: MailAccountSettings) -> Iterator[FakeIMAP]:
        yield client

    registry = MailAccountRegistry([ICLOUD, OUTLOOK], connector=connector)

    with pytest.raises(ValueError, match="pass --account"):
        MailDrafts(registry).create_draft(
            body="Hello", to=["alex@example.com"], subject="Hello"
        )
    assert not client.appended
