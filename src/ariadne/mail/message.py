"""Shared plain-text extraction for parsed mail messages."""

from __future__ import annotations

import re
from email.message import EmailMessage
from html.parser import HTMLParser

BLOCK_TAGS = frozenset({"br", "div", "li", "p", "table", "tr"})


class _HTMLText(HTMLParser):
    """Collect readable text, marking block boundaries as line breaks."""

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in BLOCK_TAGS:
            self.parts.append("\n")


def _parts(message: EmailMessage) -> tuple[list[str], bool]:
    part = None
    try:
        part = message.get_body(preferencelist=("plain", "html"))
        content = part.get_content() if part is not None else ""
    except (KeyError, LookupError, UnicodeError, ValueError):
        content = ""
    text = content if isinstance(content, str) else str(content)
    if part is not None and part.get_content_type() == "text/html":
        parser = _HTMLText()
        parser.feed(text)
        return parser.parts, True
    return [text], False


def collapsed_text(message: EmailMessage, limit: int) -> tuple[str, bool]:
    """Return one whitespace-collapsed line of body text, and whether it was cut."""
    parts, _was_html = _parts(message)
    text = re.sub(r"\s+", " ", " ".join(parts)).strip()
    return text[:limit], len(text) > limit


def quotable_text(message: EmailMessage, limit: int) -> tuple[str, bool]:
    """Return body text with its line structure kept, for quoting in a reply."""
    parts, was_html = _parts(message)
    text = ("" if was_html else " ").join(parts)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit], len(text) > limit
