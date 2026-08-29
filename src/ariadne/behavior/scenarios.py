"""Checked-in synthetic stories used to inspect Iris's judgement."""

from __future__ import annotations

from email.message import EmailMessage

from ariadne.mail import MailRoute

from .models import BehaviorScenario, ScenarioFile, ScenarioKnowledge

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
    files=(
        ScenarioFile("mail-routes.yaml", ROUTES),
        ScenarioFile(
            "People/Divy.md",
            """\
# Divy

Lives near Southwark in London. Likes M&S for practical breakfast food.
""",
        ),
        ScenarioFile(
            "Goals/Running.md",
            """\
# Running

Divy is building consistency and has mentioned wanting to complete a half
marathon comfortably. No race is currently recorded.
""",
        ),
    ),
    knowledge=(
        ScenarioKnowledge(
            id="person:divy",
            title="Divy",
            kind="person",
            body=(
                "Lives near Southwark in London. Likes M&S for practical "
                "breakfast food."
            ),
        ),
        ScenarioKnowledge(
            id="goal:running",
            title="Running",
            kind="goal",
            body=(
                "Divy is building consistency and wants to complete a half marathon "
                "comfortably. No race is currently recorded."
            ),
            aliases=("half marathon goal",),
        ),
    ),
    review_questions=(
        "Did Iris treat the booking as a commitment rather than merely summarise it?",
        "Did she inspect the relevant existing context and preserve useful new "
        "context?",
        "Did she notice concrete open loops such as transport, bib collection, "
        "food, and packing?",
        "Was the Telegram message short, warm, and useful rather than an "
        "operations report?",
        "Did she record a sensible mail triage decision?",
        "Which desired actions were impossible because the mail profile lacks "
        "capabilities?",
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
    files=(
        ScenarioFile("mail-routes.yaml", ROUTES),
        ScenarioFile(
            "Plans/Windsor Trail Run - 2026-08.md",
            """\
# Windsor Trail Run — 30 August 2026

- Half marathon starts at 09:20 at Alexandra Gardens, Windsor.
- Collect the bib from registration before the race.
- Transport is not arranged yet.
- Work out breakfast, race fuel, packing, and an optional recovery plan.
""",
        ),
        ScenarioFile(
            "Goals/Running.md",
            "# Running\n\nComplete the Windsor half marathon comfortably.\n",
        ),
    ),
    knowledge=(
        ScenarioKnowledge(
            id="plan:windsor-trail-run-2026-08",
            title="Windsor Trail Run — 30 August 2026",
            kind="plan",
            state="confirmed",
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
            kind="goal",
            body="Complete the Windsor half marathon comfortably.",
        ),
    ),
    review_questions=(
        "Did Iris connect this booking to the existing Windsor race without being "
        "told?",
        "Did she preserve that the return is flexible rather than fixing 12:32 as "
        "a commitment?",
        "Did she notice that 08:44 arrival leaves a tight bib-collection window "
        "before 09:20?",
        "Did she update the existing plan rather than create unrelated duplicate "
        "context?",
        "Was the Telegram message concise and centred on what changed?",
        "Did she record a sensible mail triage decision?",
    ),
)

SCENARIOS = (RACE_CONFIRMATION, TRAIN_CONFIRMATION)
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
