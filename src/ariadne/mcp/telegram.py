"""Telegram MCP capabilities."""

import asyncio
import logging
import os
import sqlite3
from contextlib import suppress
from datetime import datetime
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from telegram import Bot
from telegram.error import TelegramError

from ..telegram.file_delivery import FileDelivery, FileDeliveryError
from ..telegram.history import (
    TelegramHistoryMessage,
    TelegramMessageSource,
    TelegramMessageStore,
    TelegramSpeaker,
    telegram_message_time,
)
from ..telegram.outbound import (
    TelegramDeliveredWithoutHistoryError,
    send_rich_text,
)
from ..telegram.questions import (
    QUESTION_TIMEOUT_SECONDS,
    ActiveQuestionError,
    TelegramQuestionCard,
    TelegramQuestionStore,
    question_state_path,
    validate_question,
)

LOGGER = logging.getLogger(__name__)


def _timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ToolError(f"{name} must be a valid ISO 8601 timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ToolError(f"{name} must include a timezone offset.")
    return parsed


def _telegram_chat() -> tuple[str, int]:
    """Return the credentials for the one private chat Ariadne speaks in."""
    try:
        return os.environ["TELEGRAM_BOT_TOKEN"], int(
            os.environ["TELEGRAM_ALLOWED_USER_ID"]
        )
    except (KeyError, ValueError) as error:
        raise ToolError("Telegram is not reachable from this runtime.") from error


def _message_source() -> TelegramMessageSource:
    try:
        profile = os.environ["ARIADNE_PROFILE"]
    except KeyError as error:
        raise ToolError("Telegram message source is unavailable.") from error
    if profile == "mail":
        return "mail"
    if profile.startswith("revisit-"):
        return "wakeup"
    raise ToolError("Proactive Telegram messages require a background turn.")


async def send_telegram_message(text: str) -> list[int]:
    """Send a persistent message to the human in your private Telegram chat.

    The destination is fixed and cannot be changed. Use this when Ariadne's
    activation says your native response will not reach the human. In an
    ordinary Telegram conversation, speak through commentary and final instead.
    """
    if not text.strip():
        raise ToolError("A message needs something to say.")

    token, chat_id = _telegram_chat()
    try:
        return await send_rich_text(
            token,
            chat_id,
            text,
            history=TelegramMessageStore(question_state_path()),
            source=_message_source(),
        )
    except TelegramDeliveredWithoutHistoryError as error:
        delivered = ", ".join(str(identifier) for identifier in error.message_ids)
        raise ToolError(
            f"Telegram delivered message ID(s) {delivered}, but permanent history "
            "could not be updated. Do not resend the message."
        ) from error
    except (OSError, sqlite3.Error, TelegramError, ValueError) as error:
        raise ToolError("Telegram could not deliver that message.") from error


def read_recent_telegram_messages(
    since: str,
    before: str | None = None,
    speakers: list[TelegramSpeaker] | None = None,
    sources: list[TelegramMessageSource] | None = None,
    query: str | None = None,
    limit: int = 50,
) -> dict[str, object]:
    """Read recent messages from your private Telegram conversation.

    `since` and `before` are ISO 8601 timestamps with timezone offsets. Optional
    `query` is a literal case-insensitive substring match, not fuzzy or semantic
    search. Results retain the newest matches when limited, then return them in
    chronological order. Sources say what woke Iris before she sent a message:
    `telegram`, `mail`, or `wakeup`.

    A scheduled wake-up activation supplies exact `since` and `before` values
    for its reconciliation window. Use those values unchanged before deciding
    or acting; do not derive a narrower window from the due time. Missing human
    messages mean only that no reply was observed here; they do not establish
    whether a message was read, ignored, or understood. Older conversation may
    predate the available history.
    """
    try:
        chat_id = int(os.environ["TELEGRAM_ALLOWED_USER_ID"])
    except (KeyError, ValueError) as error:
        raise ToolError("Telegram history is not configured for this turn.") from error
    try:
        page = TelegramMessageStore(question_state_path()).read(
            chat_id,
            since=_timestamp(since, "since"),
            before=_timestamp(before, "before") if before is not None else None,
            speakers=speakers or (),
            sources=sources or (),
            query=query,
            limit=limit,
        )
    except (OSError, ValueError, sqlite3.Error) as error:
        raise ToolError(str(error)) from error
    return {
        "messages": [message.public_payload() for message in page.messages],
        "total": page.total,
        "truncated": page.truncated,
        "earliest_available_at": (
            page.earliest_available_at.isoformat()
            if page.earliest_available_at is not None
            else None
        ),
    }


async def ask_telegram_question(
    prompt: str,
    choices: list[str],
) -> dict[str, str]:
    """Ask the human one choice question and wait for their answer.

    Telegram renders 2-6 native buttons, while an ordinary typed reply remains
    valid. Once the human answers, you continue with their answer. Use this for
    a decision that genuinely blocks the work, not for rhetorical questions or
    information that can be inferred safely.
    """
    try:
        prompt, normalized_choices = validate_question(prompt, choices)
    except ValueError as error:
        raise ToolError(str(error)) from error
    token, chat_id = _telegram_chat()
    store = TelegramQuestionStore(question_state_path())
    history = TelegramMessageStore(question_state_path())
    try:
        history.initialize()
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
                message = await card.send(question)
                try:
                    history.record(
                        TelegramHistoryMessage(
                            chat_id=chat_id,
                            message_id=message.message_id,
                            sent_at=telegram_message_time(message),
                            speaker="iris",
                            source="telegram",
                            content_type="text",
                            text=prompt,
                        )
                    )
                except (OSError, sqlite3.Error, ValueError) as error:
                    cancelled = store.cancel(question.question_id)
                    if cancelled is not None:
                        with suppress(TelegramError):
                            await card.settle(cancelled)
                    raise ToolError(
                        f"Telegram delivered question message ID {message.message_id}, "
                        "but permanent history could not be updated. Do not ask it "
                        "again automatically."
                    ) from error
                attached = store.attach_message(
                    question.question_id, message.message_id
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

                if (
                    question.status == "answered"
                    and question.answer is not None
                    and question.answer_source is not None
                ):
                    return {
                        "status": "answered",
                        "answer": question.answer,
                        "source": question.answer_source,
                    }
                if question.status == "expired":
                    await card.settle(question)
                    raise ToolError("The Telegram question expired without an answer.")
                if question.status == "cancelled":
                    raise ToolError("The Telegram question was cancelled.")
                raise RuntimeError("The Telegram question reached an invalid state.")
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


async def request_telegram_file_delivery(paths: list[str]) -> dict[str, Any]:
    """Stage files under the user's home directory for explicit Telegram approval.

    This tool does not upload files. It sends an explicit Telegram approval card
    with Approve and Reject buttons for the exact staged batch.
    """
    try:
        approval_id, files = FileDelivery().stage(paths)
    except FileDeliveryError as error:
        raise ToolError(str(error)) from error
    token, chat_id = _telegram_chat()
    try:
        await FileDelivery().request_approval(
            approval_id, files, token=token, chat_id=chat_id
        )
    except FileDeliveryError as error:
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


def register_tools(server: FastMCP) -> None:
    """Register Telegram tools."""
    server.tool(send_telegram_message)
    server.tool(
        read_recent_telegram_messages,
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    server.tool(ask_telegram_question)
    server.tool(request_telegram_file_delivery)
