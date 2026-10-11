"""Capabilities for Iris's one continuous conversation with the owner."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..conversation.database import state_path
from ..conversation.log import ConversationLog
from ..conversation.models import MessageOrigin, Speaker
from ..conversation.questions import (
    QUESTION_TIMEOUT_SECONDS,
    ActiveQuestionError,
    QuestionStore,
    validate_question,
)
from ..conversation.shares import SharedFiles, ShareError, validate_share

QUESTION_POLL_SECONDS = 0.2


def _timestamp(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ToolError(f"{name} must be a valid ISO 8601 timestamp.") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ToolError(f"{name} must include a timezone offset.")
    return parsed


def read_recent_conversation(
    since: str,
    before: str | None = None,
    speakers: list[Speaker] | None = None,
    origins: list[MessageOrigin] | None = None,
    query: str | None = None,
    limit: int = 50,
) -> dict[str, object]:
    """Read recent messages from your private conversation with the human.

    `since` and `before` are ISO 8601 timestamps with timezone offsets. Optional
    `query` is a literal case-insensitive substring match, not fuzzy or semantic
    search. Results keep the newest matches when limited, then return them in
    chronological order. `origin` is `direct` for replies within a conversation
    the human started and `proactive` for messages you sent on your own.

    A scheduled wake-up activation supplies exact `since` and `before` values
    for its reconciliation window. Use those values unchanged before deciding
    or acting. Missing human messages mean only that no reply was observed;
    they do not establish whether a message was read, ignored, or understood.
    """
    try:
        page = ConversationLog(state_path()).read(
            since=_timestamp(since, "since"),
            before=_timestamp(before, "before") if before is not None else None,
            speakers=speakers or (),
            origins=origins or (),
            query=query,
            limit=limit,
        )
    except (OSError, ValueError, sqlite3.Error) as error:
        raise ToolError(str(error)) from error
    return {
        "messages": [message.history_payload() for message in page.messages],
        "total": page.total,
        "truncated": page.truncated,
        "earliest_available_at": (
            page.earliest_available_at.isoformat()
            if page.earliest_available_at is not None
            else None
        ),
    }


async def ask_question(prompt: str, choices: list[str]) -> dict[str, str]:
    """Ask the human one choice question and wait for their answer.

    The app shows 2-6 buttons, and an ordinary typed reply also counts. Once the
    human answers, you continue with their answer. Use this for a decision that
    genuinely blocks the work, not for rhetorical questions or information that
    can be inferred safely.
    """
    try:
        prompt, normalized_choices = validate_question(prompt, choices)
    except ValueError as error:
        raise ToolError(str(error)) from error
    store = QuestionStore(state_path())
    try:
        question = store.create(
            prompt, normalized_choices, ttl_seconds=QUESTION_TIMEOUT_SECONDS
        )
    except (ActiveQuestionError, OSError, ValueError, sqlite3.Error) as error:
        raise ToolError(str(error)) from error
    try:
        while question.status == "pending":
            await asyncio.sleep(QUESTION_POLL_SECONDS)
            current = store.get(question.question_id)
            if current is None:
                raise ToolError("The question could not be completed.")
            question = current
    except asyncio.CancelledError:
        store.cancel(question.question_id)
        raise
    except (OSError, sqlite3.Error) as error:
        store.cancel(question.question_id)
        raise ToolError("The question could not be completed.") from error
    if question.status == "answered" and question.answer is not None:
        return {
            "status": "answered",
            "answer": question.answer,
            "source": question.answer_source or "text",
        }
    if question.status == "expired":
        raise ToolError("The question expired without an answer.")
    raise ToolError("The question was cancelled.")


def share_files(paths: list[str], caption: str | None = None) -> dict[str, Any]:
    """Share files under the human's home directory into the conversation.

    The files are copied as they are now and appear in the conversation as one
    message with an optional short caption. Use this for a prepared document,
    image, or export the human should keep; refer to files in prose otherwise.
    """
    try:
        resolved = validate_share(paths, caption)
        share = SharedFiles(state_path()).stage(resolved, caption)
    except (ShareError, OSError, sqlite3.Error) as error:
        raise ToolError(str(error)) from error
    return {
        "status": "shared",
        "files": [{"filename": path.name, "path": str(path)} for path in share.paths],
    }


def register_tools(server: FastMCP) -> None:
    """Register the conversation capabilities."""
    server.tool(
        read_recent_conversation,
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    server.tool(ask_question)
    server.tool(share_files)
