"""Validated routing data and internal records shared by the mail runtime."""

from __future__ import annotations

from collections import Counter
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
TriageVerdict = Literal["routine", "important", "inspect"]
ClassifiedAction = Literal["move", "iris", "iris_then_move", "keep"]

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
    action: Literal["move", "iris", "iris_then_move"]


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
            if rule.action in {"move", "iris_then_move"}
            and rule.classification not in self.folders
        }
        if invalid:
            raise ValueError(
                "Move classifications need a folder: " + ", ".join(sorted(invalid))
            )
        duplicates = {
            route_id
            for route_id, count in Counter(rule.id for rule in self.rules).items()
            if count > 1
        }
        if duplicates:
            raise ValueError(
                "Mail route ids must be unique: " + ", ".join(sorted(duplicates))
            )
        return self

    def matches(self, message: MailMetadata) -> tuple[MailRoute, ...]:
        """Return every matching rule in configured order."""
        return tuple(rule for rule in self.rules if _matches(rule.match, message))

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


def cheap_triage(message: MailMetadata) -> TriageVerdict:
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


@dataclass(frozen=True, slots=True)
class MailClassification:
    """What ordered routing and cheap triage decide about one fresh message."""

    route: MailRoute | None
    matched_route_ids: tuple[str, ...]
    classification: str | None
    action: ClassifiedAction
    destination: str | None
    wakes_iris: bool
    triage: TriageVerdict | None

    @property
    def route_id(self) -> str | None:
        return self.route.id if self.route is not None else None


def classify_message(routes: MailRoutes, message: MailMetadata) -> MailClassification:
    """Decide the fate of one newly seen message, without contacting a model.

    Mail ingestion and the standalone classify command share this single
    decision so the reported outcome cannot drift from the applied one.
    """
    matched = routes.matches(message)
    matched_route_ids = tuple(rule.id for rule in matched)
    route = matched[0] if matched else None
    if route is not None:
        destination = (
            routes.folders[route.classification]
            if route.action in {"move", "iris_then_move"}
            else None
        )
        return MailClassification(
            route=route,
            matched_route_ids=matched_route_ids,
            classification=route.classification,
            action=route.action,
            destination=destination,
            wakes_iris=route.action != "move",
            triage=None,
        )
    triage = cheap_triage(message)
    if (
        routes.defaults.unmatched_action == "cheap_triage"
        and routes.defaults.unmatched_keep_in_inbox
        and triage == "routine"
    ):
        return MailClassification(
            route=None,
            matched_route_ids=matched_route_ids,
            classification="routine",
            action="keep",
            destination=None,
            wakes_iris=False,
            triage=triage,
        )
    return MailClassification(
        route=None,
        matched_route_ids=matched_route_ids,
        classification=None,
        action="iris",
        destination=None,
        wakes_iris=True,
        triage=triage,
    )


@dataclass(frozen=True, slots=True)
class MailJob:
    job_id: str
    account_key: str
    mailbox: str
    uidvalidity: int
    uid: int
    message_id: str | None
    status: JobStatus
    attempts: int
    route_id: str | None
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


@dataclass(frozen=True, slots=True)
class RuleLint:
    route_id: str
    action: str
    matches: int
    selected: int
    shadowed: int
    sample_subjects: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RouteOverlap:
    earlier_route_id: str
    later_route_id: str
    matches: int


@dataclass(frozen=True, slots=True)
class RouteLintReport:
    scanned: int
    unmatched: int
    rules: tuple[RuleLint, ...]
    overlaps: tuple[RouteOverlap, ...]
