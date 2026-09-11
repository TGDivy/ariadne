"""Typed public records and failures for browser control."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class BrowserError(Exception):
    """Base class for safe browser-control failures."""


class BrowserConfigurationError(BrowserError):
    """Browser control is unavailable or configured unsafely."""


class BrowserStateError(BrowserError):
    """A requested durable state transition is invalid."""


class BrowserBusyError(BrowserStateError):
    """Another task currently owns a persistent profile."""


class BrowserLeaseError(BrowserStateError):
    """A task lease is missing, expired, or has the wrong token."""


class BrowserReferenceError(BrowserStateError):
    """A semantic element reference is stale or unknown."""


class BrowserApprovalError(BrowserStateError):
    """A consequential action lacks a matching live confirmation."""


class BrowserUncertainError(BrowserStateError):
    """A consequential action may already have happened."""


class TaskState(StrEnum):
    ACTIVE = "active"
    TAKEOVER = "takeover"
    UNCERTAIN = "uncertain"
    RELEASED = "released"


class ActionOutcome(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class BrowserProfile:
    name: str
    user_data_path: Path
    created_at: datetime
    last_opened_at: datetime | None

    def public_payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "user_data_path": str(self.user_data_path),
            "created_at": self.created_at.isoformat(),
            "last_opened_at": (
                self.last_opened_at.isoformat() if self.last_opened_at else None
            ),
        }


@dataclass(frozen=True, slots=True)
class BrowserLease:
    task_id: str
    profile_name: str
    token: str
    state: TaskState
    expires_at: datetime
    page_revision: int
    uncertain_operation_id: str | None = None

    def public_payload(self, *, include_token: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "profile": self.profile_name,
            "state": self.state.value,
            "lease_expires_at": self.expires_at.isoformat(),
            "page_revision": self.page_revision,
            "uncertain_operation_id": self.uncertain_operation_id,
        }
        if include_token:
            payload["lease_token"] = self.token
        return payload


@dataclass(frozen=True, slots=True)
class JournalEntry:
    entry_id: int
    task_id: str | None
    occurred_at: datetime
    origin: str | None
    operation: str
    outcome: str
    detail: str | None
    artifact_path: Path | None

    def public_payload(self) -> dict[str, Any]:
        return {
            "id": self.entry_id,
            "task_id": self.task_id,
            "occurred_at": self.occurred_at.isoformat(),
            "origin": self.origin,
            "operation": self.operation,
            "outcome": self.outcome,
            "detail": self.detail,
            "artifact_path": str(self.artifact_path) if self.artifact_path else None,
        }


@dataclass(frozen=True, slots=True)
class ConsequentialAction:
    operation_id: str
    task_id: str
    status: ActionOutcome
    result: dict[str, Any] | None


__all__ = [
    "ActionOutcome",
    "BrowserApprovalError",
    "BrowserBusyError",
    "BrowserConfigurationError",
    "BrowserError",
    "BrowserLease",
    "BrowserLeaseError",
    "BrowserProfile",
    "BrowserReferenceError",
    "BrowserStateError",
    "BrowserUncertainError",
    "ConsequentialAction",
    "JournalEntry",
    "TaskState",
]
