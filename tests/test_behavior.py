import json
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from fastmcp.exceptions import ToolError

from ariadne.behavior import (
    SCENARIOS,
    fake_calendar,
    fake_knowledge,
    fake_mcp,
    get_scenario,
)
from ariadne.behavior.runner import (
    BehaviorReport,
    RecordedMessage,
    TimelineEntry,
    _redacted_environment,
    _write_fake_cli,
    _write_scenario,
    render_report,
)
from ariadne.knowledge.validation import validate_repository
from ariadne.mcp import mcp as production_mcp
from ariadne.mcp import server as production_server
from ariadne.telegram.history import TelegramMessageStore


def test_catalog_has_unique_production_shaped_scenarios(tmp_path: Path) -> None:
    assert [scenario.identifier for scenario in SCENARIOS] == [
        "race-confirmation",
        "train-confirmation",
        "race-evening-revisit",
        "resolved-before-wakeup",
        "conflicting-needs",
        "known-person-news",
        "tentative-ambition",
        "new-person-day",
    ]
    assert len({scenario.identifier for scenario in SCENARIOS}) == len(SCENARIOS)

    for scenario in SCENARIOS:
        prompt = scenario.turn_input(tmp_path)
        if scenario.telegram_prompt is not None:
            assert prompt == scenario.telegram_prompt
            assert scenario.profile_name == "telegram"
        elif scenario.revisit is None:
            assert scenario.route is not None
            assert "Ariadne speaking" in prompt
            assert f"ordered route {scenario.route.id!r}" in prompt
            assert str(tmp_path / "mail-routes.yaml") not in prompt
            assert "I give you permission to push" not in prompt
            assert "Use the message and your wider context" in prompt
            assert "<external_mail_evidence>" in prompt
        else:
            assert "Ariadne speaking" in prompt
            assert "Attention you selected: focused" in prompt
            assert "<earlier_iris_note>" in prompt
        assert scenario.review_questions
        assert scenario.knowledge
    assert SCENARIOS[0].calendar == ()
    assert len(SCENARIOS[1].calendar) == 1
    assert len(SCENARIOS[2].calendar) == 3
    assert len(SCENARIOS[3].calendar) == 1
    assert len(SCENARIOS[3].telegram) == 1
    assert SCENARIOS[4].telegram_prompt is not None
    assert SCENARIOS[5].telegram_prompt is not None
    assert SCENARIOS[6].telegram_prompt is not None
    assert SCENARIOS[7].telegram_prompt is not None


def test_scenario_knowledge_is_a_valid_collection(
    tmp_path: Path,
) -> None:
    for scenario in SCENARIOS:
        workspace = tmp_path / scenario.identifier
        workspace.mkdir()
        _write_scenario(scenario, workspace)

        report = validate_repository(workspace)

        assert report.records == len(scenario.knowledge)


def test_scenario_lookup_rejects_unknown_names() -> None:
    assert get_scenario("race-confirmation") is SCENARIOS[0]

    try:
        get_scenario("missing")
    except KeyError as error:
        assert "race-confirmation" in str(error)
    else:
        raise AssertionError("An unknown scenario should not resolve")


def test_model_process_does_not_inherit_service_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_PERSONAL_ACCESS_TOKEN", "not-for-the-scenario")
    monkeypatch.setenv("UNRELATED_VALUE", "ordinary")

    redacted = _redacted_environment()

    assert redacted["GITHUB_PERSONAL_ACCESS_TOKEN"] == ""
    assert redacted["TELEGRAM_BOT_TOKEN"] == ""
    assert "UNRELATED_VALUE" not in redacted


def test_disposable_cli_executable_uses_the_shared_parser(
    tmp_path: Path,
) -> None:
    calls = tmp_path / "calls.jsonl"
    executable = _write_fake_cli(tmp_path / "bin")
    environment = {
        **os.environ,
        fake_mcp.STATE_ENVIRONMENT: str(calls),
    }

    result = subprocess.run(
        [str(executable), "mail", "search", "race train", "--limit", "4"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )

    assert json.loads(result.stdout) == {
        "query": "race train",
        "results": [],
        "searched_folders": 1,
    }
    assert result.stderr == ""
    assert json.loads(calls.read_text())["tool"] == "cli.mail.search"


async def test_fake_capabilities_keep_the_production_contract() -> None:
    production = {tool.name: tool for tool in await production_mcp.list_tools()}
    fake = {tool.name: tool for tool in await fake_mcp.mcp.list_tools()}

    assert tuple(fake) == (
        "send_telegram_message",
        "read_recent_telegram_messages",
        "request_telegram_file_delivery",
        "record_current_mail_decision",
        "search_knowledge",
        "list_knowledge",
        "read_knowledge",
        "create_knowledge",
        "update_knowledge",
        "archive_knowledge",
        "schedule_wakeup",
        "list_wakeups",
        "update_wakeup",
        "cancel_wakeup",
    )
    for name, tool in fake.items():
        real = production[name]
        assert tool.description == real.description
        assert tool.parameters == real.parameters
        assert tool.output_schema == real.output_schema
        assert tool.annotations is not None
        assert tool.annotations.readOnlyHint is True
        assert tool.annotations.destructiveHint is False
        assert tool.annotations.idempotentHint is True
        assert tool.annotations.openWorldHint is False


def test_stdio_servers_do_not_start_the_networked_banner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    production_run = Mock()
    fake_run = Mock()
    monkeypatch.setattr(production_server.mcp, "run", production_run)
    monkeypatch.setattr(fake_mcp.mcp, "run", fake_run)

    production_server.main()
    fake_mcp.main()

    production_run.assert_called_once_with(show_banner=False)
    fake_run.assert_called_once_with(show_banner=False)


async def test_fake_capabilities_record_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = tmp_path / "calls.jsonl"
    monkeypatch.setenv(fake_mcp.STATE_ENVIRONMENT, str(calls))

    assert await fake_mcp.send_telegram_message("Race booked 😄") == [1001]
    result = fake_mcp.record_current_mail_decision(
        "notifications", "important", "keep_in_inbox"
    )

    assert result["status"] == "recorded"
    assert [json.loads(line)["tool"] for line in calls.read_text().splitlines()] == [
        "send_telegram_message",
        "record_current_mail_decision",
    ]


def test_fake_recent_telegram_history_is_seeded_and_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = tmp_path / "calls.jsonl"
    state = tmp_path / "telegram.sqlite3"
    scenario = SCENARIOS[3]
    store = TelegramMessageStore(state)
    for item in scenario.telegram:
        store.record(item.stored(chat_id=7))
    monkeypatch.setenv(fake_mcp.STATE_ENVIRONMENT, str(calls))
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "7")
    monkeypatch.setenv("ARIADNE_TELEGRAM_STATE", str(state))

    found = fake_mcp.read_recent_telegram_messages(
        "2026-08-29T17:00:00+01:00", speakers=["human"]
    )

    assert found["total"] == 1
    assert found["messages"] == [scenario.telegram[0].stored(7).public_payload()]
    assert json.loads(calls.read_text())["tool"] == "read_recent_telegram_messages"


async def test_fake_knowledge_is_seeded_and_mutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = tmp_path / "calls.jsonl"
    knowledge = tmp_path / "knowledge.json"
    knowledge.write_text(
        json.dumps({"records": [SCENARIOS[1].knowledge[0].payload()]}),
        encoding="utf-8",
    )
    monkeypatch.setenv(fake_mcp.STATE_ENVIRONMENT, str(calls))
    monkeypatch.setenv(fake_knowledge.KNOWLEDGE_ENVIRONMENT, str(knowledge))

    listing = fake_knowledge.list_knowledge()
    found = fake_knowledge.search_knowledge("Windsor", folder="event")
    result = found["results"][0]
    updated = fake_knowledge.update_knowledge(
        result["id"], body="Transport is arranged.", folder="event/travel"
    )

    assert listing["folders"] == [{"folder": "event", "record_count": 1}]
    assert found["count"] == 1
    assert updated["record"]["body"] == "Transport is arranged."
    assert updated["record"]["folder"] == "event/travel"
    assert [json.loads(line)["tool"] for line in calls.read_text().splitlines()] == [
        "list_knowledge",
        "search_knowledge",
        "update_knowledge",
    ]


def test_fake_knowledge_rejects_generated_id_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = tmp_path / "calls.jsonl"
    knowledge = tmp_path / "knowledge.json"
    existing = SCENARIOS[0].knowledge[1]
    knowledge.write_text(
        json.dumps({"records": [existing.payload()]}),
        encoding="utf-8",
    )
    monkeypatch.setenv(fake_mcp.STATE_ENVIRONMENT, str(calls))
    monkeypatch.setenv(fake_knowledge.KNOWLEDGE_ENVIRONMENT, str(knowledge))

    with pytest.raises(
        ToolError,
        match="Choose a more distinctive human-readable title",
    ):
        fake_knowledge.create_knowledge(
            title=existing.title,
            summary="A duplicate subject.",
            body="A duplicate subject.",
        )


async def test_fake_calendar_is_seeded_and_mutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = tmp_path / "calls.jsonl"
    calendar = tmp_path / "calendar.json"
    calendar.write_text(
        json.dumps(
            {
                "timezone": "Europe/London",
                "calendars": [
                    {
                        "id": "scenario-calendar",
                        "name": "Personal",
                        "supports_events": True,
                        "is_default": True,
                    }
                ],
                "events": [event.payload() for event in SCENARIOS[1].calendar],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(fake_mcp.STATE_ENVIRONMENT, str(calls))
    monkeypatch.setenv(fake_calendar.CALENDAR_ENVIRONMENT, str(calendar))

    found = fake_calendar.search_calendar_events("2026-08-30", "2026-08-31", "Windsor")
    created = fake_calendar.create_calendar_event(
        "Train to Windsor",
        "2026-08-30T07:27:00+01:00",
        "2026-08-30T08:44:00+01:00",
    )
    updated = fake_calendar.update_calendar_event(
        created["id"], description="Change at Staines."
    )

    assert found["events"][0]["id"] == "scenario-race-event"
    assert updated["description"] == "Change at Staines."
    assert [json.loads(line)["tool"] for line in calls.read_text().splitlines()] == [
        "cli.calendar.search",
        "cli.calendar.create",
        "cli.calendar.update",
    ]


def test_report_is_plain_reviewable_evidence() -> None:
    report = BehaviorReport(
        scenario="race-confirmation",
        model="gpt-test",
        reasoning_effort="medium",
        web_search="disabled",
        duration_seconds=12.34,
        token_usage={
            "cache_write_input_tokens": 0,
            "cached_input_tokens": 50,
            "input_tokens": 100,
            "output_tokens": 20,
            "reasoning_output_tokens": 10,
            "total_tokens": 120,
        },
        enabled_capabilities=("send_telegram_message",),
        timeline=(TimelineEntry("activity", "Reading context…"),),
        messages=(RecordedMessage("final_answer", "Done."),),
        capability_attempts=(
            {
                "server": "ariadne",
                "tool": "send_telegram_message",
                "status": "completed",
                "error": None,
            },
        ),
        capability_calls=(
            {"tool": "send_telegram_message", "arguments": {"text": "Hi"}},
        ),
        calendar_events=(
            {
                "title": "Windsor Trail Run",
                "start": "2026-08-30T09:20:00+01:00",
                "end": "2026-08-30T12:00:00+01:00",
                "location": "Alexandra Gardens",
                "busy": True,
            },
        ),
        commits=("abc123 Update plan",),
        workspace_patch="--- a/plan.md\n+++ b/plan.md\n",
        review_questions=("Was it useful?",),
    )

    rendered = render_report(report)

    assert "# Behaviour run: race-confirmation" in rendered
    assert "Duration: `12.3s`" in rendered
    assert "reasoning output tokens `10`" in rendered
    assert "**activity:** Reading context…" in rendered
    assert "### final_answer\n\nDone." in rendered
    assert "`send_telegram_message`" in rendered
    assert "`send_telegram_message`: completed" in rendered
    assert "## Calendar after turn" in rendered
    assert "**Windsor Trail Run**" in rendered
    assert "abc123 Update plan" in rendered
    assert "Was it useful?" in rendered
