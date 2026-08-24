"""Validated routing data and internal records shared by the mail runtime."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

REQUIRED_FOLDERS = frozenset(
    {"newsletters", "promotions", "receipts", "travel", "notifications"}
)

JobStatus = Literal["pending", "running", "done", "failed"]
Importance = Literal["routine", "important"]
SuggestedAction = Literal[
    "keep_in_inbox",
    "flag",
    "move_to_newsletters",
    "move_to_promotions",
    "move_to_receipts",
    "move_to_travel",
    "move_to_notifications",
]


class RouteMatch(BaseModel):
    """The deliberately small set of metadata predicates in route files."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: tuple[str, ...] = Field(default=(), alias="from")
    to: tuple[str, ...] = ()
    subject_contains_any: tuple[str, ...] = ()
    subject_starts_with_any: tuple[str, ...] = ()
    has_list_unsubscribe: bool | None = None
    unless_subject_contains_any: tuple[str, ...] = ()

    @model_validator(mode="after")
    def has_a_predicate(self) -> RouteMatch:
        if not any(
            (
                self.from_,
                self.to,
                self.subject_contains_any,
                self.subject_starts_with_any,
                self.has_list_unsubscribe is not None,
            )
        ):
            raise ValueError("A mail route match must contain a predicate.")
        return self


class MailRoute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    match: RouteMatch
    classification: str = Field(min_length=1)
    action: Literal["move", "iris"]


class MailDefaults(BaseModel):
    model_config = ConfigDict(extra="forbid")

    unmatched_action: Literal["inspect", "cheap_triage"] = "inspect"
    unmatched_keep_in_inbox: Literal[True] = True


class MailRoutes(BaseModel):
    """Validated ordered runtime routes loaded from outside the repository."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1]
    folders: dict[str, str]
    defaults: MailDefaults = MailDefaults()
    rules: tuple[MailRoute, ...]

    @model_validator(mode="after")
    def validate_folders_and_moves(self) -> MailRoutes:
        missing = REQUIRED_FOLDERS - self.folders.keys()
        if missing:
            raise ValueError(f"Missing mail folders: {', '.join(sorted(missing))}")
        if any(not name.strip() for name in self.folders.values()):
            raise ValueError("Mail folder names must not be empty.")
        invalid = {
            rule.classification
            for rule in self.rules
            if rule.action == "move" and rule.classification not in self.folders
        }
        if invalid:
            raise ValueError(
                "Move classifications need a folder: " + ", ".join(sorted(invalid))
            )
        return self

    def match(self, message: MailMetadata) -> MailRoute | None:
        """Return the first matching rule."""
        return next(
            (rule for rule in self.rules if _matches(rule.match, message)), None
        )


@dataclass(frozen=True, slots=True)
class MailMetadata:
    message_id: str
    sender: tuple[str, ...]
    recipients: tuple[str, ...]
    subject: str
    date: str
    has_list_unsubscribe: bool
    precedence: str
    auto_submitted: str


def _casefolded(values: Iterable[str]) -> frozenset[str]:
    return frozenset(value.casefold() for value in values)


def _matches(match: RouteMatch, message: MailMetadata) -> bool:
    subject = message.subject.casefold()
    if match.unless_subject_contains_any and any(
        value.casefold() in subject for value in match.unless_subject_contains_any
    ):
        return False
    if match.from_ and not (_casefolded(match.from_) & _casefolded(message.sender)):
        return False
    if match.to and not (_casefolded(match.to) & _casefolded(message.recipients)):
        return False
    if match.subject_contains_any and not any(
        value.casefold() in subject for value in match.subject_contains_any
    ):
        return False
    if match.subject_starts_with_any and not any(
        subject.startswith(value.casefold()) for value in match.subject_starts_with_any
    ):
        return False
    if (
        match.has_list_unsubscribe is not None
        and match.has_list_unsubscribe != message.has_list_unsubscribe
    ):
        return False
    return True


@dataclass(frozen=True, slots=True)
class MailJob:
    job_id: str
    mailbox: str
    uidvalidity: int
    uid: int
    message_id: str | None
    status: JobStatus
    action: str | None
    destination: str | None
    classification: str | None
    importance: str | None
    suggested_action: str | None
    draft_reply: str | None


@dataclass(frozen=True, slots=True)
class BackfillSummary:
    scanned: int
    move_matches: int
    moved: int
    iris_skipped: int
    unmatched: int


@dataclass(frozen=True, slots=True)
class RestoreSummary:
    found: int
    moved: int
