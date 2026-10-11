"""Synthetic Thread records and screen data for the dev server.

Nothing here is real personal data; it exists so every app screen has
something believable to show without credentials or a model.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..app.schemas import (
    ActivityItemView,
    ActivityPage,
    EffectView,
    RecordRef,
    ScheduledView,
    UpcomingView,
)
from ..conversation.log import ConversationLog
from ..conversation.models import MessageDraft
from ..knowledge import KnowledgeMetadata
from ..knowledge.documents import render_document
from ..knowledge.paths import slug


@dataclass(frozen=True, slots=True)
class FixtureRecord:
    id: str
    title: str
    summary: str
    body: str
    folder: str = ""
    archived: bool = False
    links: tuple[str, ...] = ()


RECORDS = (
    FixtureRecord(
        "now",
        "Now",
        "Windsor half marathon on Saturday; Ljubljana trip with friends next week.",
        "Interview preparation has gone quiet for six days.",
    ),
    FixtureRecord(
        "half-marathon",
        "Half marathon",
        "Windsor on Saturday; the training block ends this week.",
        "## Plan\n\n- Taper runs on Tuesday and Thursday.\n- Bib collection opens "
        "08:30 at the Long Walk.\n\n## Fuel\n\n- [x] Gels bought\n- [ ] Pack the "
        "night before",
        folder="goal",
    ),
    FixtureRecord(
        "interview-readiness",
        "Interview readiness",
        "Systems-design gaps; 20-minute drills work better than long sessions.",
        "Short drills land better than evening marathons of study.",
        folder="goal",
    ),
    FixtureRecord(
        "cook-at-home",
        "Cook at home four times a week",
        "Batch-cooking on Sundays; groceries delivered.",
        "| Day | Meal |\n| --- | --- |\n| Sunday | Dal and rice |\n| Wednesday | "
        "Pasta |",
        folder="goal",
    ),
    FixtureRecord(
        "windsor-10k",
        "Windsor 10k",
        "Completed in 47:12 in May.",
        "A strong negative split.",
        folder="goal",
        archived=True,
    ),
    FixtureRecord(
        "maya",
        "Maya",
        "Friend from university who organises the Ljubljana trip.",
        "Vegetarian. Prefers quiet places for dinner.",
        folder="people/friends",
    ),
    FixtureRecord(
        "ljubljana-trip",
        "Ljubljana trip",
        "Six friends in Ljubljana from Friday to Sunday.",
        "| Time | Plan |\n| --- | --- |\n| Fri 14:05 | Flight to Ljubljana |\n| Fri "
        "17:00 | Check in at the hostel |\n| Fri 20:00 | Dinner, shortlist #2 |\n\n"
        "## Before you go\n\n- [x] Hostel confirmed\n- [ ] Airport transfer\n- [ ] "
        "Euros for the market",
        folder="plan",
        links=("maya",),
    ),
)


def write_thread(root: Path) -> None:
    """Create a Git-backed Thread with a local remote so writes can sync."""
    (root / "archive").mkdir(parents=True, exist_ok=True)
    for record in RECORDS:
        base = root / "archive" if record.archived else root
        destination = (
            base.joinpath(*record.folder.split("/")) if record.folder else base
        )
        destination.mkdir(parents=True, exist_ok=True)
        metadata = KnowledgeMetadata(
            id=record.id, title=record.title, summary=record.summary, links=record.links
        )
        (destination / f"{slug(record.title)}.md").write_bytes(
            render_document(metadata, record.body)
        )
    origin = root.parent / "thread-origin.git"
    _git("init", "--quiet", "--bare", str(origin), cwd=root.parent)
    _git("init", "--quiet", "--initial-branch=main", cwd=root)
    _git("config", "user.name", "Ariadne Dev", cwd=root)
    _git("config", "user.email", "dev@ariadne.local", cwd=root)
    _git("remote", "add", "origin", str(origin), cwd=root)
    _git("add", ".", cwd=root)
    _git("commit", "--quiet", "-m", "Dev fixtures", cwd=root)
    _git("push", "--quiet", "-u", "origin", "main", cwd=root)


def seed_conversation(log: ConversationLog, *, now: datetime) -> None:
    """Leave a little history so the first screen is not empty."""
    exchanges = (
        ("human", "Can you keep an eye on the Windsor race logistics?"),
        (
            "iris",
            "Yes. I'll watch the train and bib details and tell you if anything moves.",
        ),
        ("human", "Thanks!"),
    )
    for offset, (speaker, text) in enumerate(exchanges):
        log.append(
            MessageDraft(
                speaker="human" if speaker == "human" else "iris",
                origin="direct",
                kind="text",
                text=text,
                sent_at=now - timedelta(hours=3) + timedelta(minutes=offset),
            )
        )


class FixtureUpcoming:
    """What Upcoming looks like once activations exist."""

    def upcoming(self) -> UpcomingView:
        now = datetime.now(UTC)
        return UpcomingView(
            items=(
                ScheduledView(
                    id="act_tonight",
                    kind="wakeup",
                    title="Race transport and breakfast",
                    note="If the train is still unresolved, sort options and ask.",
                    due_at=now + timedelta(hours=2),
                    attention="focused",
                    status="pending",
                    about=(RecordRef(id="half-marathon", title="Half marathon"),),
                ),
                ScheduledView(
                    id="act_after_half",
                    kind="wakeup",
                    title="After the half",
                    note="Ask how it went; record the outcome.",
                    due_at=now + timedelta(days=2),
                    attention="light",
                    status="pending",
                    about=(RecordRef(id="half-marathon", title="Half marathon"),),
                ),
                ScheduledView(
                    id="daily",
                    kind="daily",
                    title="Daily initiative",
                    due_at=now + timedelta(hours=14),
                    status="pending",
                ),
                ScheduledView(
                    id="weekly",
                    kind="weekly",
                    title="Weekly digest",
                    due_at=now + timedelta(days=3),
                    status="pending",
                ),
            )
        )


class FixtureActivity:
    """What the Activity trail looks like once activations record it."""

    def page(self, *, before: str | None, limit: int) -> ActivityPage:
        if before is not None:
            return ActivityPage(items=(), has_more=False)
        now = datetime.now(UTC)
        items = (
            ActivityItemView(
                id="act_dinner",
                kind="delegated",
                title="Dinner shortlist",
                started_at=now - timedelta(minutes=4),
                outcome="running",
                account="Reading reviews",
            ),
            ActivityItemView(
                id="act_race_mail",
                kind="mail",
                title="Windsor race confirmed",
                started_at=now - timedelta(hours=4),
                finished_at=now - timedelta(hours=4) + timedelta(minutes=2),
                outcome="handed_off",
                account="Added the race to your calendar; transport is still open.",
                effects=(
                    EffectView(
                        type="calendar", label="Created “Windsor Half” Sat 09:20"
                    ),
                    EffectView(
                        type="knowledge",
                        label="Updated Half marathon",
                        record_id="half-marathon",
                    ),
                    EffectView(type="wakeup", label="Wake-up tonight (focused)"),
                ),
                about=(RecordRef(id="half-marathon", title="Half marathon"),),
            ),
            ActivityItemView(
                id="act_daily",
                kind="daily",
                title="Daily initiative",
                started_at=now - timedelta(hours=9),
                finished_at=now - timedelta(hours=9) + timedelta(minutes=6),
                outcome="quiet",
                account="Looked at interview prep and the trip; nothing timely.",
            ),
            ActivityItemView(
                id="act_routine",
                kind="mail",
                title="6 routine emails",
                started_at=now - timedelta(hours=11),
                finished_at=now - timedelta(hours=11),
                outcome="quiet",
                account="Newsletters and receipts filed. Nothing needed you.",
            ),
            ActivityItemView(
                id="act_daily_failed",
                kind="daily",
                title="Daily initiative didn't finish",
                started_at=now - timedelta(days=1, hours=1),
                finished_at=now - timedelta(days=1),
                outcome="failed",
                error="Timed out; the retry 30 minutes later completed.",
            ),
        )
        return ActivityPage(items=items[:limit], has_more=False)


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)
