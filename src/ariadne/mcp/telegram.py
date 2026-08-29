"""Telegram MCP capabilities."""

import asyncio
import logging
import os
import sqlite3
from contextlib import suppress
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from telegram import Bot
from telegram.error import TelegramError

from ..telegram.file_delivery import FileDelivery, FileDeliveryError
from ..telegram.outbound import send_rich_text
from ..telegram.questions import (
    QUESTION_TIMEOUT_SECONDS,
    ActiveQuestionError,
    TelegramQuestionCard,
    TelegramQuestionStore,
    question_state_path,
    validate_question,
)

LOGGER = logging.getLogger(__name__)


def _telegram_chat() -> tuple[str, int]:
    """Return the credentials for the one private chat Ariadne speaks in."""
    try:
        return os.environ["TELEGRAM_BOT_TOKEN"], int(
            os.environ["TELEGRAM_ALLOWED_USER_ID"]
        )
    except (KeyError, ValueError) as error:
        raise ToolError("Telegram is not reachable from this runtime.") from error


async def send_telegram_message(text: str) -> list[int]:
    """Send a persistent message to the human's configured private Telegram.

    The only destination belongs to the same human and cannot be supplied or
    changed by the caller. This capability exists only in proactive background
    turns; Telegram-triggered turns speak through native Codex response phases.
    """
    if not text.strip():
        raise ToolError("A message needs something to say.")

    token, chat_id = _telegram_chat()
    try:
        return await send_rich_text(token, chat_id, text)
    except TelegramError as error:
        raise ToolError("Telegram could not deliver that message.") from error


async def ask_telegram_question(
    prompt: str,
    choices: list[str],
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
                message = await card.send(question)
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


async def prepare_files(paths: list[str]) -> dict[str, Any]:
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
    server.tool(ask_telegram_question)
    server.tool(prepare_files)
