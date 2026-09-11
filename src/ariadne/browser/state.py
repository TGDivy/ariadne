"""Private durable profile leases, confirmations, and action journal."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ..redaction import redact_sensitive_text
from .models import (
    ActionOutcome,
    BrowserApprovalError,
    BrowserBusyError,
    BrowserLease,
    BrowserLeaseError,
    BrowserProfile,
    BrowserStateError,
    BrowserUncertainError,
    ConsequentialAction,
    JournalEntry,
    TaskState,
)

_PROFILE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MAX_JOURNAL_DETAIL = 500


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Browser timestamps must include a timezone offset.")
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


def state_digest(payload: object) -> str:
    """Return a stable, non-reversible binding for material page state."""
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class BrowserState:
    """SQLite-backed coordination shared by the daemon and trusted clients."""

    def __init__(
        self,
        path: Path,
        *,
        lease_seconds: int = 900,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if lease_seconds < 30:
            raise ValueError("Browser task leases must last at least 30 seconds.")
        self.path = path
        self.lease_seconds = lease_seconds
        self._clock = clock

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS browser_profiles (
                    name TEXT PRIMARY KEY,
                    user_data_path TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    last_opened_at TEXT
                );
                CREATE TABLE IF NOT EXISTS browser_tasks (
                    task_id TEXT PRIMARY KEY,
                    profile_name TEXT NOT NULL REFERENCES browser_profiles(name),
                    lease_token TEXT NOT NULL,
                    state TEXT NOT NULL CHECK (
                        state IN ('active', 'takeover', 'uncertain', 'released')
                    ),
                    lease_expires_at TEXT NOT NULL,
                    page_revision INTEGER NOT NULL DEFAULT 0,
                    uncertain_operation_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_browser_profile_owner
                    ON browser_tasks(profile_name)
                    WHERE state IN ('active', 'takeover', 'uncertain');
                CREATE TABLE IF NOT EXISTS browser_confirmations (
                    approval_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES browser_tasks(task_id),
                    page_revision INTEGER NOT NULL,
                    state_digest TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    used_at TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_actions (
                    operation_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES browser_tasks(task_id),
                    approval_id TEXT NOT NULL
                        REFERENCES browser_confirmations(approval_id),
                    state_digest TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'succeeded', 'rejected', 'uncertain')
                    ),
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS browser_journal (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT,
                    occurred_at TEXT NOT NULL,
                    origin TEXT,
                    operation TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    detail TEXT,
                    artifact_path TEXT
                );
                CREATE INDEX IF NOT EXISTS browser_journal_time
                    ON browser_journal(occurred_at DESC);
                """
            )
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def validate_profile_name(name: str) -> str:
        normalized = name.strip()
        if not _PROFILE_NAME.fullmatch(normalized):
            raise BrowserStateError(
                "Profile names must start with a lowercase letter and contain only "
                "lowercase letters, digits, '-' or '_'."
            )
        return normalized

    @staticmethod
    def validate_identifier(value: str, label: str) -> str:
        normalized = value.strip()
        if not _IDENTIFIER.fullmatch(normalized):
            raise BrowserStateError(f"{label} is not a valid bounded identifier.")
        return normalized

    def create_profile(self, name: str, user_data_path: Path) -> BrowserProfile:
        name = self.validate_profile_name(name)
        resolved = user_data_path.expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved.chmod(0o700)
        now = self._clock()
        try:
            with self._connect(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO browser_profiles(
                        name, user_data_path, created_at, last_opened_at
                    ) VALUES (?, ?, ?, NULL)
                    """,
                    (name, str(resolved), _timestamp(now)),
                )
        except sqlite3.IntegrityError as error:
            raise BrowserStateError(
                f"Browser profile '{name}' already exists or reuses another path."
            ) from error
        return BrowserProfile(name, resolved, now, None)

    def profiles(self) -> tuple[BrowserProfile, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM browser_profiles ORDER BY name"
            ).fetchall()
        return tuple(self._profile(row) for row in rows)

    def profile(self, name: str) -> BrowserProfile:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM browser_profiles WHERE name = ?", (name,)
            ).fetchone()
        if row is None:
            raise BrowserStateError(f"Browser profile '{name}' does not exist.")
        return self._profile(row)

    def acquire(
        self,
        profile_name: str,
        task_id: str,
        *,
        lease_token: str | None = None,
    ) -> BrowserLease:
        profile_name = self.validate_profile_name(profile_name)
        task_id = self.validate_identifier(task_id, "task_id")
        now = self._clock()
        expires = now + timedelta(seconds=self.lease_seconds)
        with self._connect(immediate=True) as connection:
            profile = connection.execute(
                "SELECT name FROM browser_profiles WHERE name = ?", (profile_name,)
            ).fetchone()
            if profile is None:
                raise BrowserStateError(
                    f"Browser profile '{profile_name}' does not exist."
                )
            connection.execute(
                """
                UPDATE browser_tasks
                SET state = 'released', updated_at = ?
                WHERE profile_name = ?
                  AND state IN ('active', 'takeover')
                  AND lease_expires_at <= ?
                """,
                (_timestamp(now), profile_name, _timestamp(now)),
            )
            existing = connection.execute(
                "SELECT * FROM browser_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            owner = connection.execute(
                """
                SELECT * FROM browser_tasks
                WHERE profile_name = ?
                  AND state IN ('active', 'takeover', 'uncertain')
                """,
                (profile_name,),
            ).fetchone()
            if existing is not None and existing["profile_name"] != profile_name:
                raise BrowserStateError("A task cannot move between browser profiles.")
            if existing is not None and existing["state"] == TaskState.UNCERTAIN:
                raise BrowserUncertainError(
                    "The preceding consequential browser action has an uncertain "
                    "outcome; inspect the site before doing anything else."
                )
            if owner is not None and owner["task_id"] != task_id:
                raise BrowserBusyError(
                    f"Browser profile '{profile_name}' is busy with another task "
                    f"until {owner['lease_expires_at']}."
                )
            if existing is not None and existing["state"] in {
                TaskState.ACTIVE,
                TaskState.TAKEOVER,
            }:
                if not lease_token or not secrets.compare_digest(
                    lease_token, str(existing["lease_token"])
                ):
                    raise BrowserLeaseError(
                        "Attaching to an existing browser task requires its "
                        "lease token."
                    )
                connection.execute(
                    """
                    UPDATE browser_tasks
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE task_id = ?
                    """,
                    (_timestamp(expires), _timestamp(now), task_id),
                )
            else:
                token = secrets.token_urlsafe(32)
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO browser_tasks(
                            task_id, profile_name, lease_token, state,
                            lease_expires_at, page_revision, created_at, updated_at
                        ) VALUES (?, ?, ?, 'active', ?, 0, ?, ?)
                        """,
                        (
                            task_id,
                            profile_name,
                            token,
                            _timestamp(expires),
                            _timestamp(now),
                            _timestamp(now),
                        ),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE browser_tasks
                        SET lease_token = ?, state = 'active', lease_expires_at = ?,
                            uncertain_operation_id = NULL, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (token, _timestamp(expires), _timestamp(now), task_id),
                    )
            connection.execute(
                "UPDATE browser_profiles SET last_opened_at = ? WHERE name = ?",
                (_timestamp(now), profile_name),
            )
            row = connection.execute(
                "SELECT * FROM browser_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert row is not None
        return self._lease(row)

    def require_lease(
        self,
        task_id: str,
        lease_token: str,
        *,
        allow_takeover: bool = False,
    ) -> BrowserLease:
        now = self._clock()
        with self._connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM browser_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None or not secrets.compare_digest(
                lease_token, str(row["lease_token"])
            ):
                raise BrowserLeaseError("Browser task lease is missing or invalid.")
            state = TaskState(row["state"])
            if state == TaskState.UNCERTAIN:
                raise BrowserUncertainError(
                    "A consequential action may already have completed; do not retry."
                )
            if state == TaskState.RELEASED:
                raise BrowserLeaseError("Browser task lease has been released.")
            if _parse_timestamp(row["lease_expires_at"]) <= now:
                connection.execute(
                    "UPDATE browser_tasks SET state = 'released', updated_at = ? "
                    "WHERE task_id = ?",
                    (_timestamp(now), task_id),
                )
                raise BrowserLeaseError("Browser task lease expired.")
            if state == TaskState.TAKEOVER and not allow_takeover:
                raise BrowserLeaseError(
                    "The browser is under human control; resume it before "
                    "agent actions."
                )
            expires = now + timedelta(seconds=self.lease_seconds)
            connection.execute(
                "UPDATE browser_tasks SET lease_expires_at = ?, updated_at = ? "
                "WHERE task_id = ?",
                (_timestamp(expires), _timestamp(now), task_id),
            )
            updated = connection.execute(
                "SELECT * FROM browser_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert updated is not None
        return self._lease(updated)

    def bump_revision(self, task_id: str, lease_token: str) -> int:
        self.require_lease(task_id, lease_token)
        now = self._clock()
        with self._connect(immediate=True) as connection:
            connection.execute(
                """
                UPDATE browser_tasks
                SET page_revision = page_revision + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (_timestamp(now), task_id),
            )
            row = connection.execute(
                "SELECT page_revision FROM browser_tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        assert row is not None
        return int(row["page_revision"])

    def release(self, task_id: str, lease_token: str) -> BrowserLease:
        lease = self.require_lease(task_id, lease_token, allow_takeover=True)
        now = self._clock()
        with self._connect(immediate=True) as connection:
            connection.execute(
                "UPDATE browser_tasks SET state = 'released', updated_at = ? "
                "WHERE task_id = ?",
                (_timestamp(now), task_id),
            )
            row = connection.execute(
                "SELECT * FROM browser_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert row is not None
        released = self._lease(row)
        assert released.profile_name == lease.profile_name
        return released

    def request_takeover(self, task_id: str, lease_token: str) -> BrowserLease:
        self.require_lease(task_id, lease_token)
        return self._set_task_state(task_id, TaskState.TAKEOVER)

    def resume_takeover(self, task_id: str, lease_token: str) -> BrowserLease:
        lease = self.require_lease(task_id, lease_token, allow_takeover=True)
        if lease.state != TaskState.TAKEOVER:
            raise BrowserStateError("The browser task is not under human control.")
        return self._set_task_state(task_id, TaskState.ACTIVE, bump_revision=True)

    def _set_task_state(
        self, task_id: str, state: TaskState, *, bump_revision: bool = False
    ) -> BrowserLease:
        now = self._clock()
        revision = ", page_revision = page_revision + 1" if bump_revision else ""
        with self._connect(immediate=True) as connection:
            connection.execute(
                f"UPDATE browser_tasks SET state = ?, updated_at = ?{revision} "
                "WHERE task_id = ?",
                (state.value, _timestamp(now), task_id),
            )
            row = connection.execute(
                "SELECT * FROM browser_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        assert row is not None
        return self._lease(row)

    def record_confirmation(
        self,
        *,
        approval_id: str,
        task_id: str,
        lease_token: str,
        page_revision: int,
        digest: str,
        ttl_seconds: int,
    ) -> None:
        approval_id = self.validate_identifier(approval_id, "approval_id")
        if not 30 <= ttl_seconds <= 3600:
            raise BrowserApprovalError(
                "Browser confirmation expiry must be between 30 and 3600 seconds."
            )
        lease = self.require_lease(task_id, lease_token)
        if lease.page_revision != page_revision:
            raise BrowserApprovalError(
                "The page changed before confirmation could be recorded."
            )
        now = self._clock()
        try:
            with self._connect(immediate=True) as connection:
                connection.execute(
                    """
                    INSERT INTO browser_confirmations(
                        approval_id, task_id, page_revision, state_digest,
                        expires_at, used_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                    """,
                    (
                        approval_id,
                        task_id,
                        page_revision,
                        digest,
                        _timestamp(now + timedelta(seconds=ttl_seconds)),
                        _timestamp(now),
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise BrowserApprovalError("Confirmation id was already used.") from error

    def begin_consequential(
        self,
        *,
        operation_id: str,
        task_id: str,
        lease_token: str,
        approval_id: str,
        digest: str,
    ) -> ConsequentialAction:
        operation_id = self.validate_identifier(operation_id, "operation_id")
        previous_action = self.action(operation_id)
        if previous_action is not None:
            if previous_action.status == ActionOutcome.SUCCEEDED:
                return previous_action
            raise BrowserUncertainError(
                "This consequential operation was already attempted; inspect its "
                "definitive site status instead of retrying it."
            )
        lease = self.require_lease(task_id, lease_token)
        now = self._clock()
        with self._connect(immediate=True) as connection:
            previous = connection.execute(
                "SELECT * FROM browser_actions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if previous is not None:
                action = self._action(previous)
                if action.status == ActionOutcome.SUCCEEDED:
                    return action
                raise BrowserUncertainError(
                    "This consequential operation was already attempted; inspect its "
                    "definitive site status instead of retrying it."
                )
            confirmation = connection.execute(
                "SELECT * FROM browser_confirmations WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if confirmation is None or confirmation["task_id"] != task_id:
                raise BrowserApprovalError(
                    "Consequential browser action requires its matching confirmation."
                )
            if confirmation["used_at"] is not None:
                raise BrowserApprovalError("Browser confirmation was already consumed.")
            if _parse_timestamp(confirmation["expires_at"]) <= now:
                raise BrowserApprovalError("Browser confirmation expired.")
            if (
                int(confirmation["page_revision"]) != lease.page_revision
                or confirmation["state_digest"] != digest
            ):
                raise BrowserApprovalError(
                    "Material browser state changed after confirmation."
                )
            connection.execute(
                "UPDATE browser_confirmations SET used_at = ? WHERE approval_id = ?",
                (_timestamp(now), approval_id),
            )
            connection.execute(
                """
                INSERT INTO browser_actions(
                    operation_id, task_id, approval_id, state_digest, status,
                    result_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'pending', NULL, ?, ?)
                """,
                (
                    operation_id,
                    task_id,
                    approval_id,
                    digest,
                    _timestamp(now),
                    _timestamp(now),
                ),
            )
            row = connection.execute(
                "SELECT * FROM browser_actions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        assert row is not None
        return self._action(row)

    def finish_consequential(
        self,
        operation_id: str,
        *,
        succeeded: bool,
        result: dict[str, Any] | None = None,
    ) -> ConsequentialAction:
        now = self._clock()
        status = ActionOutcome.SUCCEEDED if succeeded else ActionOutcome.REJECTED
        with self._connect(immediate=True) as connection:
            existing = connection.execute(
                "SELECT * FROM browser_actions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if existing is None:
                raise BrowserStateError("Consequential operation does not exist.")
            if existing["status"] != ActionOutcome.PENDING:
                raise BrowserStateError("Consequential operation is already final.")
            connection.execute(
                """
                UPDATE browser_actions
                SET status = ?, result_json = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    status.value,
                    json.dumps(result, separators=(",", ":")) if result else None,
                    _timestamp(now),
                    operation_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM browser_actions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        assert row is not None
        return self._action(row)

    def mark_uncertain(self, operation_id: str) -> ConsequentialAction:
        now = self._clock()
        with self._connect(immediate=True) as connection:
            action = connection.execute(
                "SELECT * FROM browser_actions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if action is None:
                raise BrowserStateError("Consequential operation does not exist.")
            connection.execute(
                "UPDATE browser_actions SET status = 'uncertain', updated_at = ? "
                "WHERE operation_id = ?",
                (_timestamp(now), operation_id),
            )
            connection.execute(
                """
                UPDATE browser_tasks
                SET state = 'uncertain', uncertain_operation_id = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (operation_id, _timestamp(now), action["task_id"]),
            )
            row = connection.execute(
                "SELECT * FROM browser_actions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        assert row is not None
        return self._action(row)

    def action(self, operation_id: str) -> ConsequentialAction | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM browser_actions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        return self._action(row) if row is not None else None

    def uncertain_actions(self) -> tuple[ConsequentialAction, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM browser_actions
                WHERE status = 'uncertain'
                ORDER BY created_at
                """
            ).fetchall()
        return tuple(self._action(row) for row in rows)

    def resolve_uncertain(
        self,
        operation_id: str,
        *,
        outcome: str,
    ) -> ConsequentialAction:
        """Record an owner-verified site outcome and release its blocked task."""
        if outcome not in {"confirmed_succeeded", "not_submitted"}:
            raise BrowserStateError(
                "Recovery outcome must be confirmed_succeeded or not_submitted."
            )
        now = self._clock()
        status = (
            ActionOutcome.SUCCEEDED
            if outcome == "confirmed_succeeded"
            else ActionOutcome.REJECTED
        )
        result = (
            {"status": "owner_verified_after_uncertain_response"}
            if status == ActionOutcome.SUCCEEDED
            else None
        )
        with self._connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM browser_actions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None or row["status"] != ActionOutcome.UNCERTAIN:
                raise BrowserStateError(
                    "Only an uncertain consequential operation can be reconciled."
                )
            task = connection.execute(
                "SELECT * FROM browser_tasks WHERE task_id = ?", (row["task_id"],)
            ).fetchone()
            if (
                task is None
                or task["state"] != TaskState.UNCERTAIN
                or task["uncertain_operation_id"] != operation_id
            ):
                raise BrowserStateError(
                    "Uncertain operation is not the task's current recovery boundary."
                )
            connection.execute(
                """
                UPDATE browser_actions
                SET status = ?, result_json = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    status.value,
                    json.dumps(result, separators=(",", ":")) if result else None,
                    _timestamp(now),
                    operation_id,
                ),
            )
            connection.execute(
                """
                UPDATE browser_tasks
                SET state = 'released', uncertain_operation_id = NULL, updated_at = ?
                WHERE task_id = ?
                """,
                (_timestamp(now), row["task_id"]),
            )
            updated = connection.execute(
                "SELECT * FROM browser_actions WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        assert updated is not None
        return self._action(updated)

    def record_journal(
        self,
        operation: str,
        outcome: str,
        *,
        task_id: str | None = None,
        origin: str | None = None,
        detail: str | None = None,
        artifact_path: Path | None = None,
    ) -> int:
        safe_detail = (
            redact_sensitive_text(detail).strip()[:_MAX_JOURNAL_DETAIL]
            if detail
            else None
        )
        now = self._clock()
        with self._connect(immediate=True) as connection:
            cursor = connection.execute(
                """
                INSERT INTO browser_journal(
                    task_id, occurred_at, origin, operation, outcome, detail,
                    artifact_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    _timestamp(now),
                    origin,
                    operation[:80],
                    outcome[:40],
                    safe_detail,
                    str(artifact_path) if artifact_path else None,
                ),
            )
            if cursor.lastrowid is None:
                raise BrowserStateError("Browser journal insert did not return an id.")
            entry_id = int(cursor.lastrowid)
        return entry_id

    def journal(self, *, limit: int = 100) -> tuple[JournalEntry, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("Journal limit must be between 1 and 1000.")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM browser_journal ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return tuple(self._journal_entry(row) for row in rows)

    def prune_journal(
        self, *, retention_days: int, maximum_entries: int
    ) -> tuple[Path, ...]:
        if retention_days < 1 or maximum_entries < 1:
            raise ValueError("Journal retention limits must be positive.")
        cutoff = self._clock() - timedelta(days=retention_days)
        with self._connect(immediate=True) as connection:
            rows = connection.execute(
                """
                SELECT artifact_path FROM browser_journal
                WHERE occurred_at < ?
                   OR id NOT IN (
                       SELECT id FROM browser_journal ORDER BY id DESC LIMIT ?
                   )
                """,
                (_timestamp(cutoff), maximum_entries),
            ).fetchall()
            connection.execute(
                """
                DELETE FROM browser_journal
                WHERE occurred_at < ?
                   OR id NOT IN (
                       SELECT id FROM browser_journal ORDER BY id DESC LIMIT ?
                   )
                """,
                (_timestamp(cutoff), maximum_entries),
            )
        return tuple(
            Path(row["artifact_path"])
            for row in rows
            if row["artifact_path"] is not None
        )

    @staticmethod
    def _profile(row: sqlite3.Row) -> BrowserProfile:
        return BrowserProfile(
            name=row["name"],
            user_data_path=Path(row["user_data_path"]),
            created_at=_parse_timestamp(row["created_at"]),
            last_opened_at=(
                _parse_timestamp(row["last_opened_at"])
                if row["last_opened_at"]
                else None
            ),
        )

    @staticmethod
    def _lease(row: sqlite3.Row) -> BrowserLease:
        return BrowserLease(
            task_id=row["task_id"],
            profile_name=row["profile_name"],
            token=row["lease_token"],
            state=TaskState(row["state"]),
            expires_at=_parse_timestamp(row["lease_expires_at"]),
            page_revision=int(row["page_revision"]),
            uncertain_operation_id=row["uncertain_operation_id"],
        )

    @staticmethod
    def _action(row: sqlite3.Row) -> ConsequentialAction:
        result = json.loads(row["result_json"]) if row["result_json"] else None
        return ConsequentialAction(
            operation_id=row["operation_id"],
            task_id=row["task_id"],
            status=ActionOutcome(row["status"]),
            result=result,
        )

    @staticmethod
    def _journal_entry(row: sqlite3.Row) -> JournalEntry:
        return JournalEntry(
            entry_id=int(row["id"]),
            task_id=row["task_id"],
            occurred_at=_parse_timestamp(row["occurred_at"]),
            origin=row["origin"],
            operation=row["operation"],
            outcome=row["outcome"],
            detail=row["detail"],
            artifact_path=Path(row["artifact_path"]) if row["artifact_path"] else None,
        )


__all__ = ["BrowserState", "state_digest"]
