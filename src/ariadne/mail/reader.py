"""Direct, read-only iCloud Mail access for ordinary Iris turns."""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any

from imapclient import IMAPClient  # type: ignore[import-untyped]

LOGGER = logging.getLogger(__name__)
PREVIEW_BYTES = 16_384
RECENT_PER_FOLDER = 75
MATCHES_PER_FOLDER = 150
THREAD_PER_FOLDER = 100
THREAD_LIMIT = 30
EXCLUDED_FLAGS = {"\\drafts", "\\junk", "\\noselect", "\\trash"}
MESSAGE_ID = re.compile(r"<[^>]+>")


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


@dataclass(frozen=True, slots=True)
class MailReference:
    folder: str
    uidvalidity: int
    uid: int


@dataclass(frozen=True, slots=True)
class Candidate:
    reference: MailReference
    message: EmailMessage
    server_match: bool


def encode_mail_id(reference: MailReference) -> str:
    """Encode an IMAP identity without keeping local state."""
    raw = json.dumps(
        [reference.folder, reference.uidvalidity, reference.uid],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()
    return "mail:" + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_mail_id(value: str) -> MailReference:
    """Decode an id returned by search_mail."""
    try:
        if not value.startswith("mail:"):
            raise ValueError
        token = value.removeprefix("mail:")
        folder, validity, uid = json.loads(
            base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
        )
        if not isinstance(folder, str) or not folder:
            raise ValueError
        if not isinstance(validity, int) or validity <= 0:
            raise ValueError
        if not isinstance(uid, int) or uid <= 0:
            raise ValueError
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("That mail id is not valid. Search mail again.") from error
    return MailReference(folder, validity, uid)


def _text(value: object) -> str:
    return str(value or "")


def _body(message: EmailMessage, limit: int) -> tuple[str, bool]:
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
        text = " ".join(parser.parts)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit], len(text) > limit


def _addresses(message: EmailMessage, field: str) -> list[dict[str, str]]:
    return [
        {"name": name, "address": address}
        for name, address in getaddresses([_text(message.get(field))])
        if name or address
    ]


def _timestamp(message: EmailMessage) -> str | None:
    try:
        parsed = parsedate_to_datetime(_text(message.get("Date")))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _payload(candidate: Candidate, body_limit: int) -> dict[str, Any]:
    message = candidate.message
    body, truncated = _body(message, body_limit)
    attachments = [
        {
            "filename": _text(part.get_filename()),
            "content_type": part.get_content_type(),
        }
        for part in message.walk()
        if part.get_content_disposition() == "attachment" or part.get_filename()
    ]
    return {
        "id": encode_mail_id(candidate.reference),
        "folder": candidate.reference.folder,
        "date": _text(message.get("Date")),
        "timestamp": _timestamp(message),
        "from": _addresses(message, "From"),
        "to": _addresses(message, "To"),
        "cc": _addresses(message, "Cc"),
        "subject": _text(message.get("Subject")),
        "message_id": _text(message.get("Message-ID")),
        "in_reply_to": _text(message.get("In-Reply-To")),
        "references": _text(message.get("References")),
        "body": body,
        "body_truncated": truncated,
        "attachments": attachments,
    }


def _body_response(item: dict[object, object]) -> bytes | None:
    return next(
        (
            value
            for key, value in item.items()
            if (key if isinstance(key, bytes) else str(key).encode())
            .upper()
            .startswith(b"BODY[")
            and isinstance(value, bytes)
        ),
        None,
    )


def _validity(selected: dict[object, object]) -> int:
    value = selected.get(b"UIDVALIDITY", selected.get("UIDVALIDITY"))
    if not isinstance(value, (bytes, str, int)):
        raise RuntimeError("IMAP did not return UIDVALIDITY.")
    return int(value)


def _boundary(value: str | None, name: str) -> date | None:
    try:
        return date.fromisoformat(value) if value is not None else None
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO date such as 2026-08-23.") from error


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(re.findall(r"[^\W_]+", value.casefold())))


def _message_ids(*values: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            item.casefold() for value in values for item in MESSAGE_ID.findall(value)
        )
    )


def _score(candidate: Candidate, query: str) -> int | None:
    message = candidate.message
    sender = " ".join(
        f"{value['name']} {value['address']}" for value in _addresses(message, "From")
    ).casefold()
    subject = _text(message.get("Subject")).casefold()
    body = _body(message, 8_000)[0].casefold()
    phrase = query.casefold().strip()
    tokens = _tokens(query)
    score = 18 * (phrase in sender) + 14 * (phrase in subject) + 7 * (phrase in body)
    for token in tokens:
        score += 6 * (token in sender) + 4 * (token in subject) + (token in body)
    if tokens and all(token in f"{sender} {subject} {body}" for token in tokens):
        score += 8
    if not score and not candidate.server_match:
        return None
    return max(score, 1)


def _sort_key(candidate: Candidate) -> tuple[float, int]:
    timestamp = _timestamp(candidate.message)
    try:
        seconds = datetime.fromisoformat(timestamp).timestamp() if timestamp else 0
    except ValueError:
        seconds = 0
    return seconds, candidate.reference.uid


class MailReader:
    """Search and refetch mail using read-only selects and BODY.PEEK."""

    def __init__(self, client: IMAPClient) -> None:
        self.client = client

    def _folders(self) -> tuple[str, ...]:
        folders = []
        for flags, _delimiter, name in self.client.list_folders():
            decoded_flags = {
                (flag.decode() if isinstance(flag, bytes) else str(flag)).casefold()
                for flag in flags
            }
            if decoded_flags & EXCLUDED_FLAGS:
                continue
            folders.append(name.decode() if isinstance(name, bytes) else str(name))
        return tuple(sorted(folders, key=lambda name: (name != "INBOX", name)))

    def _fetch(
        self,
        folder: str,
        validity: int,
        uids: set[int],
        server_matches: set[int],
        *,
        full: bool = False,
    ) -> list[Candidate]:
        result = []
        query = [b"BODY.PEEK[]" if full else f"BODY.PEEK[]<0.{PREVIEW_BYTES}>".encode()]
        ordered = sorted(uids)
        for start in range(0, len(ordered), 50):
            for uid, item in self.client.fetch(
                ordered[start : start + 50], query
            ).items():
                raw = _body_response(item)
                if raw is None:
                    continue
                message = BytesParser(policy=policy.default).parsebytes(raw)
                result.append(
                    Candidate(
                        MailReference(folder, validity, int(uid)),
                        message,
                        int(uid) in server_matches,
                    )
                )
        return result

    def search(
        self,
        query: str,
        *,
        since: str | None = None,
        before: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        query = query.strip()
        if not query:
            raise ValueError("A mail search needs a query.")
        if not 1 <= limit <= 100:
            raise ValueError("Mail search limit must be between 1 and 100.")
        since_date, before_date = _boundary(since, "since"), _boundary(before, "before")
        if since_date and before_date and since_date >= before_date:
            raise ValueError("since must be earlier than before.")
        criteria: list[object] = []
        if since_date:
            criteria.extend(("SINCE", since_date))
        if before_date:
            criteria.extend(("BEFORE", before_date))
        criteria = criteria or ["ALL"]

        candidates: list[Candidate] = []
        searched = 0
        terms = tuple(
            dict.fromkeys((query, *[t for t in _tokens(query) if len(t) > 2]))
        )
        for folder in self._folders():
            try:
                validity = _validity(self.client.select_folder(folder, readonly=True))
                recent = {int(uid) for uid in self.client.search(criteria)}
                recent = set(sorted(recent)[-RECENT_PER_FOLDER:])
                matches: set[int] = set()
                for term in terms:
                    found = self.client.search([*criteria, "TEXT", term])
                    matches.update(int(uid) for uid in found)
                matches = set(sorted(matches)[-MATCHES_PER_FOLDER:])
                candidates.extend(
                    self._fetch(folder, validity, recent | matches, matches)
                )
                searched += 1
            except Exception:
                LOGGER.warning("Could not search mail folder %s", folder, exc_info=True)
        if not searched:
            raise RuntimeError("No mail folders could be searched.")

        ranked = [
            (score, _sort_key(candidate), candidate)
            for candidate in candidates
            if (score := _score(candidate, query)) is not None
        ]
        ranked.sort(reverse=True, key=lambda item: item[:2])
        results = []
        for score, _key, candidate in ranked[:limit]:
            payload = _payload(candidate, 700)
            payload["preview"] = payload.pop("body")
            payload.pop("body_truncated")
            payload.pop("in_reply_to")
            payload.pop("references")
            payload["relevance"] = score
            results.append(payload)
        return {"query": query, "results": results, "searched_folders": searched}

    def _read_candidate(self, value: str) -> Candidate:
        reference = decode_mail_id(value)
        selected = self.client.select_folder(reference.folder, readonly=True)
        if _validity(selected) != reference.uidvalidity:
            raise ValueError("That mail id is stale. Search mail again.")
        candidates = self._fetch(
            reference.folder,
            reference.uidvalidity,
            {reference.uid},
            {reference.uid},
            full=True,
        )
        if not candidates:
            raise ValueError("That message is no longer available. Search mail again.")
        return candidates[0]

    def read(self, value: str) -> dict[str, Any]:
        return _payload(self._read_candidate(value), 50_000)

    def read_thread(self, value: str) -> dict[str, Any]:
        target = self._read_candidate(value)
        target_payload = _payload(target, 1)
        ids = _message_ids(
            target_payload["message_id"],
            target_payload["in_reply_to"],
            target_payload["references"],
        )
        candidates = [target]
        subject = _text(target.message.get("Subject"))
        for folder in self._folders():
            try:
                validity = _validity(self.client.select_folder(folder, readonly=True))
                matches: set[int] = set()
                for message_id in ids[:12]:
                    for field in ("Message-ID", "References", "In-Reply-To"):
                        matches.update(
                            int(uid)
                            for uid in self.client.search(["HEADER", field, message_id])
                        )
                if not ids and subject:
                    matches.update(
                        int(uid) for uid in self.client.search(["SUBJECT", subject])
                    )
                matches = set(sorted(matches)[-THREAD_PER_FOLDER:])
                candidates.extend(
                    self._fetch(folder, validity, matches, matches, full=True)
                )
            except Exception:
                LOGGER.warning(
                    "Could not read mail thread in %s", folder, exc_info=True
                )

        unique = {
            encode_mail_id(candidate.reference): candidate for candidate in candidates
        }
        ordered = sorted(unique.values(), key=_sort_key)
        total = len(ordered)
        if total > THREAD_LIMIT:
            ordered = ordered[-THREAD_LIMIT:]
            if target not in ordered:
                ordered[0] = target
                ordered.sort(key=_sort_key)
        return {
            "thread_id": value,
            "messages": [_payload(candidate, 12_000) for candidate in ordered],
            "total_messages": total,
            "thread_truncated": total > THREAD_LIMIT,
        }
