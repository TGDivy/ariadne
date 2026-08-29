import json
from pathlib import Path

import pytest

from ariadne.behavior import SCENARIOS, fake_mcp, get_scenario
from ariadne.behavior.runner import (
    BehaviorReport,
    RecordedMessage,
    TimelineEntry,
    render_report,
)
from ariadne.mcp import mcp as production_mcp


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
    assert "abc123 Update plan" in rendered
    assert "Was it useful?" in rendered
