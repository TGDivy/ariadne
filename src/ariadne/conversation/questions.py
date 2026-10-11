"""Durable, cross-process questions for a live conversation turn.

The MCP tool creates a question and waits in Codex's child process. The
conversation core presents it, receives the owner's answer, and settles it. A
small SQLite record is the rendezvous, so answering resumes the same tool call
and therefore the same model turn.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from .database import connect, prepare

QUESTION_TIMEOUT_SECONDS = 15 * 60
QUESTION_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_QUESTION_CHOICES = 6
MAX_QUESTION_PROMPT_LENGTH = 3_500
MAX_QUESTION_CHOICE_LENGTH = 64

QuestionStatus = Literal["pending", "answered", "cancelled", "expired"]
AnswerSource = Literal["button", "text"]
SelectionOutcome = Literal["accepted", "already_answered", "inactive", "stale"]


def validate_question(
    prompt: str, choices: Sequence[str]
) -> tuple[str, tuple[str, ...]]:
    """Validate and normalize the model-provided question content."""
    normalized_prompt = prompt.strip()
    normalized_choices = tuple(choice.strip() for choice in choices)
    if not normalized_prompt:
        raise ValueError("A question needs a prompt.")
    if len(normalized_prompt) > MAX_QUESTION_PROMPT_LENGTH:
        raise ValueError(
            f"A question prompt can be at most {MAX_QUESTION_PROMPT_LENGTH} characters."
        )
    if not 2 <= len(normalized_choices) <= MAX_QUESTION_CHOICES:
        raise ValueError(
            f"A question needs between 2 and {MAX_QUESTION_CHOICES} choices."
        )
    if any(not choice for choice in normalized_choices):
        raise ValueError("Question choices cannot be empty.")
    if any(len(choice) > MAX_QUESTION_CHOICE_LENGTH for choice in normalized_choices):
        raise ValueError(
            f"Question choices can be at most {MAX_QUESTION_CHOICE_LENGTH} characters."
        )
    if len(set(normalized_choices)) != len(normalized_choices):
        raise ValueError("Question choices must be distinct.")
    return normalized_prompt, normalized_choices


@dataclass(frozen=True, slots=True)
class Question:
    """One choice question and its durable lifecycle state."""

    question_id: str
    prompt: str
    choices: tuple[str, ...]
    status: QuestionStatus
    expires_at: float
    message_id: int | None = None
    answer: str | None = None
    answer_source: AnswerSource | None = None


@dataclass(frozen=True, slots=True)
class QuestionSelection:
    """Result of atomically applying one owner's choice."""

    outcome: SelectionOutcome
    question: Question | None = None


class ActiveQuestionError(RuntimeError):
    """Raised when a turn tries to ask two unresolved questions at once."""


class QuestionStore:
    """Cross-process SQLite rendezvous for the one private conversation."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        prepare(self.path)
        with connect(self.path) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_questions (
                    question_id TEXT PRIMARY KEY,
                    prompt TEXT NOT NULL,
                    choices TEXT NOT NULL,
                    message_id INTEGER,
                    status TEXT NOT NULL
                        CHECK(status IN ('pending','answered','cancelled','expired')),
                    answer TEXT,
                    answer_source TEXT
                        CHECK(
                            answer_source IN ('button','text')
                            OR answer_source IS NULL
                        ),
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            database.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_conversation_question
                ON conversation_questions(status) WHERE status = 'pending'
                """
            )
            database.execute(
                """
                DELETE FROM conversation_questions
                WHERE status != 'pending' AND updated_at < ?
                """,
                (time.time() - QUESTION_RETENTION_SECONDS,),
            )
        self._initialized = True

    def create(
        self,
        prompt: str,
        choices: Sequence[str],
        *,
        ttl_seconds: float = QUESTION_TIMEOUT_SECONDS,
    ) -> Question:
        """Create the conversation's one active question."""
        prompt, normalized_choices = validate_question(prompt, choices)
        if ttl_seconds <= 0:
            raise ValueError("A question timeout must be positive.")
        now = time.time()
        question = Question(
            question_id=secrets.token_urlsafe(12),
            prompt=prompt,
            choices=normalized_choices,
            status="pending",
            expires_at=now + ttl_seconds,
        )
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            _expire_due(database, now)
            existing = database.execute(
                "SELECT 1 FROM conversation_questions WHERE status = 'pending'"
            ).fetchone()
            if existing is not None:
                raise ActiveQuestionError(
                    "A question is already waiting for an answer."
                )
            database.execute(
                """
                INSERT INTO conversation_questions (
                    question_id, prompt, choices, status,
                    created_at, expires_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    question.question_id,
                    prompt,
                    json.dumps(normalized_choices, ensure_ascii=False),
                    now,
                    question.expires_at,
                    now,
                ),
            )
        return question

    def attach_message(self, question_id: str, message_id: int) -> Question | None:
        """Record the conversation message that presented a question."""
        with self._connect() as database:
            database.execute(
                """
                UPDATE conversation_questions
                SET message_id = ?, updated_at = ?
                WHERE question_id = ?
                """,
                (message_id, time.time(), question_id),
            )
            row = _select(database, question_id)
        return _question(row) if row is not None else None

    def get(self, question_id: str) -> Question | None:
        """Read a question, expiring it atomically when its deadline passed."""
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            _expire_due(database, time.time(), question_id=question_id)
            row = _select(database, question_id)
        return _question(row) if row is not None else None

    def pending(self) -> Question | None:
        """Return the active, unexpired question, presented or not."""
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            _expire_due(database, time.time())
            row = database.execute(
                """
                SELECT * FROM conversation_questions
                WHERE status = 'pending' ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
        return _question(row) if row is not None else None

    def answer_text(self, answer: str) -> Question | None:
        """Atomically use ordinary text as the presented question's answer."""
        normalized = answer.strip()
        if not normalized:
            return None
        now = time.time()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            _expire_due(database, now)
            row = database.execute(
                """
                SELECT * FROM conversation_questions
                WHERE status = 'pending' AND message_id IS NOT NULL
                ORDER BY created_at DESC LIMIT 1
                """
            ).fetchone()
            if row is None:
                return None
            database.execute(
                """
                UPDATE conversation_questions
                SET status = 'answered', answer = ?, answer_source = 'text',
                    updated_at = ?
                WHERE question_id = ? AND status = 'pending'
                """,
                (normalized, now, row["question_id"]),
            )
            answered = _select(database, str(row["question_id"]))
        return _question(answered) if answered is not None else None

    def answer_choice(self, question_id: str, choice_index: int) -> QuestionSelection:
        """Validate and atomically apply one owner's button choice."""
        now = time.time()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            _expire_due(database, now, question_id=question_id)
            row = _select(database, question_id)
            if row is None:
                return QuestionSelection("stale")
            question = _question(row)
            if question.status == "answered":
                return QuestionSelection("already_answered", question)
            if question.status != "pending" or question.message_id is None:
                return QuestionSelection("inactive", question)
            if not 0 <= choice_index < len(question.choices):
                return QuestionSelection("stale", question)
            database.execute(
                """
                UPDATE conversation_questions
                SET status = 'answered', answer = ?, answer_source = 'button',
                    updated_at = ?
                WHERE question_id = ? AND status = 'pending'
                """,
                (question.choices[choice_index], now, question_id),
            )
            answered = _select(database, question_id)
        return QuestionSelection(
            "accepted", _question(answered) if answered is not None else question
        )

    def cancel(self, question_id: str) -> Question | None:
        """Cancel one question if it is still waiting."""
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """
                UPDATE conversation_questions
                SET status = 'cancelled', updated_at = ?
                WHERE question_id = ? AND status = 'pending'
                """,
                (time.time(), question_id),
            )
            row = _select(database, question_id)
        return _question(row) if row is not None else None

    def cancel_pending(self) -> tuple[Question, ...]:
        """Cancel every pending question and return what was cancelled."""
        now = time.time()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            rows = database.execute(
                "SELECT * FROM conversation_questions WHERE status = 'pending'"
            ).fetchall()
            database.execute(
                """
                UPDATE conversation_questions
                SET status = 'cancelled', updated_at = ?
                WHERE status = 'pending'
                """,
                (now,),
            )
        return tuple(
            Question(
                question_id=question.question_id,
                prompt=question.prompt,
                choices=question.choices,
                status="cancelled",
                expires_at=question.expires_at,
                message_id=question.message_id,
            )
            for question in map(_question, rows)
        )

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        return connect(self.path)


def _select(database: sqlite3.Connection, question_id: str) -> sqlite3.Row | None:
    return cast(
        sqlite3.Row | None,
        database.execute(
            "SELECT * FROM conversation_questions WHERE question_id = ?",
            (question_id,),
        ).fetchone(),
    )


def _expire_due(
    database: sqlite3.Connection,
    now: float,
    *,
    question_id: str | None = None,
) -> None:
    if question_id is None:
        database.execute(
            """
            UPDATE conversation_questions
            SET status = 'expired', updated_at = ?
            WHERE status = 'pending' AND expires_at <= ?
            """,
            (now, now),
        )
        return
    database.execute(
        """
        UPDATE conversation_questions
        SET status = 'expired', updated_at = ?
        WHERE question_id = ? AND status = 'pending' AND expires_at <= ?
        """,
        (now, question_id, now),
    )


def _question(row: sqlite3.Row) -> Question:
    return Question(
        question_id=str(row["question_id"]),
        prompt=str(row["prompt"]),
        choices=tuple(str(choice) for choice in json.loads(str(row["choices"]))),
        message_id=int(row["message_id"]) if row["message_id"] is not None else None,
        status=cast(QuestionStatus, str(row["status"])),
        answer=str(row["answer"]) if row["answer"] is not None else None,
        answer_source=(
            cast(AnswerSource, str(row["answer_source"]))
            if row["answer_source"] is not None
            else None
        ),
        expires_at=float(row["expires_at"]),
    )
