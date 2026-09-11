from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Literal

import pytest
from pydantic import SecretStr

from ariadne.config import MailAccountSettings
from ariadne.mail import (
    MailAccountRegistry,
    MailClassifier,
    MailRoutes,
    classify_message,
    parse_metadata,
)
from ariadne.mail.reader import MailReference, encode_mail_id

ACCOUNT = MailAccountSettings(
    key="icloud",
    label="iCloud",
    provider="icloud",
    address="divy@icloud.example",
    host="imap.mail.me.com",
    password=SecretStr("app-password"),
)


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
                    "id": "review-then-file",
                    "match": {"from": ["review@example.com"]},
                    "classification": "travel",
                    "action": "iris_then_move",
                },
            ],
        }
    )


def message(
    sender: str = "Stranger <stranger@example.com>",
    subject: str = "A question for you",
    *,
    message_id: str = "<one@example.com>",
    list_unsubscribe: bool = False,
) -> bytes:
    value = EmailMessage()
    value["From"] = sender
    value["To"] = "divy@icloud.example"
    value["Subject"] = subject
    value["Message-ID"] = message_id
    value["Date"] = "Sun, 23 Aug 2026 10:00:00 +0000"
    if list_unsubscribe:
        value["List-Unsubscribe"] = "<https://example.com/unsubscribe>"
    value.set_content("Useful body text.")
    return value.as_bytes()


class FakeIMAP:
    def __init__(self, raw: bytes | None = None) -> None:
        self.calls: list[tuple[str, object]] = []
        self.stored = {7: raw if raw is not None else message()}

    def select_folder(self, folder: str, readonly: bool = False) -> dict[bytes, int]:
        self.calls.append(("select", (folder, readonly)))
        return {b"UIDVALIDITY": 42}

    def fetch(self, uids: list[int], query: list[bytes]) -> dict[int, dict[bytes, Any]]:
        self.calls.append(("fetch", tuple(query)))
        return {
            uid: {b"BODY[HEADER.FIELDS]": self.stored[uid]}
            for uid in uids
            if uid in self.stored
        }


def classifier(
    client: FakeIMAP, configured: MailRoutes | None = None
) -> MailClassifier:
    @contextmanager
    def connector(_account: MailAccountSettings) -> Iterator[FakeIMAP]:
        yield client

    registry = MailAccountRegistry([ACCOUNT], connector=connector)
    return MailClassifier(configured or routes(), registry)


def test_a_move_route_files_the_message_without_waking_iris() -> None:
    decision = classify_message(
        routes(), parse_metadata(message("Same <same@example.com>", "Weekly deals"))
    )

    assert decision.route_id == "bulk-second"
    assert decision.action == "move"
    assert decision.destination == "Promotions"
    assert decision.wakes_iris is False
    assert decision.classification == "promotions"


def test_an_iris_route_wakes_iris_with_no_destination() -> None:
    decision = classify_message(
        routes(),
        parse_metadata(message("Same <same@example.com>", "Action Needed today")),
    )

    assert decision.route_id == "important-first"
    assert decision.action == "iris"
    assert decision.destination is None
    assert decision.wakes_iris is True


def test_an_iris_then_move_route_reports_the_folder_used_when_kept() -> None:
    decision = classify_message(
        routes(), parse_metadata(message("Review <review@example.com>", "Trip"))
    )

    assert decision.action == "iris_then_move"
    assert decision.destination == "Travel"
    assert decision.wakes_iris is True


def test_every_matching_rule_is_reported_in_configured_order() -> None:
    decision = classify_message(
        routes(),
        parse_metadata(message("Same <same@example.com>", "Action Needed today")),
    )

    assert decision.matched_route_ids == ("important-first", "bulk-second")
    assert decision.route_id == "important-first"


def test_unmatched_mail_defaults_to_inspection_by_iris() -> None:
    decision = classify_message(routes(), parse_metadata(message()))

    assert decision.route_id is None
    assert decision.action == "iris"
    assert decision.wakes_iris is True
    assert decision.classification is None
    assert decision.triage == "inspect"


def test_cheap_triage_keeps_clearly_routine_unmatched_mail_out_of_a_turn() -> None:
    decision = classify_message(
        routes("cheap_triage"),
        parse_metadata(message(subject="Weekly digest", list_unsubscribe=True)),
    )

    assert decision.action == "keep"
    assert decision.wakes_iris is False
    assert decision.classification == "routine"
    assert decision.triage == "routine"


def test_cheap_triage_still_wakes_iris_for_unmatched_important_mail() -> None:
    decision = classify_message(
        routes("cheap_triage"),
        parse_metadata(message(subject="Action required on your account")),
    )

    assert decision.action == "iris"
    assert decision.wakes_iris is True
    assert decision.triage == "important"


def test_classifying_a_stored_message_only_peeks_at_headers() -> None:
    client = FakeIMAP(message("Same <same@example.com>", "Weekly deals"))
    mail_id = encode_mail_id(MailReference("INBOX", 42, 7, "icloud"))

    result = classifier(client).classify(mail_id=mail_id)

    assert result["source"] == "mail_id"
    assert result["account_key"] == "icloud"
    assert result["folder"] == "INBOX"
    assert result["route_id"] == "bulk-second"
    assert result["destination"] == "Promotions"
    assert result["wakes_iris"] is False
    assert ("select", ("INBOX", True)) in client.calls
    assert all(
        query[0].startswith(b"BODY.PEEK[HEADER.FIELDS")
        for name, query in client.calls
        if name == "fetch"
    )
    assert not any(
        name in {"move", "copy", "add_flags", "delete_messages", "append"}
        for name, _ in client.calls
    )


def test_classifying_a_raw_file_needs_no_mailbox_at_all(tmp_path: Path) -> None:
    path = tmp_path / "message.eml"
    path.write_bytes(message("Review <review@example.com>", "Trip"))

    @contextmanager
    def refuse(_account: MailAccountSettings) -> Iterator[FakeIMAP]:
        raise AssertionError("classifying a file must not open a mailbox")
        yield FakeIMAP()

    registry = MailAccountRegistry([ACCOUNT], connector=refuse)
    result = MailClassifier(routes(), registry).classify(path=path)

    assert result["source"] == "file"
    assert result["path"] == str(path)
    assert result["route_id"] == "review-then-file"
    assert result["action"] == "iris_then_move"
    assert result["message"]["subject"] == "Trip"
    assert result["defaults"]["unmatched_action"] == "inspect"


def test_exactly_one_message_source_is_required(tmp_path: Path) -> None:
    client = FakeIMAP()

    with pytest.raises(ValueError, match="exactly one"):
        classifier(client).classify()
    with pytest.raises(ValueError, match="exactly one"):
        classifier(client).classify(mail_id="mail:x", path=tmp_path / "message.eml")


def test_an_unreadable_message_file_is_a_plain_request_failure(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="could not be read"):
        classifier(FakeIMAP()).classify(path=tmp_path / "absent.eml")


def test_a_stale_or_unknown_mail_id_is_refused() -> None:
    client = FakeIMAP()

    with pytest.raises(ValueError, match="stale"):
        classifier(client).classify(
            mail_id=encode_mail_id(MailReference("INBOX", 41, 7, "icloud"))
        )
    with pytest.raises(ValueError, match="not valid"):
        classifier(client).classify(mail_id="not-a-mail-id")
    with pytest.raises(ValueError, match="not enabled"):
        classifier(client).classify(
            mail_id=encode_mail_id(MailReference("INBOX", 42, 7, "outlook"))
        )
