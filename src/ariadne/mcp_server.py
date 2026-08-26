"""Local FastMCP capabilities for Ariadne's Codex conversation."""

import asyncio
import logging
import os
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, cast

from caldav.lib.error import AuthorizationError, ETagMismatchError, RateLimitError
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from imapclient import IMAPClient  # type: ignore[import-untyped]
from telegram import Bot, ReplyParameters
from telegram.constants import ParseMode, ReactionEmoji
from telegram.error import BadRequest, EndPointNotFound, TelegramError

from .calendar import (
    CalendarConflict,
    CalendarError,
    CalendarStatus,
    ICloudCalendar,
    InvitationResponse,
    UpdateScope,
)
from .mail import (
    IMAP_HOST,
    Importance,
    MailReader,
    SuggestedAction,
    record_current_mail_decision,
)
from .profile import PROFILES
from .telegram.file_delivery import FileDelivery, FileDeliveryError
from .telegram.format import split_for_telegram, telegram_messages
from .telegram.questions import (
    QUESTION_TIMEOUT_SECONDS,
    ActiveQuestionError,
    TelegramQuestionCard,
    TelegramQuestionStore,
    question_state_path,
    validate_question,
)
from .telegram.rich import RichBotAPI, split_rich_markdown

LOGGER = logging.getLogger(__name__)

mcp = FastMCP(
    "Ariadne",
    instructions=(
        "Local runtime inspection, speaking in Telegram, "
        "and explicitly approved Telegram delivery."
    ),
    version="0.1.0",
    strict_input_validation=True,
)

VARIATION_SELECTOR = "\ufe0f"
REACTIONS = {
    emoji.value.replace(VARIATION_SELECTOR, ""): emoji.value for emoji in ReactionEmoji
}


def _git_status(vault: Path) -> dict[str, Any] | None:
    if not (vault / ".git").exists():
        return None
    try:
        branch = subprocess.run(
            ["git", "-C", str(vault), "branch", "--show-current"],
            capture_output=True,
            check=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "-C", str(vault), "status", "--porcelain"],
                capture_output=True,
                check=True,
                text=True,
                timeout=2,
            ).stdout.strip()
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return {"root": str(vault), "branch": branch or None, "is_dirty": dirty}


def _process(pid: int) -> dict[str, Any]:
    result: dict[str, Any] = {"pid": pid, "name": None, "parent_pid": None}
    try:
        proc = Path("/proc") / str(pid)
        result["name"] = (proc / "comm").read_text().strip()
        result["parent_pid"] = int((proc / "stat").read_text().split()[3])
    except (OSError, IndexError, ValueError):
        pass
    return result


@mcp.tool
def runtime_status() -> dict[str, Any]:
    """Inspect Ariadne's current local runtime and Git workspace.

    Secrets and environment values are never returned.
    """
    vault = Path(os.environ.get("ARIADNE_VAULT", Path.cwd())).resolve()
    profile = PROFILES.get(os.environ.get("ARIADNE_PROFILE", "telegram"))
    return {
        "server": {"name": "ariadne", "version": "0.1.0"},
        "cwd": str(Path.cwd()),
        "vault": str(vault),
        "git": _git_status(vault),
        "process": {
            "current": _process(os.getpid()),
            "parent": _process(os.getppid()),
        },
        "capabilities": list(profile.enabled_tools) if profile is not None else [],
    }


def _telegram_chat() -> tuple[str, int]:
    """Return the credentials for the one private chat Ariadne speaks in."""
    try:
        return os.environ["TELEGRAM_BOT_TOKEN"], int(
            os.environ["TELEGRAM_ALLOWED_USER_ID"]
        )
    except (KeyError, ValueError) as error:
        raise ToolError("Telegram is not reachable from this runtime.") from error


async def _send_chunks(
    bot: Bot,
    chat_id: int,
    chunks: list[str],
    parse_mode: ParseMode | None,
    reply_to: ReplyParameters | None,
) -> list[int]:
    sent: list[int] = []
    for chunk in chunks:
        message = await bot.send_message(
            chat_id=chat_id,
            text=chunk,
            parse_mode=parse_mode,
            reply_parameters=reply_to if not sent else None,
        )
        sent.append(message.message_id)
    return sent


async def _send_rich_chunks(
    bot: Bot,
    chat_id: int,
    chunks: list[str],
    reply_to_message_id: int | None,
) -> list[int]:
    """Send complete Rich Markdown blocks through Bot API forward compatibility."""
    api = RichBotAPI(bot)
    sent: list[int] = []
    for chunk in chunks:
        message = await api.send(
            chat_id=chat_id,
            markdown=chunk,
            reply_to_message_id=(reply_to_message_id if not sent else None),
        )
        sent.append(message.message_id)
    return sent


@mcp.tool
async def send_telegram_message(
    text: str, reply_to_message_id: int | None = None
) -> list[int]:
    """Send a persistent message to the human's configured private Telegram.

    The only destination belongs to the same human and cannot be supplied or
    changed by the caller. Use this for notifications outside Telegram turns or
    to speak before a Telegram turn ends. Ariadne handles Markdown and splitting.
    """
    if not text.strip():
        raise ToolError("A message needs something to say.")

    token, chat_id = _telegram_chat()
    chunks, is_html = telegram_messages(text)
    reply_to = (
        ReplyParameters(reply_to_message_id, allow_sending_without_reply=True)
        if reply_to_message_id is not None
        else None
    )
    try:
        async with Bot(token) as bot:
            try:
                return await _send_rich_chunks(
                    bot,
                    chat_id,
                    split_rich_markdown(text),
                    reply_to_message_id,
                )
            except (BadRequest, EndPointNotFound):
                pass
            try:
                return await _send_chunks(
                    bot,
                    chat_id,
                    chunks,
                    ParseMode.HTML if is_html else None,
                    reply_to,
                )
            except BadRequest:
                if not is_html:
                    raise
                return await _send_chunks(
                    bot, chat_id, split_for_telegram(text), None, reply_to
                )
    except TelegramError as error:
        raise ToolError("Telegram could not deliver that message.") from error


@mcp.tool
async def react(message_id: int, reaction: str) -> None:
    """React to one Telegram message with a single emoji.

    Passing an empty reaction removes the one already there.
    """
    emoji = REACTIONS.get(reaction.replace(VARIATION_SELECTOR, ""))
    if reaction and emoji is None:
        raise ToolError(f"Telegram has no {reaction} reaction.")

    token, chat_id = _telegram_chat()
    try:
        async with Bot(token) as bot:
            await bot.set_message_reaction(
                chat_id=chat_id, message_id=message_id, reaction=emoji
            )
    except TelegramError as error:
        raise ToolError("Telegram could not add that reaction.") from error


@mcp.tool
async def ask_telegram_question(
    prompt: str,
    choices: list[str],
    reply_to_message_id: int | None = None,
) -> dict[str, str]:
    """Ask the human one choice question and wait for their answer.

    Telegram renders 2-6 trusted native buttons, while an ordinary typed reply
    remains valid. The tool returns only after the human answers, resuming this
    same model turn. Use it for a decision that genuinely blocks the work, not
    for rhetorical questions or information that can be inferred safely.
    """
    try:
        prompt, normalized_choices = validate_question(prompt, choices)
    except ValueError as error:
        raise ToolError(str(error)) from error
    if reply_to_message_id is not None and reply_to_message_id < 1:
        raise ToolError("A reply message id must be positive.")

    token, chat_id = _telegram_chat()
    store = TelegramQuestionStore(question_state_path())
    try:
        question = store.create(
            chat_id,
            prompt,
            normalized_choices,
            ttl_seconds=QUESTION_TIMEOUT_SECONDS,
        )
    except (ActiveQuestionError, OSError, ValueError, sqlite3.Error) as error:
        raise ToolError(str(error)) from error

    try:
        async with Bot(token) as bot:
            card = TelegramQuestionCard(bot)
            try:
                message, rich = await card.send(
                    question, reply_to_message_id=reply_to_message_id
                )
                attached = store.attach_message(
                    question.question_id, message.message_id, rich=rich
                )
                if attached is None:
                    raise RuntimeError("The Telegram question state disappeared.")
                question = attached
                if question.status != "pending":
                    await card.settle(question)

                while question.status == "pending":
                    await asyncio.sleep(0.2)
                    current = store.get(question.question_id)
                    if current is None:
                        raise RuntimeError("The Telegram question state disappeared.")
                    question = current

                if question.status == "answered" and question.answer is not None:
                    return {
                        "status": "answered",
                        "answer": question.answer,
                        "source": question.answer_source or "text",
                    }
                if question.status == "expired":
                    await card.settle(question)
                    raise ToolError("The Telegram question expired without an answer.")
                raise ToolError("The Telegram question was cancelled.")
            except asyncio.CancelledError:
                cancelled = store.cancel(question.question_id)
                if cancelled is not None:
                    with suppress(TelegramError):
                        await card.settle(cancelled)
                raise
    except ToolError:
        raise
    except TelegramError as error:
        store.cancel(question.question_id)
        raise ToolError("Telegram could not deliver the question.") from error
    except (OSError, RuntimeError, sqlite3.Error) as error:
        store.cancel(question.question_id)
        LOGGER.exception("Telegram question state failed")
        raise ToolError("The Telegram question could not be completed.") from error


@mcp.tool
async def prepare_files(paths: list[str]) -> dict[str, Any]:
    """Stage files under the user's home directory for explicit Telegram approval.

    This tool does not upload files. It sends an explicit Telegram approval card
    with Approve and Reject buttons for the exact staged batch.
    """
    try:
        approval_id, files = FileDelivery().stage(paths)
    except FileDeliveryError as error:
        raise ToolError(str(error)) from error
    try:
        token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
        await FileDelivery().request_approval(
            approval_id, files, token=token, chat_id=chat_id
        )
    except (FileDeliveryError, KeyError, ValueError) as error:
        raise ToolError(str(error)) from error
    return {
        "approval_id": approval_id,
        "expires_in_seconds": 900,
        "approval_requested": True,
        "files": [
            {
                "path": str(file.path),
                "filename": file.path.name,
                "size_bytes": file.size_bytes,
            }
            for file in files
        ],
    }


def _read_mail(operation: str, *args: object, **kwargs: object) -> dict[str, Any]:
    """Connect for one read-only operation using TOML-derived credentials."""
    try:
        username = os.environ["ARIADNE_MAIL_USERNAME"]
        password = os.environ["ARIADNE_MAIL_APP_PASSWORD"]
    except KeyError as error:
        raise ToolError("Mail reading is not configured for this turn.") from error

    client: IMAPClient | None = None
    try:
        client = IMAPClient(IMAP_HOST, port=993, ssl=True)
        client.login(username, password)
        reader = MailReader(client)
        method = cast(Callable[..., dict[str, Any]], getattr(reader, operation))
        return method(*args, **kwargs)
    except ToolError:
        raise
    except Exception as error:
        raise ToolError(f"Mail could not complete that read: {error}") from error
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                pass


@mcp.tool
def search_mail(
    query: str,
    since: str | None = None,
    before: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Search iCloud Mail using ordinary words, names, companies, or topics.

    `since` and `before`, when useful, are ISO dates (YYYY-MM-DD). Results are
    ranked locally from read-only IMAP metadata and body previews. Use a result
    id with `read_mail` or `read_mail_thread`; no local mailbox copy is kept.
    """
    return _read_mail("search", query, since=since, before=before, limit=limit)


@mcp.tool
def read_mail(id: str) -> dict[str, Any]:
    """Read one message returned by `search_mail`, without marking it read."""
    return _read_mail("read", id)


@mcp.tool
def read_mail_thread(id: str) -> dict[str, Any]:
    """Read the conversation around one search result without changing mail."""
    return _read_mail("read_thread", id)


def _calendar(operation: str, *args: object, **kwargs: object) -> dict[str, Any]:
    """Connect for one CalDAV operation using TOML-derived credentials."""
    try:
        username = os.environ["ARIADNE_ICLOUD_USERNAME"]
        password = os.environ["ARIADNE_ICLOUD_APP_PASSWORD"]
        timezone = os.environ["ARIADNE_CALENDAR_TIMEZONE"]
    except KeyError as error:
        raise ToolError("iCloud Calendar is not configured for this turn.") from error
    default_calendar = os.environ.get("ARIADNE_CALENDAR_DEFAULT")
    try:
        with ICloudCalendar(
            username,
            password,
            timezone=timezone,
            default_calendar=default_calendar,
        ) as calendar:
            method = cast(Callable[..., dict[str, Any]], getattr(calendar, operation))
            return method(*args, **kwargs)
    except CalendarConflict as error:
        raise ToolError(str(error)) from error
    except CalendarError as error:
        raise ToolError(str(error)) from error
    except AuthorizationError as error:
        raise ToolError(
            "iCloud rejected the configured Calendar credentials."
        ) from error
    except ETagMismatchError as error:
        raise ToolError(
            "The calendar event changed during the write. "
            "Read it again before retrying."
        ) from error
    except RateLimitError as error:
        raise ToolError("iCloud Calendar is temporarily rate limited.") from error
    except Exception as error:
        LOGGER.exception("iCloud Calendar operation failed: %s", operation)
        raise ToolError("iCloud Calendar could not complete that operation.") from error


@mcp.tool
def list_calendars() -> dict[str, Any]:
    """List the iCloud calendars available to the configured account.

    Returns opaque calendar ids accepted by the other calendar tools, along
    with the configured default and local timezone.
    """
    return _calendar("list_calendars")


@mcp.tool
def search_calendar(
    start: str,
    end: str,
    query: str | None = None,
    calendar_ids: list[str] | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Search iCloud events in a closed ISO date or date-time interval.

    Recurring events are expanded into occurrences. `query` matches event
    titles, descriptions, locations, and attendees. Date-only boundaries use
    the configured calendar timezone.
    """
    return _calendar(
        "search_events",
        start,
        end,
        query=query,
        calendar_ids=calendar_ids,
        limit=limit,
    )


@mcp.tool
def read_calendar_event(id: str) -> dict[str, Any]:
    """Read one iCloud event or recurrence occurrence returned by search."""
    return _calendar("read_event", id)


@mcp.tool
def calendar_free_busy(
    start: str, end: str, calendar_ids: list[str] | None = None
) -> dict[str, Any]:
    """Return merged busy intervals across selected iCloud calendars."""
    return _calendar("free_busy", start, end, calendar_ids=calendar_ids)


@mcp.tool
def create_calendar_event(
    title: str,
    start: str,
    end: str,
    calendar_id: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    timezone: str | None = None,
    recurrence: str | None = None,
    alarms_minutes_before: list[int] | None = None,
    status: CalendarStatus = "confirmed",
    busy: bool = True,
) -> dict[str, Any]:
    """Create an iCloud event, optionally sending invitations to attendees.

    Use date-only `start` and `end` for all-day events; their end is exclusive.
    Timed values are ISO date-times. `timezone` may name their IANA timezone;
    otherwise the configured default is used. `recurrence` is one RFC 5545
    RRULE value.
    """
    return _calendar(
        "create_event",
        title=title,
        start=start,
        end=end,
        calendar_id=calendar_id,
        description=description,
        location=location,
        attendees=attendees,
        timezone=timezone,
        recurrence=recurrence,
        alarms_minutes_before=alarms_minutes_before,
        status=status,
        busy=busy,
    )


@mcp.tool
def update_calendar_event(
    id: str,
    scope: UpdateScope = "occurrence",
    expected_etag: str | None = None,
    title: str | None = None,
    start: str | None = None,
    end: str | None = None,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
    timezone: str | None = None,
    recurrence: str | None = None,
    alarms_minutes_before: list[int] | None = None,
    status: CalendarStatus | None = None,
    busy: bool | None = None,
) -> dict[str, Any]:
    """Patch an iCloud event or one recurrence occurrence.

    Omitted fields are unchanged. Empty descriptions, locations, attendee
    lists, alarm lists, or recurrence strings clear those values. Use
    `scope="series"` to change an entire recurring series. Pass the last-read
    ETag when available to reject stale writes.
    """
    return _calendar(
        "update_event",
        id,
        scope=scope,
        expected_etag=expected_etag,
        title=title,
        start=start,
        end=end,
        description=description,
        location=location,
        attendees=attendees,
        timezone=timezone,
        recurrence=recurrence,
        alarms_minutes_before=alarms_minutes_before,
        status=status,
        busy=busy,
    )


@mcp.tool
def delete_calendar_event(
    id: str,
    scope: UpdateScope = "occurrence",
    expected_etag: str | None = None,
) -> dict[str, Any]:
    """Delete an iCloud event, occurrence, or recurring series immediately."""
    return _calendar("delete_event", id, scope=scope, expected_etag=expected_etag)


@mcp.tool
def respond_to_calendar_invitation(
    id: str,
    response: InvitationResponse,
    expected_etag: str | None = None,
) -> dict[str, Any]:
    """Accept, tentatively accept, or decline an iCloud calendar invitation."""
    return _calendar(
        "respond_to_invitation",
        id,
        response,
        expected_etag=expected_etag,
    )


@mcp.tool
def triage_current_mail(
    classification: str,
    importance: Importance,
    suggested_action: SuggestedAction,
    draft_reply: str | None = None,
) -> dict[str, str]:
    """Record the decision for this mail event and request a safe mailbox action.

    This capability exists only in mail turns. It can keep or flag the message,
    or move it to one of Ariadne's five configured filing folders. A draft reply
    is recorded for the user; this tool never sends email.
    """
    try:
        return record_current_mail_decision(
            classification, importance, suggested_action, draft_reply
        )
    except ValueError as error:
        raise ToolError(str(error)) from error


def main() -> None:
    """Run the local server over FastMCP's default stdio transport."""
    mcp.run()


if __name__ == "__main__":
    main()
