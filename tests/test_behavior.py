import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from ariadne.behavior import SCENARIOS, fake_mcp, get_scenario
from ariadne.behavior.runner import (
    BehaviorReport,
    RecordedMessage,
    TimelineEntry,
    _redacted_environment,
    render_report,
)
from ariadne.mcp import mcp as production_mcp
from ariadne.mcp import server as production_server


def test_catalog_has_unique_production_shaped_scenarios(tmp_path: Path) -> None:
    assert [scenario.identifier for scenario in SCENARIOS] == [
        "race-confirmation",
        "train-confirmation",
    ]
    assert len({scenario.identifier for scenario in SCENARIOS}) == len(SCENARIOS)

    for scenario in SCENARIOS:
        prompt = scenario.turn_input(tmp_path)
        assert "Owner-authorized mail task" in prompt
        assert f"ordered route {scenario.route.id!r}" in prompt
        assert str(tmp_path / "mail-routes.yaml") in prompt
        assert prompt.endswith("wait.")
        assert scenario.review_questions


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


async def test_fake_capabilities_keep_the_production_contract() -> None:
    production = {tool.name: tool for tool in await production_mcp.list_tools()}
    fake = {tool.name: tool for tool in await fake_mcp.mcp.list_tools()}

    assert tuple(fake) == (
        "runtime_status",
        "send_telegram_message",
        "prepare_files",
        "triage_current_mail",
    )
    for name, tool in fake.items():
        real = production[name]
        assert tool.description == real.description
        assert tool.parameters == real.parameters
        assert tool.output_schema == real.output_schema


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
    result = fake_mcp.triage_current_mail("notifications", "important", "keep_in_inbox")

    assert result["status"] == "recorded"
    assert [json.loads(line)["tool"] for line in calls.read_text().splitlines()] == [
        "send_telegram_message",
        "triage_current_mail",
    ]


def test_report_is_plain_reviewable_evidence() -> None:
    report = BehaviorReport(
        scenario="race-confirmation",
        model="gpt-test",
        reasoning_effort="medium",
        web_search="disabled",
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
        commits=("abc123 Update plan",),
        workspace_patch="--- a/plan.md\n+++ b/plan.md\n",
        review_questions=("Was it useful?",),
    )

    rendered = render_report(report)

    assert "# Behaviour run: race-confirmation" in rendered
    assert "**activity:** Reading context…" in rendered
    assert "### final_answer\n\nDone." in rendered
    assert "`send_telegram_message`" in rendered
    assert "`send_telegram_message`: completed" in rendered
    assert "abc123 Update plan" in rendered
    assert "Was it useful?" in rendered
