import pytest

from ariadne.prompts import fill, render


def test_filling_refuses_to_leave_a_placeholder_unresolved() -> None:
    with pytest.raises(KeyError, match="human"):
        fill("Hello {{ human }}", {})


def test_rendering_substitutes_every_occurrence() -> None:
    rendered = render("grounding", human="Example User")

    assert "Example User's computer" in rendered
    assert "{{" not in rendered


def test_knowledge_instructions_make_context_work_explicit() -> None:
    rendered = render("knowledge", human="Example User")

    assert "Example User cares deeply about continuity" in rendered
    assert "Do not skip these checks because a message is casual" in rendered
    assert "If an input names a person, call `search_knowledge`" in rendered
    assert "call `read_knowledge` with every result returned" in rendered
    assert "ask Example User who they are and why they matter" in rendered
    assert "describes his feelings, energy, health, needs, priorities" in rendered
    assert "Search before every `create_knowledge` call" in rendered
    assert "Make the durable update before responding" in rendered
    assert "a new commitment, promise, booking" in rendered
    assert "under wishes and dreams" in rendered
    assert "surrounding context reveals something" in rendered
    assert "Do not store greetings, passing jokes" in rendered
    assert "journal records Example User's lived experience" in rendered
    assert "damaged personal relationship" in rendered
    assert "Current context" in rendered
    assert "{{" not in rendered


def test_background_prompts_specify_checks_and_interruptions() -> None:
    companion = render("companion", human="Example User")
    mail = render("mail", human="Example User")
    revisit = render("revisit", human="Example User")

    assert "when at least one of these is true" in companion
    assert "If none is true, finish silently" in companion
    assert "deliberately wakes Iris for companionship" in companion
    assert "race, interview, date, meetup" in companion
    assert "important goal repeatedly stalls" in companion
    assert "Search the wider mailbox" in mail
    assert "read prior Telegram messages about the same subjects" in mail
    assert "For every dated commitment" in mail
    assert "create or update its durable event record and Calendar entry" in mail
    assert "travel crosses timezones" in mail
    assert "correct the workspace mail-routing rules" in mail
    assert "is not proof of urgency" in mail
    assert "always call `read_recent_telegram_messages`" in revisit
    assert "exact `since` and `before` values" in revisit
    assert "Read every knowledge record named or referenced" in revisit
    assert "do not interpret silence as disinterest" in revisit
