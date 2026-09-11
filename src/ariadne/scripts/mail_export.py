"""Export recent messages from one Mail account for local mailbox analysis.

Run with Ariadne's TOML configuration, for example:

    uv run python -m ariadne.scripts.mail_export \
        --limit 1000 --output mail-export.jsonl

The export contains headers, routing metadata, plain-text body excerpts, and
attachment metadata. It never sends, moves, deletes, or marks messages read.
"""

from __future__ import annotations

import argparse
import email
import imaplib
import json
import re
import tempfile
from collections.abc import Callable
from datetime import UTC
from email.header import decode_header, make_header
from email.utils import getaddresses, parsedate_to_datetime
from pathlib import Path
from typing import Any

from ariadne.config import MailAccountSettings, load_settings
from ariadne.mail import OutlookOAuth, select_account
from ariadne.scripts.progress import ProgressBar


def decode(value: str | None) -> str:
    if not value:
        return ""
    return str(make_header(decode_header(value)))


def addresses(value: str | None) -> list[dict[str, str]]:
    return [
        {"name": decode(name), "address": address}
        for name, address in getaddresses([decode(value)])
        if name or address
    ]


def message_timestamp(value: str) -> str | None:
    try:
        return parsedate_to_datetime(value).astimezone(UTC).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def body_text(message: email.message.Message, limit: int) -> tuple[str, bool]:
    for part in message.walk():
        if part.get_content_type() != "text/plain":
            continue
        if part.get_content_disposition() == "attachment":
            continue
        payload = part.get_payload(decode=True) or b""
        if isinstance(payload, bytes):
            text = payload.decode(
                part.get_content_charset() or "utf-8", errors="replace"
            )
        else:
            text = str(payload)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:limit], len(text) > limit
    return "", False


def attachment_metadata(message: email.message.Message) -> list[dict[str, Any]]:
    attachments = []
    for part in message.walk():
        filename = decode(part.get_filename())
        if part.get_content_disposition() != "attachment" and not filename:
            continue
        attachments.append(
            {
                "filename": filename,
                "content_type": part.get_content_type(),
                "content_disposition": part.get_content_disposition() or "",
            }
        )
    return attachments


def parse_message(
    raw: bytes,
    uid: bytes | str,
    folder: str,
    *,
    account_key: str = "icloud",
    account_label: str = "iCloud",
) -> dict[str, Any]:
    message = email.message_from_bytes(raw)
    date = decode(message.get("Date"))
    text, truncated = body_text(message, limit=12_000)
    return {
        "account_key": account_key,
        "account_label": account_label,
        "folder": folder,
        "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
        "message_id": decode(message.get("Message-ID")),
        "in_reply_to": decode(message.get("In-Reply-To")),
        "references": decode(message.get("References")),
        "date": date,
        "timestamp": message_timestamp(date),
        "from": addresses(message.get("From")),
        "to": addresses(message.get("To")),
        "cc": addresses(message.get("Cc")),
        "subject": decode(message.get("Subject")),
        "list_unsubscribe": decode(message.get("List-Unsubscribe")),
        "precedence": decode(message.get("Precedence")),
        "auto_submitted": decode(message.get("Auto-Submitted")),
        "body_text": text,
        "body_truncated": truncated,
        "attachments": attachment_metadata(message),
    }


def fetch_batch(mail: imaplib.IMAP4_SSL, uids: list[bytes]) -> dict[str, bytes]:
    """Fetch a batch and map each returned MIME payload back to its UID."""
    uid_set = ",".join(uid.decode() for uid in uids)
    status, fetched = mail.uid("fetch", uid_set, "(UID BODY.PEEK[])")
    if status != "OK":
        return {}

    payloads = {}
    for item in fetched:
        if not isinstance(item, tuple):
            continue
        metadata, raw = item
        if not isinstance(metadata, bytes) or not isinstance(raw, bytes):
            continue
        match = re.search(rb"\bUID\s+(\d+)\b", metadata, re.IGNORECASE)
        if match:
            payloads[match.group(1).decode()] = raw
    return payloads


def fetch_one(mail: imaplib.IMAP4_SSL, uid: bytes) -> bytes | None:
    """Fallback for a UID that a server could not return in a batch."""
    status, fetched = mail.uid("fetch", uid.decode(), "(UID BODY.PEEK[])")
    if status != "OK":
        return None
    raw = next(
        (
            item[1]
            for item in fetched
            if isinstance(item, tuple) and isinstance(item[1], bytes)
        ),
        b"",
    )
    return raw or None


def fetch_messages(
    mail: imaplib.IMAP4_SSL,
    folder: str,
    limit: int,
    batch_size: int = 25,
    progress: Callable[[int, int], None] | None = None,
    *,
    account_key: str = "icloud",
    account_label: str = "iCloud",
) -> list[dict[str, Any]]:
    status, _ = mail.select(folder, readonly=True)
    if status != "OK":
        raise RuntimeError(f"IMAP select failed for {folder!r}: {status}")

    status, data = mail.uid("search", None, "ALL")  # type: ignore[arg-type]
    if status != "OK":
        raise RuntimeError(f"IMAP search failed: {status}")

    uids = data[0].split()[-limit:]
    if progress is not None:
        progress(0, len(uids))
    results = []
    for start in range(0, len(uids), batch_size):
        batch = uids[start : start + batch_size]
        payloads = fetch_batch(mail, batch)
        for uid in batch:
            uid_text = uid.decode()
            raw = payloads.get(uid_text) or fetch_one(mail, uid)
            if raw:
                results.append(
                    parse_message(
                        raw,
                        uid,
                        folder,
                        account_key=account_key,
                        account_label=account_label,
                    )
                )
        if progress is not None:
            progress(min(start + len(batch), len(uids)), len(uids))
    return results


def write_jsonl(path: Path, messages: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as temporary:
        temporary_path = Path(temporary.name)
        for message in messages:
            temporary.write(json.dumps(message, ensure_ascii=False) + "\n")
    temporary_path.replace(path)


def authenticate(mail: imaplib.IMAP4_SSL, account: MailAccountSettings) -> None:
    """Authenticate an export connection without putting credentials in output."""
    try:
        if account.provider == "icloud":
            if account.password is None:
                raise RuntimeError("The selected iCloud account has no app password.")
            mail.login(account.address, account.password.get_secret_value())
            return
        access_token = OutlookOAuth(account).access_token()
        response = (
            f"user={account.address}\x01auth=Bearer {access_token}\x01\x01".encode()
        )
        mail.authenticate("XOAUTH2", lambda _challenge: response)
    except imaplib.IMAP4.error:
        raise RuntimeError(
            f"{account.label} rejected the configured Mail credentials."
        ) from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--account", help="stable Mail account key (required when multiple are enabled)"
    )
    parser.add_argument("--folder", default="INBOX")
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument(
        "--batch-size",
        type=int,
        default=25,
        help="Messages per IMAP fetch request (default: 25)",
    )
    parser.add_argument("--output", type=Path, default=Path("mail-export.jsonl"))
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")

    configured = load_settings(args.config).mail_settings
    if configured is None:
        raise RuntimeError("Mail must be enabled to export messages.")
    account = select_account(
        configured.accounts,
        args.account,
        require_explicit_when_multiple=True,
    )
    mail = imaplib.IMAP4_SSL(account.host, 993)
    try:
        authenticate(mail, account)
        with ProgressBar(f"Fetching {args.folder!r}") as progress:
            messages = fetch_messages(
                mail,
                args.folder,
                args.limit,
                args.batch_size,
                progress=progress.update,
                account_key=account.key,
                account_label=account.label,
            )
        write_jsonl(args.output, messages)
        print(
            f"Wrote {len(messages)} messages from account={account.key} "
            f"to {args.output}"
        )
    finally:
        try:
            mail.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
