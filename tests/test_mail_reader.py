from __future__ import annotations

import email
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

import pytest

from ariadne.mail.reader import MailReader, decode_mail_id


def make_message(
    *,
    sender: str,
    subject: str,
    date: str,
    message_id: str,
    body: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "Divy <divy@icloud.com>"
    message["Subject"] = subject
    message["Date"] = date
    message["Message-ID"] = message_id
    if in_reply_to is not None:
        message["In-Reply-To"] = in_reply_to
    if references is not None:
        message["References"] = references
    message.set_content(body)
    return message.as_bytes()


class FakeIMAP:
    def __init__(self) -> None:
        self.current = ""
        self.calls: list[tuple[object, ...]] = []
        self.messages = {
            "INBOX": {
                1: make_message(
                    sender="Ali Wilson <ali@example.com>",
                    subject="Oxford Knight introductions",
                    date="Thu, 20 Aug 2026 09:00:00 +0000",
                    message_id="<root@example.com>",
                    body="I mentioned Alpha Capital and Beacon Partners.",
                ),
                2: make_message(
                    sender="Airline <travel@example.com>",
                    subject="Flight confirmation",
                    date="Fri, 21 Aug 2026 09:00:00 +0000",
                    message_id="<flight@example.com>",
                    body="Your booking is confirmed.",
                ),
            },
            "Sent Messages": {
                8: make_message(
                    sender="Divy <divy@icloud.com>",
                    subject="Re: Oxford Knight introductions",
                    date="Fri, 21 Aug 2026 10:00:00 +0000",
                    message_id="<reply@example.com>",
                    in_reply_to="<root@example.com>",
                    references="<root@example.com>",
                    body="Thanks Ali, I will research those firms.",
                )
            },
            "Trash": {
                9: make_message(
                    sender="Ali Wilson <ali@example.com>",
                    subject="Deleted",
                    date="Sat, 22 Aug 2026 10:00:00 +0000",
                    message_id="<trash@example.com>",
                    body="This should not appear.",
                )
            },
        }

    def list_folders(self) -> list[tuple[tuple[bytes, ...], bytes, str]]:
        return [
            ((), b"/", "INBOX"),
            ((b"\\Sent",), b"/", "Sent Messages"),
            ((b"\\Trash",), b"/", "Trash"),
        ]

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, int]:
        assert readonly is True
        self.current = folder
        self.calls.append(("select", folder, readonly))
        return {b"UIDVALIDITY": 42}

    def search(self, criteria: list[object]) -> list[int]:
        self.calls.append(("search", self.current, criteria))
        messages = self.messages[self.current]
        if "TEXT" in criteria:
            term = str(criteria[criteria.index("TEXT") + 1]).casefold().encode()
            return [uid for uid, raw in messages.items() if term in raw.lower()]
        if "HEADER" in criteria:
            field = str(criteria[criteria.index("HEADER") + 1])
            term = str(criteria[criteria.index("HEADER") + 2]).casefold()
            found = []
            for uid, raw in messages.items():
                message = email.message_from_bytes(raw)
                if term in str(message.get(field, "")).casefold():
                    found.append(uid)
            return found
        if "SUBJECT" in criteria:
            term = str(criteria[criteria.index("SUBJECT") + 1]).casefold().encode()
            return [uid for uid, raw in messages.items() if term in raw.lower()]
        return sorted(messages)

    def fetch(self, uids: list[int], query: list[bytes]) -> dict[int, dict[bytes, Any]]:
        self.calls.append(("fetch", self.current, tuple(uids), tuple(query)))
        return {
            uid: {
                b"BODY[]": self.messages[self.current][uid],
                b"INTERNALDATE": datetime(2026, 8, 20, tzinfo=UTC),
            }
            for uid in uids
            if uid in self.messages[self.current]
        }


def test_human_mail_search_ranks_locally_and_returns_refetchable_ids() -> None:
    client = FakeIMAP()

    result = MailReader(client).search("Ali Wilson", since="2026-08-01")

    assert result["results"][0]["subject"] == "Oxford Knight introductions"
    assert all(item["subject"] != "Deleted" for item in result["results"])
    reference = decode_mail_id(result["results"][0]["id"])
    assert (reference.folder, reference.uidvalidity, reference.uid) == (
        "INBOX",
        42,
        1,
    )
    assert result["searched_folders"] == 2
    assert not any(call[1] == "Trash" for call in client.calls if call[0] == "select")
    assert all(
        b"BODY.PEEK[]" in call[3] or b"BODY.PEEK[]<0.16384>" in call[3]
        for call in client.calls
        if call[0] == "fetch"
    )


def test_read_mail_refetches_full_message_without_marking_it_read() -> None:
    client = FakeIMAP()
    result = MailReader(client).search("flight confirmation")

    message = MailReader(client).read(result["results"][0]["id"])

    assert message["subject"] == "Flight confirmation"
    assert "booking is confirmed" in message["body"]
    assert client.calls[-1][3] == (b"BODY.PEEK[]",)


def test_read_mail_thread_follows_references_across_folders() -> None:
    client = FakeIMAP()
    result = MailReader(client).search("Oxford Knight")

    thread = MailReader(client).read_thread(result["results"][0]["id"])

    assert [message["subject"] for message in thread["messages"]] == [
        "Oxford Knight introductions",
        "Re: Oxford Knight introductions",
    ]
    assert all("body" in message for message in thread["messages"])


@pytest.mark.parametrize("value", ["yesterday", "2026-08-23T10:00:00"])
def test_search_dates_are_unambiguous_iso_dates(value: str) -> None:
    with pytest.raises(ValueError, match="ISO date"):
        MailReader(FakeIMAP()).search("Ali", since=value)
