"""iCloud IMAP routing, durable mail jobs, and ordinary Iris mail turns."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterable, Mapping
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from itertools import combinations
from pathlib import Path
from typing import Any, Literal, cast

import yaml  # type: ignore[import-untyped]
from imapclient import IMAPClient  # type: ignore[import-untyped]
from imapclient.exceptions import IMAPClientError  # type: ignore[import-untyped]
from pydantic import ValidationError

from ..codex import CodexConversation, CodexTurnSettings
from ..codex.resolver import resolve_profile
from ..config import MailSettings
from ..profile import MAIL_PROFILE
from ..prompts.activations import build_mail_turn_prompt
from ..prompts.mail_evidence import render_mail_evidence
from ..telemetry import Telemetry
from .models import (
    BackfillSummary,
    Importance,
    JobStatus,
    MailJob,
    MailMetadata,
    MailRoute,
    MailRoutes,
    RestoreSummary,
    RouteLintReport,
    RouteOverlap,
    RuleLint,
    SuggestedAction,
)

LOGGER = logging.getLogger(__name__)

MAILBOX = "INBOX"
IMAP_HOST = "imap.mail.me.com"
HEADER_QUERY = (
    b"BODY.PEEK[HEADER.FIELDS (MESSAGE-ID FROM TO CC SUBJECT DATE "
    b"LIST-UNSUBSCRIBE PRECEDENCE AUTO-SUBMITTED)]"
)
FULL_QUERY = b"BODY.PEEK[]"
IMPORTANT_SUBJECT_WORDS = (
    "action required",
    "action needed",
    "urgent",
    "important",
    "deadline",
    "interview",
    "security alert",
    "verify",
    "verification",
    "password",
    "sign-in",
    "login",
    "appointment",
    "reservation",
    "booking confirmation",
)
_UNEXPECTED_IDLE_RESPONSE = "Unexpected IDLE response:"


def _enter_idle(client: IMAPClient) -> None:
    """Enter IMAP IDLE while preserving unsolicited mailbox updates.

    IMAP servers may legally send untagged updates before the continuation for
    the IDLE command. IMAPClient currently treats the first such update as an
    error even though it has already parsed and retained it. Continue reading
    until the expected continuation arrives, but preserve genuine command
    failures.
    """
    try:
        client.idle()
        return
    except IMAPClientError as error:
        if not str(error).startswith(_UNEXPECTED_IDLE_RESPONSE):
            raise

        imap = getattr(client, "_imap", None)
        idle_tag = getattr(client, "_idle_tag", None)
        if imap is None or idle_tag is None:
            raise

        while True:
            if imap.tagged_commands.get(idle_tag) is not None:
                raise error
            if imap._get_response() is None:
                LOGGER.debug("Accepted unsolicited IMAP update while entering IDLE")
                return


def load_routes(path: Path) -> MailRoutes:
    """Load and validate one external YAML routing file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return MailRoutes.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise ValueError(f"Invalid mail routes at {path}: {error}") from error


def _header_addresses(*values: str) -> tuple[str, ...]:
    return tuple(
        address.casefold() for _name, address in getaddresses(values) if address.strip()
    )


def parse_metadata(raw: bytes) -> MailMetadata:
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    return MailMetadata(
        message_id=str(message.get("Message-ID", "")).strip(),
        sender=_header_addresses(str(message.get("From", ""))),
        recipients=_header_addresses(
            str(message.get("To", "")), str(message.get("Cc", ""))
        ),
        subject=str(message.get("Subject", "")),
        date=str(message.get("Date", "")),
        has_list_unsubscribe=message.get("List-Unsubscribe") is not None,
        precedence=str(message.get("Precedence", "")),
        auto_submitted=str(message.get("Auto-Submitted", "")),
    )


def cheap_triage(message: MailMetadata) -> Literal["routine", "important", "inspect"]:
    """Classify unmatched headers without spending a model turn."""
    subject = message.subject.casefold()
    if any(word in subject for word in IMPORTANT_SUBJECT_WORDS):
        return "important"
    if message.has_list_unsubscribe:
        return "routine"
    if message.precedence.casefold() in {"bulk", "list", "junk"}:
        return "routine"
    if message.auto_submitted and message.auto_submitted.casefold() != "no":
        return "routine"
    return "inspect"


class MailState:
    """Small cross-process SQLite job and decision store."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS mail_jobs (
                    job_id TEXT PRIMARY KEY,
                    mailbox TEXT NOT NULL,
                    uidvalidity INTEGER NOT NULL,
                    uid INTEGER NOT NULL,
                    message_id TEXT,
                    status TEXT NOT NULL
                        CHECK(status IN ('pending','running','done','failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    route_id TEXT,
                    classification TEXT,
                    importance TEXT,
                    suggested_action TEXT,
                    draft_reply TEXT,
                    action TEXT,
                    destination TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    UNIQUE(mailbox, uidvalidity, uid)
                )
                """
            )
            database.execute(
                """
                CREATE TABLE IF NOT EXISTS mail_checkpoints (
                    mailbox TEXT PRIMARY KEY,
                    uidvalidity INTEGER NOT NULL,
                    last_seen_uid INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            database.execute(
                "UPDATE mail_jobs SET status = 'pending' WHERE status = 'running'"
            )

    def _connect(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, timeout=10)
        database.row_factory = sqlite3.Row
        return database

    @staticmethod
    def job_id(mailbox: str, uidvalidity: int, uid: int) -> str:
        return f"{mailbox}:{uidvalidity}:{uid}"

    def discover(self, mailbox: str, uidvalidity: int, uids: Iterable[int]) -> None:
        now = time.time()
        with self._connect() as database:
            database.executemany(
                """
                INSERT OR IGNORE INTO mail_jobs
                    (job_id, mailbox, uidvalidity, uid, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    (
                        self.job_id(mailbox, uidvalidity, uid),
                        mailbox,
                        uidvalidity,
                        uid,
                        now,
                        now,
                    )
                    for uid in uids
                ),
            )
            database.execute(
                """
                UPDATE mail_jobs SET status = 'failed',
                    error = 'Mailbox UIDVALIDITY changed', updated_at = ?
                WHERE mailbox = ? AND uidvalidity != ?
                    AND status IN ('pending', 'running', 'failed')
                """,
                (now, mailbox, uidvalidity),
            )

    def catch_up(
        self,
        mailbox: str,
        uidvalidity: int,
        uids: Iterable[int],
        *,
        initial_baseline_uid: int | None = None,
    ) -> tuple[int, ...]:
        """Baseline a new mailbox, then durably enqueue UIDs seen afterward."""
        current_uids = tuple(sorted(uids))
        newest_uid = current_uids[-1] if current_uids else 0
        now = time.time()
        with self._connect() as database:
            checkpoint = database.execute(
                "SELECT * FROM mail_checkpoints WHERE mailbox = ?", (mailbox,)
            ).fetchone()
            checkpoint_changed = (
                checkpoint is None or checkpoint["uidvalidity"] != uidvalidity
            )
            if checkpoint_changed:
                baseline_uid = (
                    initial_baseline_uid
                    if initial_baseline_uid is not None
                    else newest_uid
                )
                database.execute(
                    """
                    INSERT INTO mail_checkpoints
                        (mailbox, uidvalidity, last_seen_uid, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(mailbox) DO UPDATE SET
                        uidvalidity = excluded.uidvalidity,
                        last_seen_uid = excluded.last_seen_uid,
                        updated_at = excluded.updated_at
                    """,
                    (mailbox, uidvalidity, baseline_uid, now),
                )
                if checkpoint is not None:
                    database.execute(
                        """
                        UPDATE mail_jobs SET status = 'failed',
                            error = 'Mailbox UIDVALIDITY changed', updated_at = ?
                        WHERE mailbox = ? AND uidvalidity != ?
                            AND status IN ('pending', 'running', 'failed')
                        """,
                        (now, mailbox, uidvalidity),
                    )
                else:
                    database.execute(
                        """
                        UPDATE mail_jobs SET status = 'done',
                            error = 'Superseded by initial mailbox baseline',
                            updated_at = ?
                        WHERE mailbox = ?
                            AND status IN ('pending', 'running', 'failed')
                        """,
                        (now, mailbox),
                    )
                last_seen_uid = baseline_uid
            else:
                last_seen_uid = int(checkpoint["last_seen_uid"])
            new_uids = tuple(uid for uid in current_uids if uid > last_seen_uid)
            database.executemany(
                """
                INSERT OR IGNORE INTO mail_jobs
                    (job_id, mailbox, uidvalidity, uid, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    (
                        self.job_id(mailbox, uidvalidity, uid),
                        mailbox,
                        uidvalidity,
                        uid,
                        now,
                        now,
                    )
                    for uid in new_uids
                ),
            )
            database.execute(
                """
                UPDATE mail_checkpoints SET last_seen_uid = ?, updated_at = ?
                WHERE mailbox = ?
                """,
                (max(last_seen_uid, newest_uid), now, mailbox),
            )
        return new_uids

    def retryable(self, mailbox: str, uidvalidity: int) -> tuple[MailJob, ...]:
        with self._connect() as database:
            rows = database.execute(
                """
                SELECT * FROM mail_jobs
                WHERE mailbox = ? AND uidvalidity = ?
                    AND status IN ('pending', 'failed')
                ORDER BY uid
                """,
                (mailbox, uidvalidity),
            ).fetchall()
        return tuple(_job(row) for row in rows)

    def get(self, job_id: str) -> MailJob | None:
        with self._connect() as database:
            row = database.execute(
                "SELECT * FROM mail_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return _job(row) if row is not None else None

    def start(self, job_id: str) -> None:
        with self._connect() as database:
            database.execute(
                """
                UPDATE mail_jobs SET status = 'running', attempts = attempts + 1,
                    error = NULL, updated_at = ? WHERE job_id = ?
                """,
                (time.time(), job_id),
            )

    def identify(self, job_id: str, message_id: str) -> bool:
        """Record Message-ID and report a completed delivery duplicate."""
        if not message_id:
            return False
        with self._connect() as database:
            database.execute(
                "UPDATE mail_jobs SET message_id = ?, updated_at = ? WHERE job_id = ?",
                (message_id, time.time(), job_id),
            )
            duplicate = database.execute(
                """
                SELECT 1 FROM mail_jobs
                WHERE job_id != ? AND message_id = ? AND status = 'done'
                LIMIT 1
                """,
                (job_id, message_id),
            ).fetchone()
        return duplicate is not None

    def set_runtime_decision(
        self,
        job_id: str,
        *,
        route_id: str | None,
        classification: str,
        importance: Importance,
        suggested_action: SuggestedAction,
        action: str | None = None,
        destination: str | None = None,
    ) -> None:
        with self._connect() as database:
            database.execute(
                """
                UPDATE mail_jobs SET route_id = ?, classification = ?,
                    importance = ?, suggested_action = ?, action = ?, destination = ?,
                    updated_at = ? WHERE job_id = ?
                """,
                (
                    route_id,
                    classification,
                    importance,
                    suggested_action,
                    action,
                    destination,
                    time.time(),
                    job_id,
                ),
            )

    def set_pending_route_action(
        self,
        job_id: str,
        *,
        route_id: str,
        classification: str,
        action: str,
        destination: str,
    ) -> None:
        with self._connect() as database:
            database.execute(
                """
                UPDATE mail_jobs SET route_id = ?, classification = ?,
                    action = ?, destination = ?, updated_at = ? WHERE job_id = ?
                """,
                (
                    route_id,
                    classification,
                    action,
                    destination,
                    time.time(),
                    job_id,
                ),
            )

    def record_model_decision(
        self,
        job_id: str,
        classification: str,
        importance: Importance,
        suggested_action: SuggestedAction,
        draft_reply: str | None,
    ) -> None:
        if not classification.strip():
            raise ValueError("classification must not be empty")
        if draft_reply is not None:
            draft_reply = draft_reply.strip() or None
        with self._connect() as database:
            row = database.execute(
                "SELECT status FROM mail_jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None or row["status"] != "running":
                raise ValueError("The current mail job is not running.")
            database.execute(
                """
                UPDATE mail_jobs SET classification = ?, importance = ?,
                    suggested_action = ?, draft_reply = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    classification.strip(),
                    importance,
                    suggested_action,
                    draft_reply,
                    time.time(),
                    job_id,
                ),
            )

    def set_action(self, job_id: str, action: str, destination: str | None) -> None:
        with self._connect() as database:
            database.execute(
                """
                UPDATE mail_jobs SET action = ?, destination = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (action, destination, time.time(), job_id),
            )

    def finish(self, job_id: str) -> None:
        self._status(job_id, "done", None)

    def fail(self, job_id: str, error: Exception) -> None:
        self._status(job_id, "failed", str(error)[:1000])

    def _status(self, job_id: str, status: JobStatus, error: str | None) -> None:
        with self._connect() as database:
            database.execute(
                """
                UPDATE mail_jobs SET status = ?, error = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (status, error, time.time(), job_id),
            )


def _job(row: sqlite3.Row) -> MailJob:
    return MailJob(
        job_id=cast(str, row["job_id"]),
        mailbox=cast(str, row["mailbox"]),
        uidvalidity=cast(int, row["uidvalidity"]),
        uid=cast(int, row["uid"]),
        message_id=cast(str | None, row["message_id"]),
        status=cast(JobStatus, row["status"]),
        attempts=cast(int, row["attempts"]),
        route_id=cast(str | None, row["route_id"]),
        action=cast(str | None, row["action"]),
        destination=cast(str | None, row["destination"]),
        classification=cast(str | None, row["classification"]),
        importance=cast(str | None, row["importance"]),
        suggested_action=cast(str | None, row["suggested_action"]),
        draft_reply=cast(str | None, row["draft_reply"]),
    )


def record_current_mail_decision(
    classification: str,
    importance: Importance,
    suggested_action: SuggestedAction,
    draft_reply: str | None = None,
) -> dict[str, str]:
    """Record the decision for the one job authorized in this MCP process."""
    try:
        job_id = os.environ["ARIADNE_MAIL_JOB_ID"]
        state_path = Path(os.environ["ARIADNE_MAIL_STATE"])
    except KeyError as error:
        raise ValueError("Mail authority is unavailable in this turn.") from error
    state = MailState(state_path)
    state.record_model_decision(
        job_id, classification, importance, suggested_action, draft_reply
    )
    return {"status": "recorded", "job_id": job_id}


def _compact(text: str, limit: int) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


def _response_value(
    response: Mapping[Any, Mapping[Any, Any]], uid: int, prefix: bytes
) -> bytes | None:
    item = response.get(uid)
    if item is None:
        return None
    for key, value in item.items():
        key_bytes = key if isinstance(key, bytes) else str(key).encode()
        if key_bytes.upper().startswith(prefix) and isinstance(value, bytes):
            return value
    return None


def ensure_folders(client: IMAPClient, routes: MailRoutes) -> None:
    """Create the configured filing folders without touching any messages."""
    listed = client.list_folders()
    existing = {
        name.decode() if isinstance(name, bytes) else str(name)
        for _flags, _delimiter, name in listed
    }
    for folder in routes.folders.values():
        if folder not in existing:
            client.create_folder(folder)


def move_messages(
    client: IMAPClient, messages: Iterable[int], destination: str
) -> None:
    """Move UIDs atomically, or emulate it with UIDPLUS and targeted expunge.

    iCloud Mail does not advertise RFC 6851 MOVE. Its UIDPLUS support lets us
    copy, mark only the source UIDs deleted, and expunge only those same UIDs.
    A mailbox-wide EXPUNGE is deliberately never used.
    """
    uids = tuple(messages)
    if not uids:
        return
    if client.has_capability("MOVE"):
        client.move(uids, destination)
        return
    if not client.has_capability("UIDPLUS"):
        raise RuntimeError(
            "The IMAP server supports neither MOVE nor safe UIDPLUS moves."
        )
    client.copy(uids, destination)
    client.delete_messages(uids, silent=True)
    client.uid_expunge(uids)


def backfill_inbox(
    client: IMAPClient,
    routes: MailRoutes,
    *,
    apply: bool = False,
    batch_size: int = 100,
    progress: Callable[[int, int], None] | None = None,
) -> BackfillSummary:
    """Apply only deterministic move rules to mail already in INBOX.

    This operator path never creates a Codex conversation, calls Iris, or applies
    an `iris` rule. Without `apply`, it is a read-only preview.
    """
    client.select_folder(MAILBOX, readonly=not apply)
    uids = tuple(int(uid) for uid in client.search(["ALL"]))
    if progress is not None:
        progress(0, len(uids))
    scanned = move_matches = moved = iris_skipped = unmatched = 0
    for start in range(0, len(uids), batch_size):
        batch = uids[start : start + batch_size]
        response = client.fetch(batch, [HEADER_QUERY])
        moves_by_destination: dict[str, list[int]] = {}
        for uid in batch:
            raw = _response_value(response, uid, b"BODY[")
            if raw is not None:
                scanned += 1
                route = routes.match(parse_metadata(raw))
                if route is None:
                    unmatched += 1
                elif route.action != "move":
                    iris_skipped += 1
                else:
                    move_matches += 1
                    if apply:
                        destination = routes.folders[route.classification]
                        moves_by_destination.setdefault(destination, []).append(uid)
        for destination, matched_uids in moves_by_destination.items():
            move_messages(client, matched_uids, destination)
            moved += len(matched_uids)
        if progress is not None:
            progress(start + len(batch), len(uids))
    return BackfillSummary(
        scanned=scanned,
        move_matches=move_matches,
        moved=moved,
        iris_skipped=iris_skipped,
        unmatched=unmatched,
    )


def lint_mail_routes(
    client: IMAPClient,
    routes: MailRoutes,
    *,
    mailbox: str = MAILBOX,
    batch_size: int = 100,
    sample_limit: int = 5,
    progress: Callable[[int, int], None] | None = None,
) -> RouteLintReport:
    """Measure ordered route behavior against one mailbox, without mutations."""
    if batch_size < 1 or sample_limit < 1:
        raise ValueError("Route lint batch and sample limits must be positive.")
    client.select_folder(mailbox, readonly=True)
    uids = tuple(int(uid) for uid in client.search(["ALL"]))
    if progress is not None:
        progress(0, len(uids))

    indexes = {route.id: index for index, route in enumerate(routes.rules)}
    matches = [0] * len(routes.rules)
    selected = [0] * len(routes.rules)
    samples: list[list[str]] = [[] for _route in routes.rules]
    overlaps: dict[tuple[int, int], int] = {}
    scanned = unmatched = 0

    for start in range(0, len(uids), batch_size):
        batch = uids[start : start + batch_size]
        response = client.fetch(batch, [HEADER_QUERY])
        for uid in batch:
            raw = _response_value(response, uid, b"BODY[")
            if raw is None:
                continue
            scanned += 1
            metadata = parse_metadata(raw)
            matched_indexes = tuple(
                indexes[route.id] for route in routes.matches(metadata)
            )
            if not matched_indexes:
                unmatched += 1
                continue
            selected[matched_indexes[0]] += 1
            subject = _compact(metadata.subject, 160) or "(no subject)"
            for index in matched_indexes:
                matches[index] += 1
                if len(samples[index]) < sample_limit:
                    samples[index].append(subject)
            for pair in combinations(matched_indexes, 2):
                overlaps[pair] = overlaps.get(pair, 0) + 1
        if progress is not None:
            progress(start + len(batch), len(uids))

    rule_results = tuple(
        RuleLint(
            route_id=route.id,
            action=route.action,
            matches=matches[index],
            selected=selected[index],
            shadowed=matches[index] - selected[index],
            sample_subjects=tuple(samples[index]),
        )
        for index, route in enumerate(routes.rules)
    )
    overlap_results = tuple(
        RouteOverlap(
            earlier_route_id=routes.rules[earlier].id,
            later_route_id=routes.rules[later].id,
            matches=count,
        )
        for (earlier, later), count in sorted(overlaps.items())
    )
    return RouteLintReport(
        scanned=scanned,
        unmatched=unmatched,
        rules=rule_results,
        overlaps=overlap_results,
    )


def restore_folder_to_inbox(
    client: IMAPClient,
    source: str,
    *,
    apply: bool = False,
    batch_size: int = 100,
    progress: Callable[[int, int], None] | None = None,
) -> RestoreSummary:
    """Preview or move every message from one folder back to INBOX."""
    if source.casefold() == MAILBOX.casefold():
        raise ValueError("The restore source cannot be INBOX.")

    client.select_folder(source, readonly=not apply)
    uids = tuple(int(uid) for uid in client.search(["ALL"]))
    if progress is not None:
        progress(0, len(uids))

    moved = 0
    for start in range(0, len(uids), batch_size):
        batch = uids[start : start + batch_size]
        if apply:
            move_messages(client, batch, MAILBOX)
            moved += len(batch)
        if progress is not None:
            progress(start + len(batch), len(uids))
    return RestoreSummary(found=len(uids), moved=moved)


class MailProcessor:
    """Reconcile one selected mailbox and process a snapshot sequentially."""

    def __init__(
        self,
        client: IMAPClient,
        routes: MailRoutes,
        state: MailState,
        conversation_factory: Callable[[str], CodexConversation],
        telemetry: Telemetry | None = None,
    ) -> None:
        self.client = client
        self.routes = routes
        self.state = state
        self.conversation_factory = conversation_factory
        self.telemetry = telemetry or Telemetry()
        self.uidvalidity = 0

    async def reconcile(self) -> None:
        selected = await asyncio.to_thread(self.client.select_folder, MAILBOX)
        value = selected.get(b"UIDVALIDITY", selected.get("UIDVALIDITY"))
        if value is None:
            raise RuntimeError("IMAP did not return UIDVALIDITY.")
        self.uidvalidity = int(value)
        uidnext = selected.get(b"UIDNEXT", selected.get("UIDNEXT"))
        initial_baseline_uid = max(int(uidnext) - 1, 0) if uidnext is not None else None
        uids = await asyncio.to_thread(self.client.search, ["ALL"])
        new_uids = self.state.catch_up(
            MAILBOX,
            self.uidvalidity,
            (int(uid) for uid in uids),
            initial_baseline_uid=initial_baseline_uid,
        )
        if new_uids:
            LOGGER.info(
                "Mail discovered mailbox=%s uidvalidity=%d count=%d first_uid=%d "
                "last_uid=%d",
                MAILBOX,
                self.uidvalidity,
                len(new_uids),
                new_uids[0],
                new_uids[-1],
            )

    async def process_available(self) -> None:
        jobs = self.state.retryable(MAILBOX, self.uidvalidity)
        if jobs:
            LOGGER.info(
                "Mail queue ready mailbox=%s jobs=%d retries=%d",
                MAILBOX,
                len(jobs),
                sum(job.attempts > 0 for job in jobs),
            )
        for job in jobs:
            await self._process(job)

    async def _fetch(self, uid: int, query: bytes) -> bytes | None:
        response = await asyncio.to_thread(self.client.fetch, [uid], [query])
        return _response_value(response, uid, b"BODY[")

    async def _process(self, job: MailJob) -> None:
        started_at = time.monotonic()
        self.state.start(job.job_id)
        try:
            current = self.state.get(job.job_id)
            assert current is not None
            LOGGER.info(
                "Mail job started job_id=%s uid=%d attempt=%d",
                job.job_id,
                job.uid,
                current.attempts,
            )
            pending_iris_then_move = current.action == "iris_then_move"
            if current.action is not None and not pending_iris_then_move:
                if current.action == "keep":
                    self.state.finish(job.job_id)
                    return
                if await self._fetch(job.uid, HEADER_QUERY) is None:
                    # The recorded move/flag already happened, or the user moved
                    # the message. Either way there is no remaining authority to
                    # apply in this mailbox namespace.
                    self.state.finish(job.job_id)
                    return
                await self._apply(current)
                return
            if current.suggested_action is not None and not pending_iris_then_move:
                if current.suggested_action != "keep_in_inbox":
                    if await self._fetch(job.uid, HEADER_QUERY) is None:
                        self.state.finish(job.job_id)
                        return
                await self._prepare_and_apply(current)
                return

            raw_headers = await self._fetch(job.uid, HEADER_QUERY)
            if raw_headers is None:
                self.state.finish(job.job_id)
                return
            metadata = parse_metadata(raw_headers)
            if self.state.identify(job.job_id, metadata.message_id):
                self.state.finish(job.job_id)
                return

            route = self.routes.match(metadata)
            if (
                not pending_iris_then_move
                and route is not None
                and route.action == "move"
            ):
                destination = self.routes.folders[route.classification]
                self.state.set_runtime_decision(
                    job.job_id,
                    route_id=route.id,
                    classification=route.classification,
                    importance="routine",
                    suggested_action=cast(
                        SuggestedAction, f"move_to_{route.classification}"
                    ),
                    action="move",
                    destination=destination,
                )
                await self._apply(cast(MailJob, self.state.get(job.job_id)))
                return

            if pending_iris_then_move and current.destination is None:
                raise RuntimeError(
                    "A pending iris_then_move action needs a destination."
                )
            move_after_iris = current.destination if pending_iris_then_move else None
            if (
                not pending_iris_then_move
                and route is not None
                and route.action == "iris_then_move"
            ):
                move_after_iris = self.routes.folders[route.classification]
                self.state.set_pending_route_action(
                    job.job_id,
                    route_id=route.id,
                    classification=route.classification,
                    action="iris_then_move",
                    destination=move_after_iris,
                )

            if route is None and not pending_iris_then_move:
                defaults = self.routes.defaults
                if (
                    defaults.unmatched_action == "cheap_triage"
                    and defaults.unmatched_keep_in_inbox
                    and cheap_triage(metadata) == "routine"
                ):
                    self.state.set_runtime_decision(
                        job.job_id,
                        route_id=None,
                        classification="routine",
                        importance="routine",
                        suggested_action="keep_in_inbox",
                        action="keep",
                    )
                    self.state.finish(job.job_id)
                    return

            raw = await self._fetch(job.uid, FULL_QUERY)
            if raw is None:
                self.state.finish(job.job_id)
                return
            await self._mail_turn(
                job,
                metadata,
                raw,
                route=None if pending_iris_then_move else route,
                move_after_iris=move_after_iris,
            )
            decided = self.state.get(job.job_id)
            if decided is None or decided.suggested_action is None:
                raise RuntimeError("Iris finished without recording a mail decision.")
            LOGGER.info(
                "Mail decision recorded job_id=%s route=%s classification=%s "
                "importance=%s suggested_action=%s",
                job.job_id,
                decided.route_id,
                decided.classification,
                decided.importance,
                decided.suggested_action,
            )
            if (
                move_after_iris is not None
                and decided.suggested_action == "keep_in_inbox"
            ):
                self.state.set_action(job.job_id, "move", move_after_iris)
                decided = self.state.get(job.job_id)
                assert decided is not None
                await self._apply(decided)
            else:
                await self._prepare_and_apply(decided)
        except Exception as error:
            LOGGER.exception("Mail job %s failed", job.job_id)
            self.state.fail(job.job_id, error)
        finally:
            finished = self.state.get(job.job_id)
            LOGGER.info(
                "Mail job finished job_id=%s status=%s action=%s duration=%.2fs",
                job.job_id,
                finished.status if finished is not None else "missing",
                finished.action if finished is not None else None,
                time.monotonic() - started_at,
            )
            if finished is not None and finished.status in {"done", "failed"}:
                self.telemetry.background_job(
                    source="mail",
                    status="success" if finished.status == "done" else "failure",
                )

    async def _mail_turn(
        self,
        job: MailJob,
        metadata: MailMetadata,
        raw: bytes,
        *,
        route: MailRoute | None,
        move_after_iris: str | None,
    ) -> None:
        conversation = self.conversation_factory(job.job_id)
        prompt = build_mail_turn_prompt(
            render_mail_evidence(raw, metadata),
            route_id=route.id if route is not None else None,
            route_classification=(route.classification if route is not None else None),
            move_after_iris=move_after_iris,
            unmatched_keep_in_inbox=self.routes.defaults.unmatched_keep_in_inbox,
        )
        started_at = time.monotonic()
        LOGGER.info(
            "Mail Codex turn started job_id=%s route=%s move_after=%s",
            job.job_id,
            route.id if route is not None else None,
            move_after_iris,
        )
        try:
            async for _event in conversation.stream_turn(prompt):
                pass
            LOGGER.info(
                "Mail Codex turn completed job_id=%s duration=%.2fs",
                job.job_id,
                time.monotonic() - started_at,
            )
        finally:
            await conversation.close()

    async def _prepare_and_apply(self, job: MailJob) -> None:
        suggested = cast(SuggestedAction, job.suggested_action)
        if suggested == "keep_in_inbox":
            action, destination = "keep", None
        elif suggested == "flag":
            action, destination = "flag", None
        else:
            key = suggested.removeprefix("move_to_")
            action, destination = "move", self.routes.folders[key]
        self.state.set_action(job.job_id, action, destination)
        decided = self.state.get(job.job_id)
        assert decided is not None
        await self._apply(decided)

    async def _apply(self, job: MailJob) -> None:
        if job.action == "move":
            if job.destination is None:
                raise RuntimeError("A move needs a destination folder.")
            await asyncio.to_thread(
                move_messages, self.client, [job.uid], job.destination
            )
        elif job.action == "flag":
            await asyncio.to_thread(self.client.add_flags, [job.uid], [b"\\Flagged"])
        elif job.action != "keep":
            raise RuntimeError(f"Unsupported mailbox action: {job.action}")
        self.state.finish(job.job_id)
        LOGGER.info(
            "Mail action applied job_id=%s action=%s destination=%s",
            job.job_id,
            job.action,
            job.destination,
        )


ClientFactory = Callable[[], IMAPClient]


class MailLoop:
    """Reconnect, catch up, drain durable jobs, then use IDLE as a wake-up."""

    def __init__(
        self,
        settings: MailSettings,
        vault: Path,
        turn_settings: CodexTurnSettings,
        *,
        human: str,
        personality: Path | None = None,
        mcp_environment: Mapping[str, str] | None = None,
        client_factory: ClientFactory | None = None,
        telemetry: Telemetry | None = None,
    ) -> None:
        self.settings = settings
        self.vault = vault
        self.turn_settings = turn_settings
        self.human = human
        self.personality = personality
        self.mcp_environment = dict(mcp_environment or {})
        self.telemetry = telemetry or Telemetry()
        self.routes = load_routes(settings.routes)
        self.state = MailState(settings.state)
        self.state.initialize()
        self._stop = asyncio.Event()
        self._client_factory = client_factory or (
            lambda: IMAPClient(IMAP_HOST, port=993, ssl=True)
        )

    def stop(self) -> None:
        self._stop.set()

    def _conversation(self, job_id: str) -> CodexConversation:
        return CodexConversation(
            resolve_profile(
                MAIL_PROFILE,
                vault=self.vault,
                settings=self.turn_settings,
                human=self.human,
                personality=self.personality,
                knowledge_root=self.vault,
                mcp_environment={
                    **self.mcp_environment,
                    "ARIADNE_MAIL_JOB_ID": job_id,
                    "ARIADNE_MAIL_STATE": str(self.settings.state),
                },
            ),
            telemetry=self.telemetry,
        )

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self._session()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("Mail connection failed; reconnecting")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5)
            except TimeoutError:
                pass

    async def _session(self) -> None:
        client = await asyncio.to_thread(self._client_factory)
        try:
            await asyncio.to_thread(
                client.login,
                self.settings.username,
                self.settings.app_password.get_secret_value(),
            )
            await self._ensure_folders(client)
            LOGGER.info("iCloud Mail connected; monitored folders are ready")
            processor = MailProcessor(
                client,
                self.routes,
                self.state,
                self._conversation,
                self.telemetry,
            )
            while not self._stop.is_set():
                await processor.reconcile()
                await processor.process_available()
                await asyncio.to_thread(_enter_idle, client)
                try:
                    await asyncio.to_thread(client.idle_check, timeout=30)
                finally:
                    await asyncio.to_thread(client.idle_done)
        finally:
            try:
                await asyncio.to_thread(client.logout)
            except Exception:
                pass

    async def _ensure_folders(self, client: IMAPClient) -> None:
        await asyncio.to_thread(ensure_folders, client, self.routes)
