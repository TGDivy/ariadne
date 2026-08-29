"""Render mail and selected attachments as bounded external prompt evidence."""

from __future__ import annotations

import email
import logging
import re
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from io import BytesIO
from typing import TYPE_CHECKING

from pypdf import PdfReader

if TYPE_CHECKING:
    from ..mail.models import MailMetadata

LOGGER = logging.getLogger(__name__)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _decode_part(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return str(payload or "")
    return payload.decode(part.get_content_charset() or "utf-8", errors="replace")


def _compact(text: str, limit: int) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def render_mail_evidence(raw: bytes, metadata: MailMetadata) -> str:
    """Render useful body text plus PDF/ICS interpretations for a mail turn."""
    message = BytesParser(policy=policy.default).parsebytes(raw)
    plain: list[str] = []
    html: list[str] = []
    attachments: list[str] = []
    interpreted: list[str] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type()
        filename = str(part.get_filename() or "")
        disposition = part.get_content_disposition()
        is_attachment = disposition == "attachment" or bool(filename)
        if not is_attachment and content_type == "text/plain":
            plain.append(_decode_part(part))
            continue
        if not is_attachment and content_type == "text/html":
            parser = _TextExtractor()
            parser.feed(_decode_part(part))
            html.append(" ".join(parser.parts))
            continue
        if not is_attachment:
            continue
        label = filename or "unnamed attachment"
        attachments.append(f"{label} ({content_type})")
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        if content_type == "application/pdf" or filename.casefold().endswith(".pdf"):
            try:
                pages = PdfReader(BytesIO(payload)).pages
                text = "\n".join(page.extract_text() or "" for page in pages)
                interpreted.append(f"PDF {label}:\n{_compact(text, 20_000)}")
            except Exception:
                LOGGER.warning("Could not extract PDF attachment %s", label)
        elif content_type == "text/calendar" or filename.casefold().endswith(".ics"):
            calendar = payload.decode(
                part.get_content_charset() or "utf-8", errors="replace"
            )
            interpreted.append(f"Calendar {label}:\n{_compact(calendar, 12_000)}")

    body = _compact("\n\n".join(plain or html), 30_000)
    fields = [
        f"From: {', '.join(metadata.sender)}",
        f"To: {', '.join(metadata.recipients)}",
        f"Date: {metadata.date}",
        f"Subject: {metadata.subject}",
        f"Message-ID: {metadata.message_id}",
        "",
        "Body:",
        body or "(No readable body text.)",
    ]
    if attachments:
        fields.extend(("", "Attachments:", *attachments))
    if interpreted:
        fields.extend(("", "Interpreted attachments:", *interpreted))
    return "\n".join(fields)
