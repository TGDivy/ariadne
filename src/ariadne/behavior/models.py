"""Small immutable models used by the behaviour scenario lab."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ariadne.mail import MailRoute, build_mail_turn_prompt, parse_metadata
from ariadne.mail.models import MailJob


@dataclass(frozen=True, slots=True)
class ScenarioFile:
    """One synthetic file present in the disposable Thread."""

    path: str
    content: str


@dataclass(frozen=True, slots=True)
class BehaviorScenario:
    """A production-shaped event with explicit points for human review."""

    identifier: str
    title: str
    description: str
    email: bytes
    route: MailRoute
    files: tuple[ScenarioFile, ...]
    review_questions: tuple[str, ...]

    def turn_input(self, workspace: Path) -> str:
        """Render the same owner input used by the production mail runtime."""
        metadata = parse_metadata(self.email)
        job = MailJob(
            job_id=f"INBOX:1:{self.identifier}",
            mailbox="INBOX",
            uidvalidity=1,
            uid=1,
            message_id=metadata.message_id,
            status="running",
            attempts=1,
            route_id=self.route.id,
            action=None,
            destination=None,
            classification=None,
            importance=None,
            suggested_action=None,
            draft_reply=None,
        )
        return build_mail_turn_prompt(
            job,
            metadata,
            self.email,
            route=self.route,
            move_after_iris=None,
            routes_path=workspace / "mail-routes.yaml",
            unmatched_keep_in_inbox=True,
        )
