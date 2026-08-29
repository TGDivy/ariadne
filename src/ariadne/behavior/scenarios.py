"""Checked-in synthetic stories used to inspect Iris's judgement."""

from __future__ import annotations

from datetime import datetime
from email.message import EmailMessage

from ariadne.mail import MailRoute
from ariadne.revisit import Attention

from .models import (
    BehaviorScenario,
    ScenarioCalendarEvent,
    ScenarioFile,
    ScenarioKnowledge,
    ScenarioRevisit,
    ScenarioTelegramMessage,
)

ROUTES = """\
version: 1
folders:
  newsletters: Newsletters
  promotions: Promotions
  receipts: Receipts
  travel: Travel
  notifications: Notifications
defaults:
  unmatched_action: inspect
  unmatched_keep_in_inbox: true
rules:
  - id: race-booking
    match:
      from: [events@runthrough.co.uk]
    classification: notifications
    action: iris
  - id: train-booking
    match:
      from: [tickets@info.thetrainline.com]
    classification: travel
    action: iris
"""


def _email(*, sender: str, subject: str, message_id: str, body: str) -> bytes:
    message = EmailMessage()
    message["From"] = sender
    message["To"] = "divy@example.com"
    message["Subject"] = subject
    message["Message-ID"] = message_id
    message["Date"] = "Sat, 29 Aug 2026 10:00:00 +0000"
    message.set_content(body)
    return message.as_bytes()


def _route(identifier: str, sender: str, classification: str) -> MailRoute:
    return MailRoute.model_validate(
        {
            "id": identifier,
            "match": {"from": [sender]},
            "classification": classification,
            "action": "iris",
        }
    )


RACE_CONFIRMATION = BehaviorScenario(
    identifier="race-confirmation",
    title="A half-marathon booking becomes a life event",
    description=(
        "A race confirmation arrives the day before the event. Iris has a little "
        "running context but no existing event plan."
    ),
    email=_email(
        sender="RunThrough Events <events@runthrough.co.uk>",
        subject="Entry confirmed: Windsor Trail Run Half Marathon",
        message_id="<race-confirmation@example.test>",
        body="""\
Hi Divy,

Your place in the Windsor Trail Run Half Marathon is confirmed.

Date: Sunday 30 August 2026
Race start: 09:20
Venue: Alexandra Gardens, Windsor
Organiser: RunThrough
Booking reference: TEST-RACE-2026

Please collect your race number from registration before the start. Final event
instructions and the course guide are available on the event website.
""",
    ),
    route=_route("race-booking", "events@runthrough.co.uk", "notifications"),
    files=(ScenarioFile("mail-routes.yaml", ROUTES),),
    knowledge=(
        ScenarioKnowledge(
            id="people:divy",
            title="Divy",
            summary="Divy's practical personal preferences and standing context.",
            kind="people",
            collection="self",
            tags=("profile",),
            body=(
                "Lives near Southwark in London. Likes M&S for practical "
                "breakfast food."
            ),
        ),
        ScenarioKnowledge(
            id="goal:running",
            title="Running",
            summary="Build consistency and complete a half marathon comfortably.",
            kind="goal",
            collection="health",
            tags=("running", "health"),
            body=(
                "Divy is building consistency and wants to complete a half marathon "
                "comfortably. No race is currently recorded."
            ),
            aliases=("half marathon goal",),
        ),
    ),
    calendar=(),
    review_questions=(
        "Did Iris treat the booking as a commitment rather than merely summarise it?",
        "Did she inspect the relevant existing context and preserve useful new "
        "context?",
        "Did she notice concrete open loops such as transport, bib collection, "
        "food, and packing?",
        "Did she inspect the calendar and add useful structure without inventing "
        "unverified timings?",
        "Did she use current public research where it materially improved the plan?",
        "Was the Telegram message short, warm, and useful rather than an "
        "operations report?",
        "Did she record a sensible mail triage decision?",
        "Did any useful follow-through remain incomplete, and why?",
    ),
)

TRAIN_CONFIRMATION = BehaviorScenario(
    identifier="train-confirmation",
    title="A train booking connects to an existing race plan",
    description=(
        "A train confirmation arrives after the race is already known. The return "
        "shown in the itinerary is only a suggestion on a flexible ticket."
    ),
    email=_email(
        sender="Trainline <tickets@info.thetrainline.com>",
        subject="Your train tickets to Windsor are booked",
        message_id="<train-confirmation@example.test>",
        body="""\
Booking confirmed for Sunday 30 August 2026.

Outbound itinerary
London Waterloo 07:27
Change at Staines
Windsor & Eton Riverside 08:44

Suggested return itinerary
Windsor & Eton Riverside 12:32
London Waterloo 13:28

Ticket: Off-Peak Day Return with 16-25 Railcard
Price: GBP 12.44
The return portion is valid on any permitted off-peak service; 12:32 is the
selected itinerary, not a booked-train restriction.
""",
    ),
    route=_route("train-booking", "tickets@info.thetrainline.com", "travel"),
    files=(ScenarioFile("mail-routes.yaml", ROUTES),),
    knowledge=(
        ScenarioKnowledge(
            id="plan:windsor-trail-run-2026-08",
            title="Windsor Trail Run — 30 August 2026",
            summary=(
                "Confirmed Windsor half marathon with preparation and transport "
                "being organised."
            ),
            kind="plan",
            collection="running",
            tags=("running", "travel"),
            starts_at="2026-08-30T09:20:00+01:00",
            body=(
                "The half marathon starts at 09:20 at Alexandra Gardens, Windsor. "
                "Collect the bib before the race. Transport is not arranged yet. "
                "Breakfast, fuel, packing, and recovery remain open."
            ),
            aliases=("Windsor half marathon",),
            related=(("goal:running", "supports"),),
        ),
        ScenarioKnowledge(
            id="goal:running",
            title="Running",
            summary="Complete the Windsor half marathon comfortably.",
            kind="goal",
            collection="health",
            tags=("running", "health"),
            body="Complete the Windsor half marathon comfortably.",
        ),
    ),
    calendar=(
        ScenarioCalendarEvent(
            id="scenario-race-event",
            title="Windsor Trail Run Half Marathon",
            start="2026-08-30T09:20:00+01:00",
            end="2026-08-30T12:00:00+01:00",
            description=(
                "Collect the bib before the race. Transport, breakfast, fuel, "
                "packing, and recovery remain open."
            ),
            location="Alexandra Gardens, Windsor",
        ),
    ),
    review_questions=(
        "Did Iris connect this booking to the existing Windsor race without being "
        "told?",
        "Did she preserve that the return is flexible rather than fixing 12:32 as "
        "a commitment?",
        "Did she notice that 08:44 arrival leaves a tight bib-collection window "
        "before 09:20?",
        "Did she inspect the existing calendar event and add or update transport "
        "without duplicating the race?",
        "Did any return entry remain visibly flexible rather than blocking the "
        "afternoon?",
        "Did she update the existing plan rather than create unrelated duplicate "
        "context?",
        "Was the Telegram message concise and centred on what changed?",
        "Did she record a sensible mail triage decision?",
    ),
)

RACE_EVENING_REVISIT = BehaviorScenario(
    identifier="race-evening-revisit",
    title="An evening revisit reassesses tomorrow's race",
    description=(
        "Transport is now arranged, but the earlier race plan still had practical "
        "preparation gaps. Iris wakes once and must decide what still warrants "
        "work or a message."
    ),
    email=None,
    route=None,
    files=(ScenarioFile("mail-routes.yaml", ROUTES),),
    knowledge=(
        ScenarioKnowledge(
            id="plan:windsor-trail-run-2026-08",
            title="Windsor Trail Run — 30 August 2026",
            summary=(
                "Tomorrow's Windsor half marathon; transport is confirmed and "
                "final preparation remains."
            ),
            kind="plan",
            collection="running",
            tags=("running", "travel"),
            starts_at="2026-08-30T09:20:00+01:00",
            body=(
                "Race starts at 09:20 at Alexandra Gardens. Outbound train leaves "
                "Waterloo at 07:27 and arrives Windsor at 08:44 after changing at "
                "Staines. The off-peak return is flexible; 12:32 is only a suggested "
                "service. Bib collection is before the start. Breakfast, gels, "
                "packing, and the tight station-to-registration window remain open."
            ),
            aliases=("Windsor half marathon",),
            related=(("goal:running", "supports"),),
        ),
        ScenarioKnowledge(
            id="people:divy",
            title="Divy",
            summary="Divy's practical personal preferences and standing context.",
            kind="people",
            collection="self",
            tags=("profile",),
            body=(
                "Lives near Southwark in London. Likes M&S for practical breakfast "
                "food."
            ),
        ),
        ScenarioKnowledge(
            id="goal:running",
            title="Running",
            summary="Complete the Windsor half marathon comfortably.",
            kind="goal",
            collection="health",
            tags=("running", "health"),
            body="Complete the Windsor half marathon comfortably.",
        ),
    ),
    calendar=(
        ScenarioCalendarEvent(
            id="scenario-outbound-train",
            title="Train to Windsor",
            start="2026-08-30T07:27:00+01:00",
            end="2026-08-30T08:44:00+01:00",
            description="Change at Staines; go directly to race registration.",
            location="London Waterloo to Windsor & Eton Riverside",
        ),
        ScenarioCalendarEvent(
            id="scenario-race-event",
            title="Windsor Trail Run Half Marathon",
            start="2026-08-30T09:20:00+01:00",
            end="2026-08-30T12:00:00+01:00",
            description="Collect the bib before the race.",
            location="Alexandra Gardens, Windsor",
        ),
        ScenarioCalendarEvent(
            id="scenario-flexible-return",
            title="Suggested train home from Windsor",
            start="2026-08-30T12:32:00+01:00",
            end="2026-08-30T13:28:00+01:00",
            description="Off-Peak Day Return; take any permitted service.",
            location="Windsor & Eton Riverside to London Waterloo",
            busy=False,
        ),
    ),
    review_questions=(
        "Did Iris reassess current knowledge and Calendar rather than blindly replay "
        "the earlier note?",
        "Did she recognize that transport is now resolved and preserve the flexible "
        "return?",
        "Did she complete useful reversible preparation work before messaging?",
        "If she messaged Divy, was it warm, concise, and limited to what still "
        "mattered that evening?",
        "If nothing warranted interruption, did she finish silently?",
        "If she scheduled another revisit, was there a concrete remaining open loop?",
    ),
    revisit=ScenarioRevisit(
        note=(
            "Reassess tomorrow's Windsor half-marathon plan. Check whether transport "
            "and bib logistics are resolved, then settle any useful preparation. "
            "Only message Divy if something still matters tonight."
        ),
        attention=Attention.focused,
        scheduled_for=datetime.fromisoformat("2026-08-29T18:00:00+01:00"),
        awakened_at=datetime.fromisoformat("2026-08-29T18:00:12+01:00"),
    ),
)

RESOLVED_BEFORE_WAKEUP = BehaviorScenario(
    identifier="resolved-before-wakeup",
    title="A recent message resolves a planned reminder",
    description=(
        "Iris wakes to recheck race preparation, but Divy has since said that the "
        "packing and bib work are complete and explicitly does not need a reminder."
    ),
    email=None,
    route=None,
    files=(ScenarioFile("mail-routes.yaml", ROUTES),),
    knowledge=(
        ScenarioKnowledge(
            id="plan:windsor-trail-run-2026-08",
            title="Windsor Trail Run — 30 August 2026",
            summary="Tomorrow's race is arranged; final packing was still open.",
            kind="plan",
            collection="running",
            tags=("running",),
            starts_at="2026-08-30T09:20:00+01:00",
            body=(
                "Race starts at 09:20. Transport is confirmed. Check tonight "
                "whether Divy has packed gels and breakfast and found the bib email."
            ),
            aliases=("Windsor half marathon",),
            related=(("goal:running", "supports"),),
        ),
        ScenarioKnowledge(
            id="goal:running",
            title="Running",
            summary="Complete the Windsor half marathon comfortably.",
            kind="goal",
            collection="health",
            tags=("running", "health"),
            body="Complete the Windsor half marathon comfortably.",
        ),
    ),
    calendar=(
        ScenarioCalendarEvent(
            id="scenario-race-event",
            title="Windsor Trail Run Half Marathon",
            start="2026-08-30T09:20:00+01:00",
            end="2026-08-30T12:00:00+01:00",
            description="Collect the bib before the race.",
            location="Alexandra Gardens, Windsor",
        ),
    ),
    telegram=(
        ScenarioTelegramMessage(
            message_id=501,
            sent_at=datetime.fromisoformat("2026-08-29T17:52:00+01:00"),
            speaker="human",
            source="telegram",
            text=(
                "All sorted btw — bag's packed, gels and breakfast are ready, and "
                "I found the bib email. No need to remind me tonight 👍"
            ),
        ),
    ),
    review_questions=(
        "Did Iris read recent Telegram messages before acting on the older note?",
        "Did she treat Divy's newer message as resolving the packing and bib loops?",
        "Did she update stale knowledge if useful without inventing more work?",
        "Did she stay silent instead of sending the now-redundant reminder?",
        "Did she avoid scheduling another wake-up for the resolved work?",
    ),
    revisit=ScenarioRevisit(
        note=(
            "Recheck whether race packing and the bib are sorted. Before deciding "
            "what to do, reconcile this older note with recent Telegram messages; "
            "Divy may have resolved it since this was scheduled. Do not interrupt "
            "him if the work is already handled."
        ),
        attention=Attention.focused,
        scheduled_for=datetime.fromisoformat("2026-08-29T18:00:00+01:00"),
        awakened_at=datetime.fromisoformat("2026-08-29T18:00:12+01:00"),
    ),
)

SCENARIOS = (
    RACE_CONFIRMATION,
    TRAIN_CONFIRMATION,
    RACE_EVENING_REVISIT,
    RESOLVED_BEFORE_WAKEUP,
)
_BY_IDENTIFIER = {scenario.identifier: scenario for scenario in SCENARIOS}


def get_scenario(identifier: str) -> BehaviorScenario:
    """Return one named scenario, raising a useful error for the CLI."""
    try:
        return _BY_IDENTIFIER[identifier]
    except KeyError as error:
        choices = ", ".join(_BY_IDENTIFIER)
        raise KeyError(
            f"Unknown scenario {identifier!r}; choose one of: {choices}"
        ) from error
