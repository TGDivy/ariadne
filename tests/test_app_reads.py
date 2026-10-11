"""Views outside the conversation, and the contract the app is built from."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app_harness import RunningServer, start_server
from starlette.routing import Route, WebSocketRoute

from ariadne.app.contract import CONTRACT_PATH, OPERATIONS, render_contract
from ariadne.app.devices import DeviceStore
from ariadne.devkit.scripted import Say, script_of
from ariadne.revisit import Attention


@pytest.fixture
async def server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[RunningServer]:
    monkeypatch.setenv("HOME", str(tmp_path))
    running = await start_server(tmp_path / "instance", script_of((".*", [Say("ok")])))
    yield running
    await running.close()


async def test_goals_come_from_the_thread_goal_folder(server: RunningServer) -> None:
    goals = (await server.http.get("/v1/goals")).json()

    assert {goal["id"] for goal in goals["active"]} == {
        "half-marathon",
        "interview-readiness",
        "cook-at-home",
    }
    half = next(goal for goal in goals["active"] if goal["id"] == "half-marathon")
    assert half["summary"].startswith("Windsor on Saturday")
    assert goals["completed"] == [{"id": "windsor-10k", "title": "Windsor 10k"}]


async def test_library_browses_one_level_and_renders_records(
    server: RunningServer,
) -> None:
    root = (await server.http.get("/v1/library")).json()
    plans = (await server.http.get("/v1/library", params={"folder": "plan"})).json()
    record = (await server.http.get("/v1/records/ljubljana-trip")).json()
    missing_record = await server.http.get("/v1/records/nothing-here")
    missing_folder = await server.http.get("/v1/library", params={"folder": "nope"})

    assert {folder["folder"] for folder in root["folders"]} == {
        "goal",
        "people",
        "plan",
    }
    assert plans["records"] == [{"id": "ljubljana-trip", "title": "Ljubljana trip"}]
    assert "| Fri 14:05 | Flight to Ljubljana |" in record["body"]
    assert "- [ ] Airport transfer" in record["body"]
    assert record["links"] == [{"id": "maya", "title": "Maya"}]
    assert missing_record.status_code == 404
    assert missing_folder.status_code == 404


async def test_status_and_upcoming_report_what_is_enabled_and_scheduled(
    server: RunningServer,
) -> None:
    server.runtime.revisits.state.schedule(
        due_at=datetime(2030, 1, 1, tzinfo=UTC),
        note="Check the race packing\nand breakfast plan",
        attention=Attention.focused,
    )

    status = (await server.http.get("/v1/status")).json()
    upcoming = (await server.http.get("/v1/upcoming")).json()
    identity = (await server.http.get("/v1/identity")).json()
    activity = (await server.http.get("/v1/activity")).json()

    assert status["state"] == "idle"
    assert status["wakeups_upcoming"] == 1
    assert {"name": "Knowledge", "enabled": True} in status["sources"]
    assert {"name": "Mail", "enabled": False} in status["sources"]
    (wakeup,) = upcoming["items"]
    assert wakeup["kind"] == "wakeup"
    assert wakeup["title"] == "Check the race packing"
    assert wakeup["attention"] == "focused"
    assert identity == {"agent_name": "Iris", "human_name": "Divy", "avatar_url": None}
    assert activity == {"items": [], "has_more": False}


async def test_push_tokens_register_once_per_phone(server: RunningServer) -> None:
    token = "a1" * 32
    first = await server.http.post(
        "/v1/devices", json={"token": token, "environment": "sandbox"}
    )
    again = await server.http.post(
        "/v1/devices", json={"token": token.upper(), "environment": "production"}
    )
    invalid = await server.http.post(
        "/v1/devices", json={"token": "not-hex", "environment": "sandbox"}
    )

    assert first.status_code == again.status_code == 200
    assert invalid.status_code == 422
    stored = DeviceStore(server.state).all()
    assert [(item.token, item.environment) for item in stored] == [
        (token, "production")
    ]


def test_checked_in_contract_matches_the_server_models() -> None:
    assert CONTRACT_PATH.read_text(encoding="utf-8") == render_contract(), (
        "docs/app-api.json is stale; run `uv run python -m ariadne.app.contract`"
    )


async def test_every_served_route_is_in_the_contract(server: RunningServer) -> None:
    served = set()
    for route in server.runtime.app.routes:
        if isinstance(route, WebSocketRoute):
            continue
        assert isinstance(route, Route)
        path = route.path.replace("{message_id:int}", "{message_id}")
        served.update(
            (method, path) for method in route.methods or () if method != "HEAD"
        )

    assert served == {(operation.method, operation.path) for operation in OPERATIONS}
