from email.message import EmailMessage

from ariadne.scripts.mail_export import fetch_messages, parse_message


def make_message() -> bytes:
    message = EmailMessage()
    message["From"] = "Recruiter <recruiter@example.com>"
    message["To"] = "work@divyb.xyz"
    message["Subject"] = "Interview details"
    message["Date"] = "Sun, 23 Aug 2026 10:00:00 +0000"
    message["Message-ID"] = "<message@example.com>"
    message.set_content("Please reply with your availability.")
    message.add_attachment(
        b"PDF bytes", maintype="application", subtype="pdf", filename="role.pdf"
    )
    return message.as_bytes()


class FakeMail:
    def __init__(self, raw_by_uid: dict[str, bytes]) -> None:
        self.raw_by_uid = raw_by_uid
        self.calls: list[tuple[object, ...]] = []

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", folder, readonly))
        return "OK", [b"1"]

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        self.calls.append((command, *args))
        if command == "search":
            return "OK", [b"12 13"]
        uid_set = str(args[0])
        return "OK", [
            (
                f"{uid} (UID {uid} BODY[] {{{len(raw)}}})".encode(),
                raw,
            )
            for uid, raw in self.raw_by_uid.items()
            if uid in uid_set.split(",")
        ]


def test_parse_message_keeps_body_and_attachment_metadata_without_binary_attachment():
    parsed = parse_message(make_message(), b"12", "INBOX")

    assert parsed["uid"] == "12"
    assert parsed["from"] == [{"name": "Recruiter", "address": "recruiter@example.com"}]
    assert "availability" in parsed["body_text"]
    assert parsed["attachments"] == [
        {
            "filename": "role.pdf",
            "content_type": "application/pdf",
            "content_disposition": "attachment",
        }
    ]


def test_fetch_selects_read_only_and_uses_body_peek():
    mail = FakeMail({"12": make_message(), "13": make_message()})
    updates: list[tuple[int, int]] = []

    results = fetch_messages(
        mail,
        "INBOX",
        1000,
        batch_size=25,
        progress=lambda done, total: updates.append((done, total)),
    )

    assert len(results) == 2
    assert updates == [(0, 2), (2, 2)]
    assert mail.calls == [
        ("select", "INBOX", True),
        ("search", None, "ALL"),
        ("fetch", "12,13", "(UID BODY.PEEK[])"),
    ]
