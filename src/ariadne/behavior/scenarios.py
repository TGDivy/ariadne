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
            id="divy",
            title="Divy",
            summary="Divy's practical personal preferences and standing context.",
            body=(
                "Lives near Southwark in London. Likes M&S for practical "
                "breakfast food."
            ),
        ),
        ScenarioKnowledge(
            id="running",
            title="Running",
            summary="Build consistency and complete a half marathon comfortably.",
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
            id="windsor-trail-run-2026-08",
            title="Windsor Trail Run — 30 August 2026",
            summary=(
                "Confirmed Windsor half marathon with preparation and transport "
                "being organised."
            ),
            body=(
                "The half marathon starts at 09:20 at Alexandra Gardens, Windsor. "
                "Collect the bib before the race. Transport is not arranged yet. "
                "Breakfast, fuel, packing, and recovery remain open."
            ),
            aliases=("Windsor half marathon",),
            links=("running",),
        ),
        ScenarioKnowledge(
            id="running",
            title="Running",
            summary="Complete the Windsor half marathon comfortably.",
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
            id="windsor-trail-run-2026-08",
            title="Windsor Trail Run — 30 August 2026",
            summary=(
                "Tomorrow's Windsor half marathon; transport is confirmed and "
                "final preparation remains."
            ),
            body=(
                "Race starts at 09:20 at Alexandra Gardens. Outbound train leaves "
                "Waterloo at 07:27 and arrives Windsor at 08:44 after changing at "
                "Staines. The off-peak return is flexible; 12:32 is only a suggested "
                "service. Bib collection is before the start. Breakfast, gels, "
                "packing, and the tight station-to-registration window remain open."
            ),
            aliases=("Windsor half marathon",),
            links=("running",),
        ),
        ScenarioKnowledge(
            id="divy",
            title="Divy",
            summary="Divy's practical personal preferences and standing context.",
            body=(
                "Lives near Southwark in London. Likes M&S for practical breakfast "
                "food."
            ),
        ),
        ScenarioKnowledge(
            id="running",
            title="Running",
            summary="Complete the Windsor half marathon comfortably.",
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
        created_at=datetime.fromisoformat("2026-08-29T17:30:00+01:00"),
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
            id="windsor-trail-run-2026-08",
            title="Windsor Trail Run — 30 August 2026",
            summary="Tomorrow's race is arranged; final packing was still open.",
            body=(
                "Race starts at 09:20. Transport is confirmed. Check tonight "
                "whether Divy has packed gels and breakfast and found the bib email."
            ),
            aliases=("Windsor half marathon",),
            links=("running",),
        ),
        ScenarioKnowledge(
            id="running",
            title="Running",
            summary="Complete the Windsor half marathon comfortably.",
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
        created_at=datetime.fromisoformat("2026-08-29T17:30:00+01:00"),
        scheduled_for=datetime.fromisoformat("2026-08-29T18:00:00+01:00"),
        awakened_at=datetime.fromisoformat("2026-08-29T18:00:12+01:00"),
    ),
)

CONFLICTING_NEEDS = BehaviorScenario(
    identifier="conflicting-needs",
    title="Divy's conflicting needs are both allowed to be true",
    description=(
        "Divy planned an intense long-weekend sprint but currently wants an "
        "unstructured evening. Iris has personal context that should help her "
        "respond without flattening either side of him."
    ),
    email=None,
    route=None,
    files=(ScenarioFile("mail-routes.yaml", ROUTES),),
    knowledge=(
        ScenarioKnowledge(
            id="divy-bramhecha",
            title="Divy Bramhecha",
            summary=(
                "Divy's own profile, including how his energy and working style "
                "can vary with context."
            ),
            aliases=("Divy", "me", "myself"),
            body=(
                "Divy genuinely enjoys ambitious, intense building sprints when "
                "the energy is there. He also needs real unstructured time and can "
                "be too quick to call that laziness. Both patterns are authentic; "
                "recent energy and the immediate moment matter more than a single "
                "permanent productivity rule."
            ),
        ),
        ScenarioKnowledge(
            id="watching-preferences",
            title="Watching Preferences",
            summary=(
                "Divy likes reflective, humane stories with quiet wonder and "
                "emotional intelligence."
            ),
            body=(
                "Perfect Days and Frieren are established taste anchors. Prefer "
                "one thoughtful recommendation over a large menu when one is "
                "actually requested."
            ),
            links=("divy-bramhecha",),
        ),
    ),
    calendar=(),
    review_questions=(
        "Did Iris inspect Divy's own context before making a personal judgement?",
        "Did she allow ambition and the desire to rest to both be genuine rather "
        "than declaring one the real Divy?",
        "Did she infer thoughtfully from the moment without turning uncertainty "
        "into an interrogation?",
        "Did the response sound like a natural message rather than coaching, a "
        "therapy script, or a productivity report?",
        "Did she avoid manufacturing a plan or private action merely to look useful?",
    ),
    telegram_prompt=(
        "I said this would be a huge hackathon weekend but honestly right now I "
        "just want to watch something and do nothing. idk maybe I'm being lazy"
    ),
)

KNOWN_PERSON_NEWS = BehaviorScenario(
    identifier="known-person-news",
    title="Good news about a known person lands as a human moment",
    description=(
        "Divy shares exciting news about Lily without asking for a task. Existing "
        "context explains why it matters and can be updated quietly."
    ),
    email=None,
    route=None,
    files=(ScenarioFile("mail-routes.yaml", ROUTES),),
    knowledge=(
        ScenarioKnowledge(
            id="lily",
            title="Lily",
            summary=(
                "A close friend of Divy's who was anxious about the final interview "
                "for a role she really wanted."
            ),
            aliases=("Lil",),
            body=(
                "Lily is a close friend. She recently reached the final interview "
                "for a role she really wanted and worried that she had performed "
                "badly. Divy cared about the outcome and wanted to support her."
            ),
        ),
        ScenarioKnowledge(
            id="divy-bramhecha",
            title="Divy Bramhecha",
            summary="Divy's own stable personal context.",
            aliases=("Divy", "me", "myself"),
            body="Divy values close friendships and shows up for his friends.",
        ),
    ),
    calendar=(),
    review_questions=(
        "Did Iris retrieve Lily's existing context before deciding how to respond?",
        "Did she recognise why the news mattered and share Divy's excitement?",
        "Did she update Lily's existing context rather than create a duplicate?",
        "Did routine remembering remain invisible in the message?",
        "Was the response short and natural rather than turning the moment into a "
        "CRM update or action plan?",
    ),
    telegram_prompt=(
        "Lily got the role!!! apparently she thought she bombed the last interview 😭"
    ),
)

TENTATIVE_AMBITION = BehaviorScenario(
    identifier="tentative-ambition",
    title="A possible ambition is remembered without becoming a goal",
    description=(
        "Divy voices a possible future ambition while explicitly saying he has "
        "not decided whether to commit to it."
    ),
    email=None,
    route=None,
    files=(ScenarioFile("mail-routes.yaml", ROUTES),),
    knowledge=(
        ScenarioKnowledge(
            id="divy-bramhecha",
            title="Divy Bramhecha",
            summary="Divy's own stable personal context and developing direction.",
            aliases=("Divy", "me", "myself"),
            body=(
                "Divy is training for a half marathon. No full-marathon goal has "
                "been established. His tentative ambitions belong under wishes "
                "and dreams until he explicitly makes one an active goal."
            ),
        ),
    ),
    calendar=(),
    review_questions=(
        "Did Iris read Divy's profile before interpreting the ambition?",
        "Did she preserve the possible marathon under Divy's wishes or current "
        "context rather than create an active goal?",
        "Did she ask whether Divy wants to make it a real goal?",
        "Was the response curious and natural rather than administrative?",
    ),
    telegram_prompt=(
        "maybe I want to run a full marathon next year? not sure if that's a real "
        "goal yet though"
    ),
)

NEW_PERSON_DAY = BehaviorScenario(
    identifier="new-person-day",
    title="A new person and Divy's lived experience go to different records",
    description=(
        "Divy recounts an enjoyable part of his day while also introducing a new "
        "friend and several facts that belong in her person record."
    ),
    email=None,
    route=None,
    files=(ScenarioFile("mail-routes.yaml", ROUTES),),
    knowledge=(
        ScenarioKnowledge(
            id="divy-bramhecha",
            title="Divy Bramhecha",
            summary="Divy's own stable personal context.",
            aliases=("Divy", "me", "myself"),
            body="Divy values warm friendships and enjoys getting to know people.",
        ),
    ),
    calendar=(),
    review_questions=(
        "Did Iris search for Maya before creating a new person?",
        "Did she create a person record containing Maya's relationship to Divy, "
        "background, and interests?",
        "Did the journal retain Divy's coffee, feelings, and experience without "
        "becoming the canonical store for facts about Maya?",
        "Did the response engage with the lovely human moment rather than narrate "
        "record keeping?",
    ),
    telegram_prompt=(
        "Had such a lovely coffee with Maya today. She's a new friend from my "
        "running club, grew up in Leeds and is obsessed with ceramics. I felt so "
        "comfortable around her, which was really nice."
    ),
)

SCENARIOS = (
    RACE_CONFIRMATION,
    TRAIN_CONFIRMATION,
    RACE_EVENING_REVISIT,
    RESOLVED_BEFORE_WAKEUP,
    CONFLICTING_NEEDS,
    KNOWN_PERSON_NEWS,
    TENTATIVE_AMBITION,
    NEW_PERSON_DAY,
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
