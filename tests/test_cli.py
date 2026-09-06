import json
import logging
import os
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from caldav.lib.error import AuthorizationError, PutError, RateLimitError
from imapclient.exceptions import LoginError

import ariadne.cli as cli_module
import ariadne.service as service_module
from ariadne.behavior.fake_cli import BehaviorBackend
from ariadne.behavior.recording import STATE_ENVIRONMENT
from ariadne.calendar import CalendarConflict
from ariadne.cli import CliError, ProductionBackend, main
from ariadne.health import (
    IthacaAuthenticationError,
    IthacaNotFoundError,
    IthacaRequestError,
    IthacaResponseError,
    IthacaUnavailableError,
    WorkoutActivityType,
)
from ariadne.health.models import WorkoutSearchResponse


class RecordingMail:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls

    def _result(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, arguments))
        return {"operation": operation, "arguments": arguments}

    def search(
        self,
        query: str,
        *,
        since: str | None = None,
        before: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        return self._result(
            "mail.search",
            {"query": query, "since": since, "before": before, "limit": limit},
        )

    def read(self, value: str) -> dict[str, Any]:
        return self._result("mail.read", {"id": value})

    def read_thread(self, value: str) -> dict[str, Any]:
        return self._result("mail.thread", {"id": value})


class RecordingCalendar:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls

    def _result(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, arguments))
        return {"operation": operation, "arguments": arguments}

    def list_calendars(self) -> dict[str, Any]:
        return self._result("calendar.list", {})

    def search_events(
        self,
        start: str,
        end: str,
        *,
        query: str | None = None,
        calendar_ids: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        return self._result(
            "calendar.search",
            {
                "start": start,
                "end": end,
                "query": query,
                "calendar_ids": calendar_ids,
                "limit": limit,
            },
        )

    def read_event(self, value: str) -> dict[str, Any]:
        return self._result("calendar.read", {"id": value})

    def free_busy(
        self, start: str, end: str, *, calendar_ids: list[str] | None = None
    ) -> dict[str, Any]:
        return self._result(
            "calendar.availability",
            {"start": start, "end": end, "calendar_ids": calendar_ids},
        )

    def create_event(self, **kwargs: Any) -> dict[str, Any]:
        return self._result("calendar.create", kwargs)

    def update_event(self, value: str, **kwargs: Any) -> dict[str, Any]:
        return self._result("calendar.update", {"id": value, **kwargs})

    def delete_event(self, value: str, **kwargs: Any) -> dict[str, Any]:
        return self._result("calendar.delete", {"id": value, **kwargs})

    def respond_to_invitation(
        self,
        value: str,
        response: str,
        *,
        expected_etag: str | None = None,
    ) -> dict[str, Any]:
        return self._result(
            "calendar.respond",
            {"id": value, "response": response, "expected_etag": expected_etag},
        )


class RecordingHealth:
    def __init__(self, calls: list[tuple[str, dict[str, Any]]]) -> None:
        self.calls = calls

    def _result(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((operation, arguments))
        return {"operation": operation}

    def list_workouts(
        self,
        *,
        start: str,
        end: str,
        activity_types: Sequence[WorkoutActivityType] = (),
        limit: int = 20,
        cursor: str | None = None,
    ) -> Any:
        return self._result(
            "health.workouts.list",
            {
                "start": start,
                "end": end,
                "activity_types": list(activity_types),
                "limit": limit,
                "cursor": cursor,
            },
        )

    def summarize_workouts(
        self,
        *,
        start: str,
        end: str,
        activity_types: Sequence[WorkoutActivityType] = (),
    ) -> Any:
        return self._result(
            "health.workouts.summarize",
            {
                "start": start,
                "end": end,
                "activity_types": list(activity_types),
            },
        )

    def show_workout(self, workout_uuid: UUID) -> Any:
        return self._result(
            "health.workouts.show",
            {"workout_uuid": workout_uuid},
        )

    def list_sleep(
        self,
        *,
        start_date: date,
        end_date: date,
        limit: int = 20,
        cursor: str | None = None,
    ) -> Any:
        return self._result(
            "health.sleep.list",
            {
                "start_date": start_date,
                "end_date": end_date,
                "limit": limit,
                "cursor": cursor,
            },
        )

    def summarize_sleep(self, *, start_date: date, end_date: date) -> Any:
        return self._result(
            "health.sleep.summarize",
            {"start_date": start_date, "end_date": end_date},
        )

    def show_sleep(self, sleep_date: date) -> Any:
        return self._result("health.sleep.show", {"sleep_date": sleep_date})

    def latest_sleep(self) -> Any:
        return self._result("health.sleep.latest", {})


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.served = False
        self.config_payload: dict[str, Any] = {"status": "valid"}
        self.failure: Exception | None = None

    def _check_failure(self) -> None:
        if self.failure is not None:
            raise self.failure

    def config_check(self) -> dict[str, object]:
        self._check_failure()
        self.calls.append(("config.check", {}))
        return self.config_payload

    def config_show(self) -> dict[str, Any]:
        self._check_failure()
        self.calls.append(("config.show", {}))
        return self.config_payload

    def serve(self) -> None:
        self._check_failure()
        self.served = True

    @contextmanager
    def mail(self):
        self._check_failure()
        yield RecordingMail(self.calls)

    @contextmanager
    def calendar(self):
        self._check_failure()
        yield RecordingCalendar(self.calls)

    @contextmanager
    def health(self):
        self._check_failure()
        yield RecordingHealth(self.calls)


def _json_stdout(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    return json.loads(captured.out)


def test_nested_help_is_lazy_and_explains_repeated_and_clear_options(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unexpected_load(_: object = None) -> None:
        raise AssertionError("help must not load private configuration")

    monkeypatch.setattr(cli_module, "load_settings", unexpected_load)

    with pytest.raises(SystemExit) as raised:
        main(["calendar", "update", "--help"])

    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "--attendee EMAIL" in captured.out
    assert "repeat for more than one" in captured.out
    assert "--clear-attendees" in captured.out
    assert "--expected-etag" in captured.out
    assert captured.err == ""


def test_mail_commands_map_common_cli_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = RecordingBackend()

    main(
        [
            "mail",
            "search",
            "Project Alpha",
            "--since",
            "2026-08-01",
            "--before",
            "2026-09-01",
            "--limit",
            "7",
        ],
        backend=backend,
    )
    assert _json_stdout(capsys)["operation"] == "mail.search"
    main(["mail", "read", "mail:opaque"], backend=backend)
    assert _json_stdout(capsys)["operation"] == "mail.read"
    main(["mail", "thread", "mail:opaque"], backend=backend)
    assert _json_stdout(capsys)["operation"] == "mail.thread"

    assert backend.calls == [
        (
            "mail.search",
            {
                "query": "Project Alpha",
                "since": "2026-08-01",
                "before": "2026-09-01",
                "limit": 7,
            },
        ),
        ("mail.read", {"id": "mail:opaque"}),
        ("mail.thread", {"id": "mail:opaque"}),
    ]


def test_calendar_read_commands_map_repeated_selections(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = RecordingBackend()

    main(["calendar", "list"], backend=backend)
    _json_stdout(capsys)
    main(
        [
            "calendar",
            "search",
            "--start",
            "2026-09-01",
            "--end",
            "2026-09-08",
            "--query",
            "planning lunch",
            "--calendar-id",
            "calendar:one",
            "--calendar-id",
            "calendar:two",
            "--limit",
            "12",
        ],
        backend=backend,
    )
    _json_stdout(capsys)
    main(["calendar", "read", "event:one"], backend=backend)
    _json_stdout(capsys)
    main(
        [
            "calendar",
            "availability",
            "--start",
            "2026-09-01T09:00:00+01:00",
            "--end",
            "2026-09-01T17:00:00+01:00",
            "--calendar-id",
            "calendar:one",
        ],
        backend=backend,
    )
    _json_stdout(capsys)

    assert backend.calls == [
        ("calendar.list", {}),
        (
            "calendar.search",
            {
                "start": "2026-09-01",
                "end": "2026-09-08",
                "query": "planning lunch",
                "calendar_ids": ["calendar:one", "calendar:two"],
                "limit": 12,
            },
        ),
        ("calendar.read", {"id": "event:one"}),
        (
            "calendar.availability",
            {
                "start": "2026-09-01T09:00:00+01:00",
                "end": "2026-09-01T17:00:00+01:00",
                "calendar_ids": ["calendar:one"],
            },
        ),
    ]


def test_calendar_create_maps_repeated_values_and_free_status(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = RecordingBackend()

    main(
        [
            "calendar",
            "create",
            "--title",
            "Design review",
            "--start",
            "2026-09-03T10:00:00",
            "--end",
            "2026-09-03T11:00:00",
            "--calendar-id",
            "calendar:work",
            "--description",
            "Review the boundary",
            "--location",
            "Studio",
            "--attendee",
            "one@example.com",
            "--attendee",
            "two@example.com",
            "--timezone",
            "Europe/London",
            "--recurrence",
            "FREQ=WEEKLY;COUNT=3",
            "--alarm-minutes-before",
            "60",
            "--alarm-minutes-before",
            "10",
            "--status",
            "tentative",
            "--free",
        ],
        backend=backend,
    )
    _json_stdout(capsys)

    assert backend.calls == [
        (
            "calendar.create",
            {
                "title": "Design review",
                "start": "2026-09-03T10:00:00",
                "end": "2026-09-03T11:00:00",
                "calendar_id": "calendar:work",
                "description": "Review the boundary",
                "location": "Studio",
                "attendees": ["one@example.com", "two@example.com"],
                "timezone": "Europe/London",
                "recurrence": "FREQ=WEEKLY;COUNT=3",
                "alarms_minutes_before": [60, 10],
                "status": "tentative",
                "busy": False,
            },
        )
    ]


def test_calendar_update_delete_and_response_map_patch_semantics(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = RecordingBackend()

    main(
        [
            "calendar",
            "update",
            "event:one",
            "--scope",
            "series",
            "--expected-etag",
            '"etag-1"',
            "--clear-description",
            "--clear-location",
            "--clear-attendees",
            "--clear-recurrence",
            "--clear-alarms",
            "--free",
        ],
        backend=backend,
    )
    _json_stdout(capsys)
    main(
        [
            "calendar",
            "delete",
            "event:one",
            "--scope",
            "series",
            "--expected-etag",
            '"etag-2"',
        ],
        backend=backend,
    )
    _json_stdout(capsys)
    main(
        [
            "calendar",
            "respond",
            "event:invite",
            "accepted",
            "--expected-etag",
            '"etag-3"',
        ],
        backend=backend,
    )
    _json_stdout(capsys)

    assert backend.calls == [
        (
            "calendar.update",
            {
                "id": "event:one",
                "scope": "series",
                "expected_etag": '"etag-1"',
                "title": None,
                "start": None,
                "end": None,
                "description": "",
                "location": "",
                "attendees": [],
                "timezone": None,
                "recurrence": "",
                "alarms_minutes_before": [],
                "status": None,
                "busy": False,
            },
        ),
        (
            "calendar.delete",
            {
                "id": "event:one",
                "scope": "series",
                "expected_etag": '"etag-2"',
            },
        ),
        (
            "calendar.respond",
            {
                "id": "event:invite",
                "response": "accepted",
                "expected_etag": '"etag-3"',
            },
        ),
    ]


def test_health_workout_commands_map_typed_bounded_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = RecordingBackend()
    workout_uuid = "00000000-0000-0000-0000-000000000101"

    main(
        [
            "health",
            "workouts",
            "list",
            "--start",
            "2026-08-01",
            "--end",
            "2026-09-01",
            "--activity",
            "running",
            "--activity",
            "cycling",
            "--limit",
            "12",
            "--cursor",
            "next-page",
        ],
        backend=backend,
    )
    assert _json_stdout(capsys)["operation"] == "health.workouts.list"
    main(
        [
            "health",
            "workouts",
            "summarize",
            "--start",
            "2026-08-01T00:00:00+01:00",
            "--end",
            "2026-09-01T00:00:00+01:00",
            "--activity",
            "running",
        ],
        backend=backend,
    )
    _json_stdout(capsys)
    main(
        ["health", "workouts", "show", workout_uuid],
        backend=backend,
    )
    _json_stdout(capsys)

    assert backend.calls == [
        (
            "health.workouts.list",
            {
                "start": "2026-08-01",
                "end": "2026-09-01",
                "activity_types": [
                    WorkoutActivityType.RUNNING,
                    WorkoutActivityType.CYCLING,
                ],
                "limit": 12,
                "cursor": "next-page",
            },
        ),
        (
            "health.workouts.summarize",
            {
                "start": "2026-08-01T00:00:00+01:00",
                "end": "2026-09-01T00:00:00+01:00",
                "activity_types": [WorkoutActivityType.RUNNING],
            },
        ),
        (
            "health.workouts.show",
            {
                "workout_uuid": UUID(workout_uuid),
            },
        ),
    ]


def test_health_sleep_commands_map_typed_bounded_arguments(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = RecordingBackend()

    main(
        [
            "health",
            "sleep",
            "list",
            "--start",
            "2026-08-01",
            "--end",
            "2026-09-01",
            "--limit",
            "12",
            "--cursor",
            "next-page",
        ],
        backend=backend,
    )
    assert _json_stdout(capsys)["operation"] == "health.sleep.list"
    main(
        [
            "health",
            "sleep",
            "summarize",
            "--start",
            "2026-08-01",
            "--end",
            "2026-09-01",
        ],
        backend=backend,
    )
    _json_stdout(capsys)
    main(["health", "sleep", "show", "2026-08-31"], backend=backend)
    _json_stdout(capsys)
    main(["health", "sleep", "latest"], backend=backend)
    _json_stdout(capsys)

    assert backend.calls == [
        (
            "health.sleep.list",
            {
                "start_date": date(2026, 8, 1),
                "end_date": date(2026, 9, 1),
                "limit": 12,
                "cursor": "next-page",
            },
        ),
        (
            "health.sleep.summarize",
            {
                "start_date": date(2026, 8, 1),
                "end_date": date(2026, 9, 1),
            },
        ),
        ("health.sleep.show", {"sleep_date": date(2026, 8, 31)}),
        ("health.sleep.latest", {}),
    ]


def test_health_help_is_lazy_and_lists_only_the_useful_commands(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unexpected_load(_: object = None) -> None:
        raise AssertionError("help must not load private configuration")

    monkeypatch.setattr(cli_module, "load_settings", unexpected_load)

    with pytest.raises(SystemExit) as raised:
        main(["health", "--help"])
    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "{workouts,sleep}" in captured.out
    assert captured.err == ""

    with pytest.raises(SystemExit) as raised:
        main(["health", "workouts", "--help"])
    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "{list,summarize,show}" in captured.out
    assert "search" not in captured.out
    assert "List and summarize require --start and --end" in captured.out
    assert "WORKOUT_ID" in captured.out
    assert "returned by list" in captured.out
    assert "series" not in captured.out
    assert captured.err == ""

    with pytest.raises(SystemExit) as raised:
        main(["health", "workouts", "list", "--help"])
    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "--activity TYPE" in captured.out
    assert "Valid activity types:" in captured.out
    assert "--source" not in captured.out
    assert captured.err == ""

    with pytest.raises(SystemExit) as raised:
        main(["health", "workouts", "show", "--help"])
    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "WORKOUT_ID" in captured.out
    assert "snapshot" not in captured.out
    assert captured.err == ""

    with pytest.raises(SystemExit) as raised:
        main(["health", "sleep", "--help"])
    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "{list,summarize,show,latest}" in captured.out
    assert "SLEEP_DATE" in captured.out
    assert "returned by list or latest" in captured.out
    assert "recorded timezone" in captured.out
    assert captured.err == ""

    with pytest.raises(SystemExit) as raised:
        main(["health", "sleep", "show", "--help"])
    captured = capsys.readouterr()
    assert raised.value.code == 0
    assert "SLEEP_DATE" in captured.out
    assert "returned by list or latest" in captured.out
    assert captured.err == ""


def test_config_and_serve_dispatch_without_mixing_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = RecordingBackend()
    backend.config_payload = {"status": "valid", "name": "Iris"}

    main(["config", "check"], backend=backend)
    assert _json_stdout(capsys) == {"status": "valid", "name": "Iris"}
    main(["--pretty", "config", "show"], backend=backend)
    captured = capsys.readouterr()
    assert json.loads(captured.out) == backend.config_payload
    assert captured.out.startswith("{\n  ")
    assert captured.err == ""
    main(["serve"], backend=backend)
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""
    assert backend.served is True


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["mail", "search", "query", "--limit", "101"],
        ["calendar", "search", "--start", "2026-09-01"],
        ["calendar", "respond", "event:id", "maybe"],
        [
            "health",
            "workouts",
            "list",
            "--start",
            "2026-09-01",
            "--end",
            "2026-09-02",
            "--limit",
            "51",
        ],
        ["health", "workouts", "show", "not-a-uuid"],
        [
            "health",
            "sleep",
            "list",
            "--start",
            "2026-09-01",
            "--end",
            "2026-09-02",
            "--limit",
            "51",
        ],
        ["health", "sleep", "show", "yesterday"],
    ],
)
def test_argument_errors_are_short_machine_readable_json(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as raised:
        main(argv, backend=RecordingBackend())

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert raised.value.code == 2
    assert captured.out == ""
    assert payload["error"]["code"] == "invalid_arguments"
    assert payload["error"]["retryable"] is False
    assert len(payload["error"]["message"]) < 300


@pytest.mark.parametrize(
    ("error", "exit_code", "code", "retryable"),
    [
        (ValueError("bad interval"), 2, "invalid_request", False),
        (CalendarConflict("event changed"), 5, "calendar_conflict", False),
        (
            IthacaAuthenticationError("Ithaca rejected the configured token."),
            3,
            "health_authentication_failed",
            False,
        ),
        (IthacaNotFoundError("Workout not found."), 4, "health_not_found", False),
        (
            IthacaRequestError("The period is invalid."),
            2,
            "health_invalid_request",
            False,
        ),
        (
            IthacaUnavailableError("Ithaca is unavailable."),
            6,
            "health_unavailable",
            True,
        ),
        (
            IthacaResponseError("Ithaca response contract mismatch."),
            1,
            "health_invalid_response",
            False,
        ),
        (
            AuthorizationError("https://caldav.icloud.test/private"),
            3,
            "calendar_authentication_failed",
            False,
        ),
        (
            RateLimitError("https://caldav.icloud.test/private"),
            6,
            "calendar_rate_limited",
            True,
        ),
        (
            PutError("400 Bad Request token=provider-secret"),
            6,
            "calendar_unavailable",
            True,
        ),
        (OSError("socket detail"), 6, "provider_unavailable", True),
        (RuntimeError("implementation detail"), 1, "internal_error", False),
    ],
)
def test_runtime_failures_have_stable_exits_and_safe_errors(
    error: Exception,
    exit_code: int,
    code: str,
    retryable: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    backend = RecordingBackend()
    backend.failure = error

    with pytest.raises(SystemExit) as raised:
        main(["config", "show"], backend=backend)

    captured = capsys.readouterr()
    payload = json.loads(captured.err)
    assert raised.value.code == exit_code
    assert captured.out == ""
    assert payload["error"]["code"] == code
    assert payload["error"]["retryable"] is retryable
    assert "provider-secret" not in captured.err
    assert "implementation detail" not in captured.err


def test_error_messages_redact_environment_values_and_secret_syntax(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EXAMPLE_TOKEN", "top-secret-value")
    backend = RecordingBackend()
    backend.failure = CliError(
        "example_failure",
        "token=top-secret-value authorization: Bearer abc.def",
        2,
    )

    with pytest.raises(SystemExit):
        main(["config", "check"], backend=backend)

    captured = capsys.readouterr()
    assert "top-secret-value" not in captured.err
    assert "abc.def" not in captured.err
    assert "[REDACTED]" in captured.err


def test_output_limit_is_measured_in_bytes_and_never_prints_partial_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    backend = RecordingBackend()
    backend.config_payload = {"value": "é" * 20}
    monkeypatch.setattr(cli_module, "MAX_OUTPUT_BYTES", 30)

    with pytest.raises(SystemExit) as raised:
        main(["config", "show"], backend=backend)

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "output_too_large"


def _private_config(tmp_path: Path) -> tuple[Path, str, str]:
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True)
    routes = tmp_path / "routes.yaml"
    routes.write_text("version: 1\n", encoding="utf-8")
    username = "person@example.com"
    password = "private-app-password"
    config = tmp_path / "config.toml"
    config.write_text(
        f'''\
version = 1
human_name = "Example User"
vault = "{vault}"

[telegram]
bot_token = "private-telegram-token"
allowed_user_id = 7

[icloud]
username = "{username}"
app_password = "{password}"

[mail]
enabled = true
routes = "{routes}"

[calendar]
enabled = true
timezone = "Europe/London"
default_calendar = "Personal"
''',
        encoding="utf-8",
    )
    return config, username, password


def test_production_mail_loads_private_toml_credentials_only_on_demand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, username, password = _private_config(tmp_path)
    calls: list[tuple[str, object]] = []

    class FakeIMAP:
        def __init__(self, host: str, *, port: int, ssl: bool, timeout: int) -> None:
            calls.append(("connect", (host, port, ssl, timeout)))

        def __enter__(self) -> "FakeIMAP":
            return self

        def __exit__(self, *_: object) -> None:
            calls.append(("close", None))

        def login(self, selected_username: str, selected_password: str) -> None:
            calls.append(("login", (selected_username, selected_password)))

    class FakeReader:
        def __init__(self, client: FakeIMAP) -> None:
            calls.append(("reader", client))

        def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
            calls.append(("search", (query, kwargs)))
            return {"query": query, "results": []}

    monkeypatch.setattr(cli_module, "IMAPClient", FakeIMAP)
    monkeypatch.setattr(cli_module, "MailReader", FakeReader)

    main(
        ["--config", str(config), "mail", "search", "planning"],
        backend=ProductionBackend(config),
    )

    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"query": "planning", "results": []}
    assert captured.err == ""
    assert calls[0] == ("connect", ("imap.mail.me.com", 993, True, 30))
    assert calls[1] == ("login", (username, password))
    assert calls[-1] == ("close", None)
    assert password not in captured.out
    assert username not in captured.out


def test_production_calendar_uses_typed_config_without_secret_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, username, password = _private_config(tmp_path)
    captured_constructor: dict[str, Any] = {}
    monkeypatch.delenv("ARIADNE_ICLOUD_APP_PASSWORD", raising=False)

    class FakeCalendar:
        def __init__(
            self, selected_username: str, selected_password: str, **kwargs: object
        ) -> None:
            captured_constructor.update(
                username=selected_username, password=selected_password, **kwargs
            )

        def __enter__(self) -> "FakeCalendar":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def list_calendars(self) -> dict[str, Any]:
            return {"calendars": [], "timezone": "Europe/London"}

    monkeypatch.setattr(cli_module, "ICloudCalendar", FakeCalendar)

    main(["--config", str(config), "calendar", "list"])

    captured = capsys.readouterr()
    assert json.loads(captured.out)["calendars"] == []
    assert captured.err == ""
    assert captured_constructor == {
        "username": username,
        "password": password,
        "timezone": "Europe/London",
        "default_calendar": "Personal",
    }
    assert "ARIADNE_ICLOUD_APP_PASSWORD" not in os.environ


def test_production_health_loads_the_private_token_only_for_a_health_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, _, _ = _private_config(tmp_path)
    read_token = "health-read-token-0123456789abcdef"
    config.write_text(
        config.read_text(encoding="utf-8")
        + f'''\

[health]
enabled = true
api_url = "https://ithaca.example"
read_token = "{read_token}"
timezone = "Europe/London"
timeout_seconds = 19
''',
        encoding="utf-8",
    )
    captured_constructor: dict[str, Any] = {}
    captured_request: dict[str, Any] = {}

    class FakeClient:
        def search_workouts(self, **kwargs: Any) -> WorkoutSearchResponse:
            captured_request.update(kwargs)
            return WorkoutSearchResponse.model_validate(
                {
                    "schema_version": 1,
                    "projection_coverage": {
                        "scope": "requested_period_before_activity_filters",
                        "canonical_workout_count": 0,
                        "queryable_workout_count": 0,
                        "unqueryable_workout_count": 0,
                        "workouts_with_newer_unqueryable_snapshot_count": 0,
                    },
                    "items": [],
                    "next_cursor": None,
                }
            )

    def fake_client(api_url: str, token: str, **kwargs: Any) -> FakeClient:
        captured_constructor.update(api_url=api_url, token=token, **kwargs)
        return FakeClient()

    monkeypatch.setattr(cli_module, "IthacaClient", fake_client)

    main(
        [
            "--config",
            str(config),
            "health",
            "workouts",
            "list",
            "--start",
            "2026-09-01",
            "--end",
            "2026-10-01",
        ]
    )

    output = _json_stdout(capsys)
    assert output == {
        "period_data_coverage": {
            "canonical_workouts": 0,
            "queryable_workouts": 0,
            "unqueryable_workouts": 0,
            "workouts_with_newer_unqueryable_data": 0,
        },
        "workouts": [],
        "next_cursor": None,
    }
    assert captured_constructor == {
        "api_url": "https://ithaca.example/",
        "token": read_token,
        "timezone": "Europe/London",
        "timeout_seconds": 19,
    }
    assert captured_request["start"] == "2026-09-01"
    assert read_token not in json.dumps(output)


def test_production_provider_failures_do_not_echo_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config, username, password = _private_config(tmp_path)

    class FailingIMAP:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        def __enter__(self) -> "FailingIMAP":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def login(self, *_: object) -> None:
            raise LoginError(f"rejected {username} {password}")

    monkeypatch.setattr(cli_module, "IMAPClient", FailingIMAP)

    with pytest.raises(SystemExit) as raised:
        main(["--config", str(config), "mail", "search", "query"])

    captured = capsys.readouterr()
    assert raised.value.code == 3
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "mail_authentication_failed"
    assert username not in captured.err
    assert password not in captured.err


def test_missing_private_config_has_a_configuration_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing.toml"

    with pytest.raises(SystemExit) as raised:
        main(["--config", str(missing), "config", "check"])

    captured = capsys.readouterr()
    assert raised.value.code == 2
    assert captured.out == ""
    assert json.loads(captured.err)["error"]["code"] == "configuration_error"


def test_service_exports_selected_config_and_installed_cli_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_python = Path(service_module.sys.executable)
    executable = tmp_path / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.symlink_to(real_python)
    config = tmp_path / "config.toml"
    config.touch()
    monkeypatch.setattr(service_module.sys, "executable", str(executable))
    monkeypatch.setenv("PATH", "/usr/local/bin:/usr/bin")

    service_module._configure_cli_environment(config)
    service_module._configure_cli_environment(config)

    assert service_module.os.environ["ARIADNE_CONFIG"] == str(config.resolve())
    entries = service_module.os.environ["PATH"].split(service_module.os.pathsep)
    assert entries[0] == str(executable.parent.resolve())
    assert entries.count(str(executable.parent.resolve())) == 1


def test_non_service_commands_restore_the_callers_logging_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    previous = logging.root.manager.disable

    main(["config", "check"], backend=RecordingBackend())

    _json_stdout(capsys)
    assert logging.root.manager.disable == previous


def test_behavior_backend_uses_the_production_parser_and_records_cli_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = tmp_path / "calls.jsonl"
    calendar = tmp_path / "calendar.json"
    calendar.write_text(
        json.dumps(
            {
                "timezone": "UTC",
                "calendars": [
                    {
                        "id": "calendar:one",
                        "name": "Personal",
                        "supports_events": True,
                        "is_default": True,
                    }
                ],
                "events": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(STATE_ENVIRONMENT, str(calls))
    monkeypatch.setenv("ARIADNE_BEHAVIOR_CALENDAR", str(calendar))

    main(["mail", "search", "planning", "--limit", "3"], backend=BehaviorBackend())
    _json_stdout(capsys)
    main(["calendar", "list"], backend=BehaviorBackend())
    _json_stdout(capsys)

    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert recorded[0] == {
        "arguments": {
            "before": None,
            "limit": 3,
            "query": "planning",
            "since": None,
        },
        "tool": "cli.mail.search",
    }
    assert recorded[1]["tool"] == "cli.calendar.list"
