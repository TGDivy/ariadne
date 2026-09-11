"""Build and store inert provider-side drafts. Ariadne never sends mail.

The only mailbox write here is an IMAP APPEND carrying the `\\Draft` flag into
the account's own Drafts folder. There is no submission path of any kind, and
nothing in Ariadne later selects, sends, or acts on a message it has drafted.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import (
    format_datetime,
    formataddr,
    getaddresses,
    make_msgid,
    parsedate_to_datetime,
)
from typing import Any

from imapclient import IMAPClient  # type: ignore[import-untyped]

from .accounts import MailAccountRegistry, select_account
from .message import quotable_text
from .reader import MESSAGE_ID, MailReader, decode_mail_id

DRAFT_FLAGS = (b"\\Draft", b"\\Seen")
DRAFT_FOLDER_FLAG = "\\drafts"
DRAFT_FOLDER_NAMES = frozenset({"draft", "drafts"})
QUOTE_LIMIT = 8_000
REFERENCES_LIMIT = 20
BODY_LIMIT = 100_000
SUBJECT_LIMIT = 500
NO_SUBJECT = "(no subject)"


@dataclass(frozen=True, slots=True)
class DraftContent:
    """One complete draft message and the facts worth reporting about it."""

    message: EmailMessage
    subject: str
    to: tuple[str, ...]
    cc: tuple[str, ...]
    message_id: str
    in_reply_to: str | None
    quoted: bool
    quote_truncated: bool


def _header(message: EmailMessage, name: str) -> str:
    return str(message.get(name, "") or "")


def _pairs(*values: str) -> tuple[tuple[str, str], ...]:
    return tuple(
        (name, address)
        for name, address in getaddresses(list(values))
        if address.strip()
    )


def _rendered(pairs: Iterable[tuple[str, str]]) -> tuple[str, ...]:
    return tuple(formataddr((name, address)) for name, address in pairs)


def _without(
    pairs: Iterable[tuple[str, str]], exclude: Iterable[str]
) -> tuple[tuple[str, str], ...]:
    """Drop excluded and repeated addresses while preserving the original order."""
    seen = {value.casefold() for value in exclude}
    kept: list[tuple[str, str]] = []
    for name, address in pairs:
        key = address.casefold()
        if key in seen:
            continue
        seen.add(key)
        kept.append((name, address))
    return tuple(kept)


def validated_address(value: str) -> str:
    """Accept one ordinary address, rejecting anything that could forge headers."""
    candidate = value.strip()
    if not candidate or any(character in candidate for character in "\r\n"):
        raise ValueError(f"{value!r} is not a valid email address.")
    pairs = _pairs(candidate)
    if len(pairs) != 1:
        raise ValueError(f"{value!r} must be exactly one email address.")
    address = pairs[0][1]
    if address.count("@") != 1 or any(part == "" for part in address.split("@")):
        raise ValueError(f"{value!r} is not a valid email address.")
    return candidate


def validated_subject(value: str) -> str:
    subject = value.strip()
    if not subject or any(character in subject for character in "\r\n"):
        raise ValueError("A draft subject must be one line of text.")
    if len(subject) > SUBJECT_LIMIT:
        raise ValueError(f"A draft subject must be at most {SUBJECT_LIMIT} characters.")
    return subject


def _validated_body(value: str) -> str:
    if len(value) > BODY_LIMIT:
        raise ValueError(f"A draft body must be at most {BODY_LIMIT} characters.")
    body = value.strip()
    if not body:
        raise ValueError("A draft needs a body.")
    return body


def reply_subject(original: str) -> str:
    """Prefix exactly one `Re:` regardless of how the thread was already labelled."""
    base = original.strip() or NO_SUBJECT
    return base if base.casefold().startswith("re:") else f"Re: {base}"


def _references(existing: str, parent_id: str) -> str:
    identifiers = list(dict.fromkeys([*MESSAGE_ID.findall(existing), parent_id]))
    if len(identifiers) > REFERENCES_LIMIT:
        identifiers = [identifiers[0], *identifiers[-(REFERENCES_LIMIT - 1) :]]
    return " ".join(identifiers)


def _attribution(original: EmailMessage) -> str:
    sender = _pairs(_header(original, "From"))
    who = (sender[0][0] or sender[0][1]) if sender else ""
    raw_date = _header(original, "Date").strip()
    try:
        when = parsedate_to_datetime(raw_date).strftime("%a, %d %b %Y at %H:%M")
    except (TypeError, ValueError, OverflowError):
        when = raw_date
    if who and when:
        return f"On {when}, {who} wrote:"
    if who:
        return f"{who} wrote:"
    if when:
        return f"On {when}, the sender wrote:"
    return "The sender wrote:"


def _quoted(original: EmailMessage, limit: int) -> tuple[str, bool]:
    text, truncated = quotable_text(original, limit)
    if not text:
        return "", False
    lines = [f"> {line}" if line else ">" for line in text.split("\n")]
    if truncated:
        lines.append("> [truncated]")
    return "\n".join(lines), truncated


def _domain(address: str) -> str | None:
    _name, _separator, domain = address.rpartition("@")
    return domain or None


def _assembled(
    *,
    body: str,
    from_address: str,
    to: tuple[tuple[str, str], ...],
    cc: tuple[tuple[str, str], ...],
    subject: str,
    parent_id: str | None,
    references: str | None,
    quote: str,
    attribution: str,
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = from_address
    message["To"] = ", ".join(_rendered(to))
    if cc:
        message["Cc"] = ", ".join(_rendered(cc))
    message["Subject"] = subject
    if parent_id:
        message["In-Reply-To"] = parent_id
        if references:
            message["References"] = references
    message["Message-ID"] = make_msgid(domain=_domain(from_address))
    message["Date"] = format_datetime(datetime.now(UTC))
    content = f"{body}\n\n{attribution}\n{quote}\n" if quote else f"{body}\n"
    message.set_content(content)
    return message


def build_reply(
    original: EmailMessage,
    *,
    body: str,
    from_address: str,
    quote_limit: int = QUOTE_LIMIT,
) -> DraftContent:
    """Build a reply-all draft that threads under the message it answers."""
    text = _validated_body(body)
    own = (from_address.casefold(),)
    audience = _pairs(_header(original, "Reply-To")) or _pairs(
        _header(original, "From")
    )
    to = _without(audience, own)
    if not to:
        to = _without(_pairs(_header(original, "To")), own)
    if not to:
        raise ValueError("That message has no address to reply to.")
    cc = _without(
        _pairs(_header(original, "To"), _header(original, "Cc")),
        (*own, *(address for _name, address in to)),
    )
    subject = reply_subject(_header(original, "Subject"))
    parent_id = _header(original, "Message-ID").strip()
    quote, quote_truncated = _quoted(original, quote_limit)
    message = _assembled(
        body=text,
        from_address=from_address,
        to=to,
        cc=cc,
        subject=subject,
        parent_id=parent_id or None,
        references=_references(_header(original, "References"), parent_id)
        if parent_id
        else None,
        quote=quote,
        attribution=_attribution(original) if quote else "",
    )
    return DraftContent(
        message=message,
        subject=subject,
        to=_rendered(to),
        cc=_rendered(cc),
        message_id=str(message["Message-ID"]),
        in_reply_to=parent_id or None,
        quoted=bool(quote),
        quote_truncated=quote_truncated,
    )


def build_message(
    *,
    body: str,
    from_address: str,
    to: Sequence[str],
    cc: Sequence[str] = (),
    subject: str,
) -> DraftContent:
    """Build a new draft to explicitly named recipients."""
    text = _validated_body(body)
    if not to:
        raise ValueError("A new draft needs at least one recipient.")
    checked_subject = validated_subject(subject)
    recipients = _without(_pairs(*(validated_address(value) for value in to)), ())
    if not recipients:
        raise ValueError("A new draft needs at least one recipient.")
    copies = _without(
        _pairs(*(validated_address(value) for value in cc)),
        tuple(address for _name, address in recipients),
    )
    message = _assembled(
        body=text,
        from_address=from_address,
        to=recipients,
        cc=copies,
        subject=checked_subject,
        parent_id=None,
        references=None,
        quote="",
        attribution="",
    )
    return DraftContent(
        message=message,
        subject=checked_subject,
        to=_rendered(recipients),
        cc=_rendered(copies),
        message_id=str(message["Message-ID"]),
        in_reply_to=None,
        quoted=False,
        quote_truncated=False,
    )


def drafts_folder(client: IMAPClient) -> str:
    """Find the account's own Drafts folder; never create or invent one."""
    fallback: str | None = None
    for flags, delimiter, name in client.list_folders():
        folder = name.decode() if isinstance(name, bytes) else str(name)
        decoded_flags = {
            (flag.decode() if isinstance(flag, bytes) else str(flag)).casefold()
            for flag in flags
        }
        if DRAFT_FOLDER_FLAG in decoded_flags:
            return folder
        separator = (
            delimiter.decode() if isinstance(delimiter, bytes) else str(delimiter or "")
        )
        leaf = folder.rsplit(separator, 1)[-1] if separator else folder
        if fallback is None and leaf.casefold() in DRAFT_FOLDER_NAMES:
            fallback = folder
    if fallback is not None:
        return fallback
    raise RuntimeError(
        "This account has no Drafts folder; create one in your mail client."
    )


def append_draft(client: IMAPClient, message: EmailMessage, *, folder: str) -> None:
    """Store one message as an unsent draft. This is the only write performed."""
    client.append(
        folder,
        message.as_bytes(),
        flags=DRAFT_FLAGS,
        msg_time=datetime.now(UTC),
    )


class MailDrafts:
    """Create inert drafts in whichever enabled account owns the conversation."""

    def __init__(self, registry: MailAccountRegistry) -> None:
        self.registry = registry

    def create_draft(
        self,
        *,
        body: str,
        reply_to: str | None = None,
        account: str | None = None,
        to: Sequence[str] = (),
        cc: Sequence[str] = (),
        subject: str | None = None,
    ) -> dict[str, Any]:
        if reply_to is not None:
            return self._reply(reply_to, body=body, account=account)
        return self._compose(
            body=body, account=account, to=to, cc=cc, subject=subject or ""
        )

    def _reply(
        self, reply_to: str, *, body: str, account: str | None
    ) -> dict[str, Any]:
        reference = decode_mail_id(reply_to)
        settings = self.registry.get(reference.account_key)
        if account is not None and account != settings.key:
            raise ValueError("That mail id belongs to a different account.")
        with self.registry.connect(settings.key) as client:
            original = (
                MailReader(client, settings.key, settings.label)
                .read_candidate(reply_to)
                .message
            )
            content = build_reply(original, body=body, from_address=settings.address)
            folder = drafts_folder(client)
            append_draft(client, content.message, folder=folder)
        return _payload(content, key=settings.key, label=settings.label, folder=folder)

    def _compose(
        self,
        *,
        body: str,
        account: str | None,
        to: Sequence[str],
        cc: Sequence[str],
        subject: str,
    ) -> dict[str, Any]:
        settings = select_account(
            self.registry.accounts, account, require_explicit_when_multiple=True
        )
        content = build_message(
            body=body,
            from_address=settings.address,
            to=to,
            cc=cc,
            subject=subject,
        )
        with self.registry.connect(settings.key) as client:
            folder = drafts_folder(client)
            append_draft(client, content.message, folder=folder)
        return _payload(content, key=settings.key, label=settings.label, folder=folder)


def _payload(
    content: DraftContent, *, key: str, label: str, folder: str
) -> dict[str, Any]:
    return {
        "status": "drafted",
        "sent": False,
        "account_key": key,
        "account_label": label,
        "folder": folder,
        "subject": content.subject,
        "to": list(content.to),
        "cc": list(content.cc),
        "message_id": content.message_id,
        "in_reply_to": content.in_reply_to,
        "quoted_original": content.quoted,
        "quote_truncated": content.quote_truncated,
    }
