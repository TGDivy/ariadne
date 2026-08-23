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
    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self.calls: list[tuple[object, ...]] = []

    def select(self, folder: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("select", folder, readonly))
        return "OK", [b"1"]

    def uid(self, command: str, *args: object) -> tuple[str, list[object]]:
        self.calls.append((command, *args))
        if command == "search":
            return "OK", [b"12"]
        return "OK", [(b"metadata", self.raw)]


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
    mail = FakeMail(make_message())
    updates: list[tuple[int, int]] = []

    results = fetch_messages(
        mail,
        "INBOX",
        1000,
        progress=lambda done, total: updates.append((done, total)),
    )

    assert len(results) == 1
    assert updates == [(1, 1)]
    assert mail.calls == [
        ("select", "INBOX", True),
        ("search", None, "ALL"),
        ("fetch", b"12", "(BODY.PEEK[])")
    ]
