"""Durable, cross-process questions for a live Telegram turn.

The Codex MCP server asks the question and waits in a child process.  The main
Telegram process receives button presses or typed answers.  A small SQLite
record is the rendezvous between them, so answering resumes the same tool call
and therefore the same model turn.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from telegram import (
    Bot,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyParameters,
)
from telegram.error import BadRequest, EndPointNotFound

from .rich import RichBotAPI, RichButton

QUESTION_CALLBACK_PREFIX = "question:"
QUESTION_STATE_ENVIRONMENT = "ARIADNE_TELEGRAM_STATE"
QUESTION_TIMEOUT_SECONDS = 15 * 60
QUESTION_RETENTION_SECONDS = 7 * 24 * 60 * 60
MAX_QUESTION_CHOICES = 6
MAX_QUESTION_PROMPT_LENGTH = 3_500
MAX_QUESTION_CHOICE_LENGTH = 64

QuestionStatus = Literal["pending", "answered", "cancelled", "expired"]
AnswerSource = Literal["button", "text"]
SelectionOutcome = Literal["accepted", "already_answered", "inactive", "stale"]


def default_question_state_path() -> Path:
    """Return the private state file shared by Ariadne and its MCP process."""
    return Path("~/.local/state/ariadne/telegram.sqlite3").expanduser()


def question_state_path(
    environ: Mapping[str, str] = os.environ,
) -> Path:
    """Resolve the explicitly forwarded state path or the process default."""
    configured = environ.get(QUESTION_STATE_ENVIRONMENT)
    return (
        Path(configured).expanduser() if configured else default_question_state_path()
    )


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
class TelegramQuestion:
    """One choice question and its durable lifecycle state."""

    question_id: str
    chat_id: int
    prompt: str
    choices: tuple[str, ...]
    status: QuestionStatus
    expires_at: float
    message_id: int | None = None
    rich: bool = True
    answer: str | None = None
    answer_source: AnswerSource | None = None

    def callback_data(self, choice_index: int) -> str:
        """Return compact callback data that stays below Telegram's 64-byte cap."""
        return f"{QUESTION_CALLBACK_PREFIX}{self.question_id}:{choice_index}"


@dataclass(frozen=True, slots=True)
class QuestionSelection:
    """Result of atomically applying one callback selection."""

    outcome: SelectionOutcome
    question: TelegramQuestion | None = None


class ActiveQuestionError(RuntimeError):
    """Raised when a turn tries to display two unresolved questions at once."""


class TelegramQuestionStore:
    """Cross-process SQLite rendezvous for one private Telegram chat."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._initialized = False

    def initialize(self) -> None:
        """Create the private database and schema when first used."""
        if self._initialized:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        with self._connect_unchecked() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS telegram_questions (
                    question_id TEXT PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    choices TEXT NOT NULL,
                    message_id INTEGER,
                    rich INTEGER NOT NULL DEFAULT 1,
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
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_telegram_question
                ON telegram_questions(chat_id) WHERE status = 'pending'
                """
            )
            database.execute(
                """
                DELETE FROM telegram_questions
                WHERE status != 'pending' AND updated_at < ?
                """,
                (time.time() - QUESTION_RETENTION_SECONDS,),
            )
        self._initialized = True

    def create(
        self,
        chat_id: int,
        prompt: str,
        choices: Sequence[str],
        *,
        ttl_seconds: float = QUESTION_TIMEOUT_SECONDS,
    ) -> TelegramQuestion:
        """Create the one active question for a chat."""
        prompt, normalized_choices = validate_question(prompt, choices)
        if ttl_seconds <= 0:
            raise ValueError("A question timeout must be positive.")
        now = time.time()
        question = TelegramQuestion(
            question_id=secrets.token_urlsafe(12),
            chat_id=chat_id,
            prompt=prompt,
            choices=normalized_choices,
            status="pending",
            expires_at=now + ttl_seconds,
        )
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            self._expire_due(database, now)
            existing = database.execute(
                """
                SELECT 1 FROM telegram_questions
                WHERE chat_id = ? AND status = 'pending'
                """,
                (chat_id,),
            ).fetchone()
            if existing is not None:
                raise ActiveQuestionError(
                    "A Telegram question is already waiting for an answer."
                )
            database.execute(
                """
                INSERT INTO telegram_questions (
                    question_id, chat_id, prompt, choices, rich, status,
                    created_at, expires_at, updated_at
                ) VALUES (?, ?, ?, ?, 1, 'pending', ?, ?, ?)
                """,
                (
                    question.question_id,
                    chat_id,
                    prompt,
                    json.dumps(normalized_choices, ensure_ascii=False),
                    now,
                    question.expires_at,
                    now,
                ),
            )
        return question

    def attach_message(
        self, question_id: str, message_id: int, *, rich: bool
    ) -> TelegramQuestion | None:
        """Record the Telegram message backing a question card."""
        with self._connect() as database:
            database.execute(
                """
                UPDATE telegram_questions
                SET message_id = ?, rich = ?, updated_at = ?
                WHERE question_id = ?
                """,
                (message_id, int(rich), time.time(), question_id),
            )
            row = self._select(database, question_id)
        return _question(row) if row is not None else None

    def get(self, question_id: str) -> TelegramQuestion | None:
        """Read a question, expiring it atomically when its deadline passed."""
        now = time.time()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            self._expire_due(database, now, question_id=question_id)
            row = self._select(database, question_id)
        return _question(row) if row is not None else None

    def pending(self, chat_id: int) -> TelegramQuestion | None:
        """Return the chat's active, unexpired question."""
        now = time.time()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            self._expire_due(database, now)
            row = database.execute(
                """
                SELECT * FROM telegram_questions
                WHERE chat_id = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
        return _question(row) if row is not None else None

    def answer_text(self, chat_id: int, answer: str) -> TelegramQuestion | None:
        """Atomically use ordinary chat text as the active question's answer."""
        normalized = answer.strip()
        if not normalized:
            return None
        now = time.time()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            self._expire_due(database, now)
            row = database.execute(
                """
                SELECT * FROM telegram_questions
                WHERE chat_id = ? AND status = 'pending'
                ORDER BY created_at DESC LIMIT 1
                """,
                (chat_id,),
            ).fetchone()
            if row is None:
                return None
            database.execute(
                """
                UPDATE telegram_questions
                SET status = 'answered', answer = ?, answer_source = 'text',
                    updated_at = ?
                WHERE question_id = ? AND status = 'pending'
                """,
                (normalized, now, row["question_id"]),
            )
            answered = self._select(database, str(row["question_id"]))
        return _question(answered) if answered is not None else None

    def answer_choice(
        self,
        question_id: str,
        *,
        chat_id: int,
        message_id: int,
        choice_index: int,
    ) -> QuestionSelection:
        """Validate and atomically apply a trusted callback selection."""
        now = time.time()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            self._expire_due(database, now, question_id=question_id)
            row = self._select(database, question_id)
            if row is None:
                return QuestionSelection("stale")
            question = _question(row)
            if question.chat_id != chat_id or (
                question.message_id is not None and question.message_id != message_id
            ):
                return QuestionSelection("stale")
            if question.status == "answered":
                return QuestionSelection("already_answered", question)
            if question.status != "pending":
                return QuestionSelection("inactive", question)
            if not 0 <= choice_index < len(question.choices):
                return QuestionSelection("stale", question)

            database.execute(
                """
                UPDATE telegram_questions
                SET status = 'answered', answer = ?, answer_source = 'button',
                    updated_at = ?
                WHERE question_id = ? AND status = 'pending'
                """,
                (question.choices[choice_index], now, question_id),
            )
            answered = self._select(database, question_id)
        return QuestionSelection(
            "accepted", _question(answered) if answered is not None else question
        )

    def cancel(self, question_id: str) -> TelegramQuestion | None:
        """Cancel one question if it is still waiting."""
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                """
                UPDATE telegram_questions
                SET status = 'cancelled', updated_at = ?
                WHERE question_id = ? AND status = 'pending'
                """,
                (time.time(), question_id),
            )
            row = self._select(database, question_id)
        return _question(row) if row is not None else None

    def cancel_pending(self, chat_id: int) -> tuple[TelegramQuestion, ...]:
        """Cancel every orphaned pending question for a chat and return them."""
        now = time.time()
        with self._connect() as database:
            database.execute("BEGIN IMMEDIATE")
            rows = database.execute(
                """
                SELECT * FROM telegram_questions
                WHERE chat_id = ? AND status = 'pending'
                """,
                (chat_id,),
            ).fetchall()
            database.execute(
                """
                UPDATE telegram_questions
                SET status = 'cancelled', updated_at = ?
                WHERE chat_id = ? AND status = 'pending'
                """,
                (now, chat_id),
            )
        return tuple(
            TelegramQuestion(
                question_id=question.question_id,
                chat_id=question.chat_id,
                prompt=question.prompt,
                choices=question.choices,
                status="cancelled",
                expires_at=question.expires_at,
                message_id=question.message_id,
                rich=question.rich,
            )
            for question in map(_question, rows)
        )

    def _connect(self) -> sqlite3.Connection:
        self.initialize()
        return self._connect_unchecked()

    def _connect_unchecked(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        return database

    @staticmethod
    def _select(database: sqlite3.Connection, question_id: str) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            database.execute(
                "SELECT * FROM telegram_questions WHERE question_id = ?",
                (question_id,),
            ).fetchone(),
        )

    @staticmethod
    def _expire_due(
        database: sqlite3.Connection,
        now: float,
        *,
        question_id: str | None = None,
    ) -> None:
        if question_id is None:
            database.execute(
                """
                UPDATE telegram_questions
                SET status = 'expired', updated_at = ?
                WHERE status = 'pending' AND expires_at <= ?
                """,
                (now, now),
            )
            return
        database.execute(
            """
            UPDATE telegram_questions
            SET status = 'expired', updated_at = ?
            WHERE question_id = ? AND status = 'pending' AND expires_at <= ?
            """,
            (now, question_id, now),
        )


class TelegramQuestionCard:
    """Send and settle a choice card using Rich Messages with classic fallback."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._rich = RichBotAPI(bot)

    async def send(
        self,
        question: TelegramQuestion,
        *,
        reply_to_message_id: int | None = None,
    ) -> tuple[Message, bool]:
        """Send a rich question, falling back only when Telegram rejects it."""
        try:
            message = await self._rich.send(
                chat_id=question.chat_id,
                markdown=question.prompt,
                reply_to_message_id=reply_to_message_id,
                buttons=_pending_buttons(question),
                buttons_per_row=2,
            )
            return message, True
        except (BadRequest, EndPointNotFound):
            message = await self._bot.send_message(
                chat_id=question.chat_id,
                text=question.prompt,
                reply_markup=_classic_keyboard(question),
                reply_parameters=(
                    ReplyParameters(
                        reply_to_message_id, allow_sending_without_reply=True
                    )
                    if reply_to_message_id is not None
                    else None
                ),
            )
            return message, False

    async def settle(self, question: TelegramQuestion) -> None:
        """Disable controls and make the terminal question state visible."""
        if question.message_id is None or question.status == "pending":
            return
        markdown, plain = _settled_content(question)
        if question.rich:
            try:
                await self._rich.edit_by_id(
                    chat_id=question.chat_id,
                    message_id=question.message_id,
                    markdown=markdown,
                    buttons=_settled_buttons(question),
                    buttons_per_row=2,
                )
                return
            except BadRequest:
                pass
        await self._bot.edit_message_text(
            chat_id=question.chat_id,
            message_id=question.message_id,
            text=plain,
            reply_markup=None,
        )


def parse_question_callback(data: str) -> tuple[str, int] | None:
    """Parse callback data without trusting either component."""
    if not data.startswith(QUESTION_CALLBACK_PREFIX):
        return None
    payload = data.removeprefix(QUESTION_CALLBACK_PREFIX)
    question_id, separator, raw_index = payload.rpartition(":")
    if not separator or not question_id:
        return None
    try:
        index = int(raw_index)
    except ValueError:
        return None
    return question_id, index


def _pending_buttons(question: TelegramQuestion) -> tuple[RichButton, ...]:
    return tuple(
        RichButton(
            choice,
            "callback_data",
            question.callback_data(index),
            style="primary",
        )
        for index, choice in enumerate(question.choices)
    )


def _settled_buttons(question: TelegramQuestion) -> tuple[RichButton, ...]:
    if question.status == "answered" and question.answer_source == "button":
        return tuple(
            RichButton(
                f"✓ {choice}" if choice == question.answer else choice,
                "disabled",
                style="success" if choice == question.answer else None,
            )
            for choice in question.choices
        )
    if question.status == "answered":
        return (
            *(RichButton(choice, "disabled") for choice in question.choices),
            RichButton("✓ Answered in chat", "disabled", style="success"),
        )
    label = "Expired" if question.status == "expired" else "Cancelled"
    return (RichButton(label, "disabled", style="danger"),)


def _classic_keyboard(question: TelegramQuestion) -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(choice, callback_data=question.callback_data(index))
        for index, choice in enumerate(question.choices)
    ]
    return InlineKeyboardMarkup(
        [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
    )


def _settled_content(question: TelegramQuestion) -> tuple[str, str]:
    labels = {
        "answered": "Answered.",
        "cancelled": "Question cancelled.",
        "expired": "Question expired.",
    }
    label = labels[question.status]
    return f"{question.prompt}\n\n_{label}_", f"{question.prompt}\n\n{label}"


def _question(row: sqlite3.Row) -> TelegramQuestion:
    choices = json.loads(str(row["choices"]))
    return TelegramQuestion(
        question_id=str(row["question_id"]),
        chat_id=int(row["chat_id"]),
        prompt=str(row["prompt"]),
        choices=tuple(str(choice) for choice in choices),
        message_id=(int(row["message_id"]) if row["message_id"] is not None else None),
        rich=bool(row["rich"]),
        status=cast(QuestionStatus, str(row["status"])),
        answer=str(row["answer"]) if row["answer"] is not None else None,
        answer_source=(
            cast(AnswerSource, str(row["answer_source"]))
            if row["answer_source"] is not None
            else None
        ),
        expires_at=float(row["expires_at"]),
    )
