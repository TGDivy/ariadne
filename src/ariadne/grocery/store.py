"""Private SQLite lifecycle, approval, checkout, and repair state."""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal, cast

from .errors import GroceryApprovalError, GroceryStateError, GroceryUncertainError
from .models import (
    ApprovalStatus,
    BrowserAttachment,
    CheckoutOperation,
    CheckoutStatus,
    DownstreamRecord,
    DownstreamStatus,
    FulfilmentMode,
    GroceryApproval,
    GroceryDraft,
    GroceryState,
    OrderConfirmation,
    OrderSnapshot,
    PreferenceEvidence,
    ProductConstraint,
    RequestedItem,
    material_differences,
)

ApprovalDecision = Literal["approve", "cancel"]
ApprovalOutcome = Literal[
    "accepted", "already_decided", "inactive", "stale", "unauthorized"
]
RecoveryOutcome = Literal["confirmed_succeeded", "confirmed_not_submitted"]


def _now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Grocery timestamps must include a timezone offset.")
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(UTC)


class ApprovalSelection(tuple[ApprovalOutcome, GroceryApproval | None]):
    """Typed compact result returned by a trusted Telegram callback."""

    __slots__ = ()

    @property
    def outcome(self) -> ApprovalOutcome:
        return self[0]

    @property
    def approval(self) -> GroceryApproval | None:
        return self[1]


_TRANSITIONS: dict[GroceryState, frozenset[GroceryState]] = {
    GroceryState.UNDERSTAND: frozenset({GroceryState.BUILD, GroceryState.CANCELLED}),
    GroceryState.BUILD: frozenset(
        {
            GroceryState.RESOLVE,
            GroceryState.NEEDS_TAKEOVER,
            GroceryState.CANCELLED,
        }
    ),
    GroceryState.RESOLVE: frozenset(
        {
            GroceryState.BUILD,
            GroceryState.REVIEW,
            GroceryState.NEEDS_TAKEOVER,
            GroceryState.CANCELLED,
        }
    ),
    GroceryState.REVIEW: frozenset(
        {GroceryState.RESOLVE, GroceryState.APPROVED, GroceryState.CANCELLED}
    ),
    GroceryState.APPROVED: frozenset(
        {GroceryState.REVALIDATE, GroceryState.REVIEW, GroceryState.CANCELLED}
    ),
    GroceryState.REVALIDATE: frozenset(
        {
            GroceryState.CHECKOUT,
            GroceryState.REVIEW,
            GroceryState.NEEDS_TAKEOVER,
            GroceryState.CANCELLED,
        }
    ),
    GroceryState.CHECKOUT: frozenset(
        {
            GroceryState.CONFIRMED,
            GroceryState.REJECTED,
            GroceryState.UNCERTAIN,
        }
    ),
    GroceryState.NEEDS_TAKEOVER: frozenset(
        {
            GroceryState.BUILD,
            GroceryState.RESOLVE,
            GroceryState.REVALIDATE,
            GroceryState.CANCELLED,
        }
    ),
    GroceryState.UNCERTAIN: frozenset({GroceryState.CONFIRMED, GroceryState.REJECTED}),
    GroceryState.REJECTED: frozenset({GroceryState.RESOLVE}),
    GroceryState.CONFIRMED: frozenset(),
    GroceryState.CANCELLED: frozenset(),
}


class GroceryStore:
    """Coordinate grocery work across the bot, MCP, and browser processes."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime] = _now,
    ) -> None:
        self.path = path.expanduser().resolve()
        self._clock = clock
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self.path.touch(mode=0o600, exist_ok=True)
        self.path.chmod(0o600)
        with self._connect_unchecked() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA foreign_keys = ON;
                CREATE TABLE IF NOT EXISTS grocery_drafts (
                    draft_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    draft_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS grocery_revisions (
                    draft_id TEXT NOT NULL REFERENCES grocery_drafts(draft_id),
                    revision INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    snapshot_json TEXT,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(draft_id, revision)
                );
                CREATE TABLE IF NOT EXISTS grocery_approvals (
                    approval_id TEXT PRIMARY KEY,
                    draft_id TEXT NOT NULL REFERENCES grocery_drafts(draft_id),
                    revision INTEGER NOT NULL,
                    snapshot_digest TEXT NOT NULL,
                    owner_user_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER,
                    status TEXT NOT NULL CHECK(status IN (
                        'pending','approved','cancelled','expired','invalidated','used'
                    )),
                    expires_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_live_grocery_approval
                    ON grocery_approvals(draft_id)
                    WHERE status IN ('pending','approved');
                CREATE TABLE IF NOT EXISTS grocery_checkouts (
                    operation_id TEXT PRIMARY KEY,
                    draft_id TEXT NOT NULL UNIQUE
                        REFERENCES grocery_drafts(draft_id),
                    approval_id TEXT NOT NULL
                        REFERENCES grocery_approvals(approval_id),
                    status TEXT NOT NULL CHECK(status IN (
                        'submitting','succeeded','rejected','uncertain'
                    )),
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS grocery_confirmations (
                    draft_id TEXT PRIMARY KEY REFERENCES grocery_drafts(draft_id),
                    confirmation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS grocery_downstream (
                    draft_id TEXT PRIMARY KEY REFERENCES grocery_drafts(draft_id),
                    calendar_status TEXT NOT NULL,
                    calendar_reference TEXT,
                    calendar_error TEXT,
                    knowledge_status TEXT NOT NULL,
                    knowledge_reference TEXT,
                    knowledge_error TEXT,
                    handoff_status TEXT NOT NULL,
                    handoff_reference TEXT,
                    handoff_error TEXT
                );
                CREATE TABLE IF NOT EXISTS grocery_handoff_outbox (
                    handoff_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    draft_id TEXT NOT NULL UNIQUE
                        REFERENCES grocery_drafts(draft_id),
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                """
            )
        self._initialized = True

    @contextmanager
    def _connect(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        self.initialize()
        with self._connect_unchecked() as connection:
            if immediate:
                connection.execute("BEGIN IMMEDIATE")
            yield connection

    def _connect_unchecked(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def create_draft(
        self,
        requested_items: Sequence[RequestedItem],
        *,
        mode: FulfilmentMode | None = None,
        requested_window: str | None = None,
        location: str | None = None,
        preferences: Sequence[PreferenceEvidence] = (),
        constraints: Sequence[ProductConstraint] = (),
    ) -> GroceryDraft:
        if not requested_items:
            raise GroceryStateError(
                "A grocery draft needs at least one requested item."
            )
        identifiers = [item.item_id for item in requested_items]
        if len(identifiers) != len(set(identifiers)):
            raise GroceryStateError("Requested grocery item ids must be unique.")
        if mode is None and location is not None:
            raise GroceryStateError(
                "A fulfilment location cannot be fixed before delivery or collection."
            )
        now = self._clock()
        # Hex, not URL-safe base64: an identifier must never begin with the
        # leading '-' or '_' that a base64 token can produce.
        draft_id = secrets.token_hex(9)
        draft = GroceryDraft(
            draft_id=draft_id,
            task_id=f"grocery.{draft_id}",
            state=GroceryState.UNDERSTAND,
            revision=1,
            requested_items=tuple(requested_items),
            requested_window=requested_window,
            mode=mode,
            location=location,
            preferences=tuple(preferences),
            constraints=tuple(constraints),
            created_at=now,
            updated_at=now,
        )
        with self._connect(immediate=True) as connection:
            connection.execute(
                """
                INSERT INTO grocery_drafts(
                    draft_id, state, revision, draft_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    draft.draft_id,
                    draft.state.value,
                    draft.revision,
                    draft.model_dump_json(),
                    _timestamp(now),
                    _timestamp(now),
                ),
            )
            self._insert_revision(connection, draft, "Created requested grocery list")
        return draft

    def get(self, draft_id: str) -> GroceryDraft:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT draft_json FROM grocery_drafts WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        if row is None:
            raise GroceryStateError("That grocery draft does not exist.")
        return GroceryDraft.model_validate_json(str(row["draft_json"]))

    def list(self, *, limit: int = 20) -> tuple[GroceryDraft, ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Grocery draft limit must be between 1 and 100.")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT draft_json FROM grocery_drafts "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(
            GroceryDraft.model_validate_json(str(row["draft_json"])) for row in rows
        )

    def save(
        self,
        proposed: GroceryDraft,
        *,
        expected_revision: int,
        reason: str,
        material: bool = True,
    ) -> GroceryDraft:
        """Atomically save one caller-derived draft and invalidate stale approval."""
        now = self._clock()
        with self._connect(immediate=True) as connection:
            current = self._draft_in(connection, proposed.draft_id)
            if current.revision != expected_revision:
                raise GroceryStateError(
                    "The grocery draft changed; read it again before updating it."
                )
            if (
                proposed.draft_id != current.draft_id
                or proposed.task_id != current.task_id
            ):
                raise GroceryStateError("A grocery draft's identity cannot change.")
            self._validate_transition(current.state, proposed.state)
            revision = current.revision + 1 if material else current.revision
            updated = proposed.model_copy(
                update={
                    "revision": revision,
                    "created_at": current.created_at,
                    "updated_at": now,
                    # The browser lease belongs to the adapter, not to whichever
                    # caller happens to be saving. A caller holding a draft read
                    # before the adapter attached must not erase the live lease.
                    "browser": proposed.browser or current.browser,
                }
            )
            cursor = connection.execute(
                """
                UPDATE grocery_drafts
                SET state = ?, revision = ?, draft_json = ?, updated_at = ?
                WHERE draft_id = ? AND revision = ?
                """,
                (
                    updated.state.value,
                    updated.revision,
                    updated.model_dump_json(),
                    _timestamp(now),
                    updated.draft_id,
                    expected_revision,
                ),
            )
            if cursor.rowcount != 1:
                raise GroceryStateError(
                    "The grocery draft changed; read it again before updating it."
                )
            if material:
                self._invalidate_approvals(connection, updated.draft_id, now)
                self._insert_revision(connection, updated, reason)
        return updated

    def attach_browser(
        self,
        draft_id: str,
        attachment: BrowserAttachment,
    ) -> GroceryDraft:
        """Persist the private lease without changing the approval revision."""
        current = self.get(draft_id)
        if attachment.task_id != current.task_id:
            raise GroceryStateError("Browser task does not belong to this draft.")
        proposed = current.model_copy(update={"browser": attachment})
        return self.save(
            proposed,
            expected_revision=current.revision,
            reason="Attached private browser task",
            material=False,
        )

    def create_approval(
        self,
        draft_id: str,
        *,
        owner_user_id: int,
        chat_id: int,
        ttl_seconds: int,
    ) -> GroceryApproval:
        if not 30 <= ttl_seconds <= 3600:
            raise ValueError("Grocery approval must expire in 30-3600 seconds.")
        now = self._clock()
        with self._connect(immediate=True) as connection:
            self._expire_approvals(connection, now)
            draft = self._draft_in(connection, draft_id)
            if draft.state != GroceryState.REVIEW or draft.snapshot is None:
                raise GroceryApprovalError(
                    "The live basket must be in review before approval is requested."
                )
            approval = GroceryApproval(
                approval_id=secrets.token_hex(9),
                draft_id=draft_id,
                revision=draft.revision,
                snapshot_digest=draft.snapshot.digest,
                owner_user_id=owner_user_id,
                chat_id=chat_id,
                status=ApprovalStatus.PENDING,
                expires_at=now + timedelta(seconds=ttl_seconds),
                created_at=now,
            )
            try:
                connection.execute(
                    """
                    INSERT INTO grocery_approvals(
                        approval_id, draft_id, revision, snapshot_digest,
                        owner_user_id, chat_id, message_id, status, expires_at,
                        created_at, decided_at
                    ) VALUES (?, ?, ?, ?, ?, ?, NULL, 'pending', ?, ?, NULL)
                    """,
                    (
                        approval.approval_id,
                        approval.draft_id,
                        approval.revision,
                        approval.snapshot_digest,
                        approval.owner_user_id,
                        approval.chat_id,
                        _timestamp(approval.expires_at),
                        _timestamp(now),
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise GroceryApprovalError(
                    "This grocery draft already has a live approval request."
                ) from error
        return approval

    def attach_approval_message(
        self,
        approval_id: str,
        message_id: int,
    ) -> GroceryApproval:
        with self._connect(immediate=True) as connection:
            cursor = connection.execute(
                """
                UPDATE grocery_approvals SET message_id = ?
                WHERE approval_id = ? AND message_id IS NULL
                """,
                (message_id, approval_id),
            )
            if cursor.rowcount != 1:
                raise GroceryApprovalError("Grocery approval message is stale.")
            row = self._approval_row(connection, approval_id)
        assert row is not None
        return self._approval(row)

    def approval(self, approval_id: str) -> GroceryApproval | None:
        now = self._clock()
        with self._connect(immediate=True) as connection:
            self._expire_approvals(connection, now)
            row = self._approval_row(connection, approval_id)
        return self._approval(row) if row is not None else None

    def live_approval(self, draft_id: str) -> GroceryApproval | None:
        """Return the current pending/approved binding after expiring stale rows."""
        now = self._clock()
        with self._connect(immediate=True) as connection:
            self._expire_approvals(connection, now)
            row = connection.execute(
                """
                SELECT * FROM grocery_approvals
                WHERE draft_id = ? AND status IN ('pending','approved')
                ORDER BY created_at DESC LIMIT 1
                """,
                (draft_id,),
            ).fetchone()
        return self._approval(row) if row is not None else None

    def decide_approval(
        self,
        approval_id: str,
        *,
        user_id: int,
        chat_id: int,
        message_id: int,
        decision: ApprovalDecision,
    ) -> ApprovalSelection:
        now = self._clock()
        with self._connect(immediate=True) as connection:
            self._expire_approvals(connection, now)
            row = self._approval_row(connection, approval_id)
            if row is None:
                return ApprovalSelection(("stale", None))
            approval = self._approval(row)
            if user_id != approval.owner_user_id:
                return ApprovalSelection(("unauthorized", approval))
            if approval.chat_id != chat_id or (
                approval.message_id is not None and approval.message_id != message_id
            ):
                return ApprovalSelection(("stale", approval))
            if approval.status in {ApprovalStatus.APPROVED, ApprovalStatus.CANCELLED}:
                return ApprovalSelection(("already_decided", approval))
            if approval.status != ApprovalStatus.PENDING:
                return ApprovalSelection(("inactive", approval))
            draft = self._draft_in(connection, approval.draft_id)
            if (
                draft.state != GroceryState.REVIEW
                or draft.revision != approval.revision
                or draft.snapshot is None
                or draft.snapshot.digest != approval.snapshot_digest
            ):
                connection.execute(
                    "UPDATE grocery_approvals SET status = 'invalidated' "
                    "WHERE approval_id = ?",
                    (approval_id,),
                )
                return ApprovalSelection(("inactive", approval))

            status = (
                ApprovalStatus.APPROVED
                if decision == "approve"
                else ApprovalStatus.CANCELLED
            )
            state = (
                GroceryState.APPROVED
                if decision == "approve"
                else GroceryState.CANCELLED
            )
            connection.execute(
                """
                UPDATE grocery_approvals
                SET status = ?, decided_at = ? WHERE approval_id = ?
                """,
                (status.value, _timestamp(now), approval_id),
            )
            updated_draft = draft.model_copy(update={"state": state, "updated_at": now})
            self._replace_draft(connection, updated_draft)
            decided_row = self._approval_row(connection, approval_id)
        assert decided_row is not None
        return ApprovalSelection(("accepted", self._approval(decided_row)))

    def mark_revalidating(self, draft_id: str) -> tuple[GroceryDraft, GroceryApproval]:
        now = self._clock()
        with self._connect(immediate=True) as connection:
            self._expire_approvals(connection, now)
            draft = self._draft_in(connection, draft_id)
            if draft.state != GroceryState.APPROVED or draft.snapshot is None:
                raise GroceryApprovalError(
                    "This checkout does not have a current exact approval."
                )
            row = connection.execute(
                """
                SELECT * FROM grocery_approvals
                WHERE draft_id = ? AND status = 'approved'
                ORDER BY created_at DESC LIMIT 1
                """,
                (draft_id,),
            ).fetchone()
            if row is None:
                raise GroceryApprovalError("The grocery approval expired or changed.")
            approval = self._approval(row)
            if (
                approval.revision != draft.revision
                or approval.snapshot_digest != draft.snapshot.digest
            ):
                raise GroceryApprovalError("The approved grocery snapshot is stale.")
            updated = draft.model_copy(
                update={"state": GroceryState.REVALIDATE, "updated_at": now}
            )
            self._replace_draft(connection, updated)
        return updated, approval

    def return_to_review(
        self,
        draft_id: str,
        live: OrderSnapshot,
        *,
        reason: str,
    ) -> GroceryDraft:
        draft = self.get(draft_id)
        proposed = draft.model_copy(
            update={
                "state": GroceryState.REVIEW,
                "snapshot": live,
                "basket": live.items,
                "substitutions": live.substitutions,
                "omitted": live.omitted,
                "selected_slot": live.slot,
                "mode": live.mode,
                "location": live.location,
                "issue": reason,
            }
        )
        return self.save(
            proposed,
            expected_revision=draft.revision,
            reason=reason,
            material=True,
        )

    def begin_checkout(
        self,
        draft_id: str,
        *,
        approval_id: str,
        operation_id: str,
        live: OrderSnapshot,
        weighted_tolerance: Decimal,
    ) -> CheckoutOperation:
        now = self._clock()
        with self._connect(immediate=True) as connection:
            self._expire_approvals(connection, now)
            existing = connection.execute(
                "SELECT * FROM grocery_checkouts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
            if existing is not None:
                operation = self._checkout(existing)
                if operation.status == CheckoutStatus.SUCCEEDED:
                    return operation
                raise GroceryUncertainError(
                    "Checkout was already attempted. Verify the retailer's definitive "
                    "order status; do not submit it again."
                )
            draft = self._draft_in(connection, draft_id)
            row = self._approval_row(connection, approval_id)
            if row is None:
                raise GroceryApprovalError("The checkout approval does not exist.")
            approval = self._approval(row)
            if (
                draft.state != GroceryState.REVALIDATE
                or draft.snapshot is None
                or approval.draft_id != draft_id
                or approval.status != ApprovalStatus.APPROVED
                or approval.revision != draft.revision
                or approval.snapshot_digest != draft.snapshot.digest
            ):
                raise GroceryApprovalError("The checkout approval is no longer exact.")
            differences = material_differences(
                draft.snapshot,
                live,
                weighted_tolerance=weighted_tolerance,
            )
            if differences:
                raise GroceryApprovalError("; ".join(differences))
            connection.execute(
                "UPDATE grocery_approvals SET status = 'used', decided_at = ? "
                "WHERE approval_id = ?",
                (_timestamp(now), approval_id),
            )
            updated = draft.model_copy(
                update={
                    "state": GroceryState.CHECKOUT,
                    "snapshot": live,
                    "basket": live.items,
                    "selected_slot": live.slot,
                    "updated_at": now,
                }
            )
            self._replace_draft(connection, updated)
            connection.execute(
                """
                INSERT INTO grocery_checkouts(
                    operation_id, draft_id, approval_id, status, result_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, 'submitting', NULL, ?, ?)
                """,
                (
                    operation_id,
                    draft_id,
                    approval_id,
                    _timestamp(now),
                    _timestamp(now),
                ),
            )
            checkout = connection.execute(
                "SELECT * FROM grocery_checkouts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        assert checkout is not None
        return self._checkout(checkout)

    def mark_checkout_uncertain(
        self, operation_id: str, reason: str
    ) -> CheckoutOperation:
        return self._finish_checkout(
            operation_id,
            CheckoutStatus.UNCERTAIN,
            GroceryState.UNCERTAIN,
            {"reason": reason[:1_000]},
        )

    def reject_checkout(self, operation_id: str, reason: str) -> CheckoutOperation:
        return self._finish_checkout(
            operation_id,
            CheckoutStatus.REJECTED,
            GroceryState.REJECTED,
            {"reason": reason[:1_000]},
        )

    def confirm_checkout(
        self,
        operation_id: str,
        confirmation: OrderConfirmation,
    ) -> CheckoutOperation:
        operation = self._finish_checkout(
            operation_id,
            CheckoutStatus.SUCCEEDED,
            GroceryState.CONFIRMED,
            confirmation.model_dump(mode="json"),
        )
        with self._connect(immediate=True) as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO grocery_confirmations(
                    draft_id, confirmation_json, created_at
                ) VALUES (?, ?, ?)
                """,
                (
                    operation.draft_id,
                    confirmation.model_dump_json(),
                    _timestamp(self._clock()),
                ),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO grocery_downstream(
                    draft_id, calendar_status, knowledge_status, handoff_status
                ) VALUES (?, 'pending', 'pending', 'pending')
                """,
                (operation.draft_id,),
            )
        return operation

    def _finish_checkout(
        self,
        operation_id: str,
        status: CheckoutStatus,
        state: GroceryState,
        result: dict[str, object],
    ) -> CheckoutOperation:
        now = self._clock()
        with self._connect(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM grocery_checkouts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if row is None:
                raise GroceryStateError("Checkout operation does not exist.")
            operation = self._checkout(row)
            if operation.status == CheckoutStatus.SUCCEEDED:
                return operation
            if operation.status != CheckoutStatus.SUBMITTING:
                raise GroceryUncertainError(
                    "Checkout already has a terminal or uncertain outcome."
                )
            connection.execute(
                """
                UPDATE grocery_checkouts
                SET status = ?, result_json = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (
                    status.value,
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                    _timestamp(now),
                    operation_id,
                ),
            )
            draft = self._draft_in(connection, operation.draft_id)
            self._validate_transition(draft.state, state)
            self._replace_draft(
                connection,
                draft.model_copy(update={"state": state, "updated_at": now}),
            )
            updated = connection.execute(
                "SELECT * FROM grocery_checkouts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        assert updated is not None
        return self._checkout(updated)

    def recover_interrupted_checkouts(self) -> tuple[CheckoutOperation, ...]:
        """Conservatively mark submissions left in-flight by a process restart."""
        now = self._clock()
        with self._connect(immediate=True) as connection:
            rows = connection.execute(
                "SELECT * FROM grocery_checkouts WHERE status = 'submitting'"
            ).fetchall()
            for row in rows:
                operation = self._checkout(row)
                connection.execute(
                    """
                    UPDATE grocery_checkouts
                    SET status = 'uncertain', result_json = ?, updated_at = ?
                    WHERE operation_id = ?
                    """,
                    (
                        json.dumps({"reason": "service restarted during submission"}),
                        _timestamp(now),
                        operation.operation_id,
                    ),
                )
                draft = self._draft_in(connection, operation.draft_id)
                if draft.state == GroceryState.CHECKOUT:
                    self._replace_draft(
                        connection,
                        draft.model_copy(
                            update={"state": GroceryState.UNCERTAIN, "updated_at": now}
                        ),
                    )
            recovered_rows = connection.execute(
                "SELECT * FROM grocery_checkouts WHERE status = 'uncertain'"
            ).fetchall()
        return tuple(self._checkout(row) for row in recovered_rows)

    def reconcile_uncertain(
        self,
        draft_id: str,
        *,
        outcome: RecoveryOutcome,
        confirmation: OrderConfirmation | None = None,
    ) -> GroceryDraft:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM grocery_checkouts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        if row is None or self._checkout(row).status != CheckoutStatus.UNCERTAIN:
            raise GroceryStateError("This grocery checkout is not uncertain.")
        operation = self._checkout(row)
        if outcome == "confirmed_succeeded":
            if confirmation is None:
                raise GroceryStateError(
                    "A definitive order number is required to confirm success."
                )
            now = self._clock()
            with self._connect(immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE grocery_checkouts
                    SET status = 'succeeded', result_json = ?, updated_at = ?
                    WHERE operation_id = ? AND status = 'uncertain'
                    """,
                    (
                        confirmation.model_dump_json(),
                        _timestamp(now),
                        operation.operation_id,
                    ),
                )
                draft = self._draft_in(connection, draft_id)
                self._replace_draft(
                    connection,
                    draft.model_copy(
                        update={"state": GroceryState.CONFIRMED, "updated_at": now}
                    ),
                )
                connection.execute(
                    """
                    INSERT OR REPLACE INTO grocery_confirmations(
                        draft_id, confirmation_json, created_at
                    ) VALUES (?, ?, ?)
                    """,
                    (draft_id, confirmation.model_dump_json(), _timestamp(now)),
                )
                connection.execute(
                    """
                    INSERT OR IGNORE INTO grocery_downstream(
                        draft_id, calendar_status, knowledge_status, handoff_status
                    ) VALUES (?, 'pending', 'pending', 'pending')
                    """,
                    (draft_id,),
                )
        else:
            now = self._clock()
            with self._connect(immediate=True) as connection:
                connection.execute(
                    """
                    UPDATE grocery_checkouts
                    SET status = 'rejected', result_json = ?, updated_at = ?
                    WHERE operation_id = ? AND status = 'uncertain'
                    """,
                    (
                        json.dumps({"owner_verified": "not_submitted"}),
                        _timestamp(now),
                        operation.operation_id,
                    ),
                )
                draft = self._draft_in(connection, draft_id)
                self._replace_draft(
                    connection,
                    draft.model_copy(
                        update={"state": GroceryState.REJECTED, "updated_at": now}
                    ),
                )
        return self.get(draft_id)

    def checkout(self, draft_id: str) -> CheckoutOperation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM grocery_checkouts WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        return self._checkout(row) if row is not None else None

    def confirmation(self, draft_id: str) -> OrderConfirmation | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT confirmation_json FROM grocery_confirmations "
                "WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        return (
            OrderConfirmation.model_validate_json(str(row["confirmation_json"]))
            if row is not None
            else None
        )

    def downstream(self, draft_id: str) -> DownstreamRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM grocery_downstream WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        return self._downstream(row) if row is not None else None

    def set_downstream(
        self,
        draft_id: str,
        target: Literal["calendar", "knowledge", "handoff"],
        status: DownstreamStatus,
        *,
        reference: str | None = None,
        error: str | None = None,
    ) -> DownstreamRecord:
        with self._connect(immediate=True) as connection:
            cursor = connection.execute(
                f"""
                UPDATE grocery_downstream
                SET {target}_status = ?, {target}_reference = ?, {target}_error = ?
                WHERE draft_id = ?
                """,
                (status.value, reference, error[:1_000] if error else None, draft_id),
            )
            if cursor.rowcount != 1:
                raise GroceryStateError("Confirmed order downstream state is missing.")
            row = connection.execute(
                "SELECT * FROM grocery_downstream WHERE draft_id = ?", (draft_id,)
            ).fetchone()
        assert row is not None
        return self._downstream(row)

    def enqueue_handoff(self, draft_id: str, payload: dict[str, object]) -> str:
        now = self._clock()
        with self._connect(immediate=True) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO grocery_handoff_outbox(
                    draft_id, payload_json, created_at, consumed_at
                ) VALUES (?, ?, ?, NULL)
                """,
                (
                    draft_id,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    _timestamp(now),
                ),
            )
            row = connection.execute(
                "SELECT handoff_id FROM grocery_handoff_outbox WHERE draft_id = ?",
                (draft_id,),
            ).fetchone()
        assert row is not None
        return f"grocery-handoff:{int(row['handoff_id'])}"

    def pending_handoffs(self, *, limit: int = 50) -> tuple[dict[str, object], ...]:
        if not 1 <= limit <= 100:
            raise ValueError("Handoff limit must be between 1 and 100.")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT handoff_id, draft_id, payload_json, created_at
                FROM grocery_handoff_outbox
                WHERE consumed_at IS NULL ORDER BY handoff_id LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return tuple(
            {
                "handoff_id": f"grocery-handoff:{int(row['handoff_id'])}",
                "draft_id": str(row["draft_id"]),
                "payload": cast(dict[str, object], json.loads(row["payload_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        )

    def cancel(
        self, draft_id: str, *, reason: str = "Cancelled by owner"
    ) -> GroceryDraft:
        draft = self.get(draft_id)
        if draft.state in {
            GroceryState.CHECKOUT,
            GroceryState.UNCERTAIN,
            GroceryState.CONFIRMED,
        }:
            raise GroceryStateError(
                "A submitted or uncertain checkout cannot be cancelled as unsubmitted."
            )
        proposed = draft.model_copy(
            update={"state": GroceryState.CANCELLED, "issue": reason}
        )
        return self.save(
            proposed,
            expected_revision=draft.revision,
            reason=reason,
            material=True,
        )

    def _expire_approvals(self, connection: sqlite3.Connection, now: datetime) -> None:
        connection.execute(
            """
            UPDATE grocery_approvals
            SET status = 'expired', decided_at = ?
            WHERE status IN ('pending','approved') AND expires_at <= ?
            """,
            (_timestamp(now), _timestamp(now)),
        )
        rows = connection.execute(
            """
            SELECT DISTINCT draft_id FROM grocery_approvals
            WHERE status = 'expired' AND decided_at = ?
            """,
            (_timestamp(now),),
        ).fetchall()
        for row in rows:
            draft = self._draft_in(connection, str(row["draft_id"]))
            if draft.state == GroceryState.APPROVED:
                self._replace_draft(
                    connection,
                    draft.model_copy(
                        update={"state": GroceryState.REVIEW, "updated_at": now}
                    ),
                )

    @staticmethod
    def _invalidate_approvals(
        connection: sqlite3.Connection, draft_id: str, now: datetime
    ) -> None:
        connection.execute(
            """
            UPDATE grocery_approvals
            SET status = 'invalidated', decided_at = ?
            WHERE draft_id = ? AND status IN ('pending','approved')
            """,
            (_timestamp(now), draft_id),
        )

    @staticmethod
    def _validate_transition(current: GroceryState, target: GroceryState) -> None:
        if target == current:
            return
        if target not in _TRANSITIONS[current]:
            raise GroceryStateError(
                f"Grocery draft cannot move from {current.value} to {target.value}."
            )

    def _draft_in(self, connection: sqlite3.Connection, draft_id: str) -> GroceryDraft:
        row = connection.execute(
            "SELECT draft_json FROM grocery_drafts WHERE draft_id = ?", (draft_id,)
        ).fetchone()
        if row is None:
            raise GroceryStateError("That grocery draft does not exist.")
        return GroceryDraft.model_validate_json(str(row["draft_json"]))

    @staticmethod
    def _replace_draft(connection: sqlite3.Connection, draft: GroceryDraft) -> None:
        connection.execute(
            """
            UPDATE grocery_drafts
            SET state = ?, revision = ?, draft_json = ?, updated_at = ?
            WHERE draft_id = ?
            """,
            (
                draft.state.value,
                draft.revision,
                draft.model_dump_json(),
                _timestamp(draft.updated_at),
                draft.draft_id,
            ),
        )

    @staticmethod
    def _insert_revision(
        connection: sqlite3.Connection,
        draft: GroceryDraft,
        reason: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO grocery_revisions(
                draft_id, revision, state, snapshot_json, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                draft.draft_id,
                draft.revision,
                draft.state.value,
                draft.snapshot.model_dump_json() if draft.snapshot else None,
                reason[:1_000],
                _timestamp(draft.updated_at),
            ),
        )

    @staticmethod
    def _approval_row(
        connection: sqlite3.Connection, approval_id: str
    ) -> sqlite3.Row | None:
        return cast(
            sqlite3.Row | None,
            connection.execute(
                "SELECT * FROM grocery_approvals WHERE approval_id = ?",
                (approval_id,),
            ).fetchone(),
        )

    @staticmethod
    def _approval(row: sqlite3.Row) -> GroceryApproval:
        return GroceryApproval(
            approval_id=str(row["approval_id"]),
            draft_id=str(row["draft_id"]),
            revision=int(row["revision"]),
            snapshot_digest=str(row["snapshot_digest"]),
            owner_user_id=int(row["owner_user_id"]),
            chat_id=int(row["chat_id"]),
            message_id=(
                int(row["message_id"]) if row["message_id"] is not None else None
            ),
            status=ApprovalStatus(str(row["status"])),
            expires_at=_parse_timestamp(str(row["expires_at"])),
            created_at=_parse_timestamp(str(row["created_at"])),
            decided_at=(
                _parse_timestamp(str(row["decided_at"]))
                if row["decided_at"] is not None
                else None
            ),
        )

    @staticmethod
    def _checkout(row: sqlite3.Row) -> CheckoutOperation:
        result = (
            cast(dict[str, object], json.loads(str(row["result_json"])))
            if row["result_json"] is not None
            else None
        )
        return CheckoutOperation(
            operation_id=str(row["operation_id"]),
            draft_id=str(row["draft_id"]),
            approval_id=str(row["approval_id"]),
            status=CheckoutStatus(str(row["status"])),
            result=result,
            created_at=_parse_timestamp(str(row["created_at"])),
            updated_at=_parse_timestamp(str(row["updated_at"])),
        )

    @staticmethod
    def _downstream(row: sqlite3.Row) -> DownstreamRecord:
        return DownstreamRecord(
            draft_id=str(row["draft_id"]),
            calendar_status=DownstreamStatus(str(row["calendar_status"])),
            calendar_reference=row["calendar_reference"],
            calendar_error=row["calendar_error"],
            knowledge_status=DownstreamStatus(str(row["knowledge_status"])),
            knowledge_reference=row["knowledge_reference"],
            knowledge_error=row["knowledge_error"],
            handoff_status=DownstreamStatus(str(row["handoff_status"])),
            handoff_reference=row["handoff_reference"],
            handoff_error=row["handoff_error"],
        )


__all__ = [
    "ApprovalDecision",
    "ApprovalOutcome",
    "ApprovalSelection",
    "GroceryStore",
    "RecoveryOutcome",
]
