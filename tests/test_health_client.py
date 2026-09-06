from __future__ import annotations

import json
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from urllib.error import HTTPError, URLError
from urllib.request import Request
from uuid import UUID

import pytest

import ariadne.health.client as client_module
from ariadne.health import (
    IthacaAuthenticationError,
    IthacaClient,
    IthacaNotFoundError,
    IthacaRequestError,
    IthacaResponseError,
    IthacaUnavailableError,
    WorkoutActivityType,
)
from ariadne.health.models import (
    SleepLatestResponse,
    SleepSearchResponse,
    SleepShowResponse,
    SleepSummarizeResponse,
    WorkoutSearchResponse,
    WorkoutShowResponse,
    WorkoutSummarizeResponse,
)
from ariadne.health.presentation import (
    list_view,
    show_view,
    sleep_latest_view,
    sleep_list_view,
    sleep_show_view,
    sleep_summarize_view,
    summarize_view,
)

WORKOUT_UUID = UUID("00000000-0000-0000-0000-000000000101")
SNAPSHOT_UUID = UUID("00000000-0000-0000-0000-000000000001")


class Response:
    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self, amount: int = -1) -> bytes:
        return self.body if amount < 0 else self.body[:amount]


class Opener:
    def __init__(self, *results: object) -> None:
        self.results = list(results)
        self.requests: list[tuple[Request, int]] = []

    def __call__(self, request: Request, *, timeout: int) -> Response:
        self.requests.append((request, timeout))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        assert isinstance(result, Response)
        return result


def coverage() -> dict[str, object]:
    return {
        "scope": "requested_period_before_activity_filters",
        "canonical_workout_count": 1,
        "queryable_workout_count": 1,
        "unqueryable_workout_count": 0,
        "workouts_with_newer_unqueryable_snapshot_count": 0,
    }


def search_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "projection_coverage": coverage(),
        "items": [
            {
                "workout_uuid": str(WORKOUT_UUID),
                "snapshot_id": str(SNAPSHOT_UUID),
                "newer_unqueryable_snapshot_exists": False,
                "activity": {"code": 37, "name": "Running"},
                "start_at": "2026-09-05T08:00:00Z",
                "end_at": "2026-09-05T08:10:00Z",
                "source": {"name": "Apple Watch"},
                "workout_time_seconds": 600,
                "elapsed_time_seconds": 600,
                "distance_meters": 2000,
                "average_pace_seconds_per_kilometer": 300,
                "average_heart_rate_bpm": 150,
                "route_available": True,
                "quality_issue_count": 0,
            }
        ],
        "next_cursor": "next-page",
    }


def summary_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "start_at": "2026-08-31T23:00:00Z",
        "end_at": "2026-09-30T23:00:00Z",
        "projection_coverage": coverage(),
        "totals": {
            "workout_count": 1,
            "workout_time_seconds": 600,
            "elapsed_time_seconds": 600,
        },
        "activities": [
            {
                "activity": {"code": 37, "name": "Running"},
                "workout_count": 1,
                "workout_time_seconds": 600,
                "elapsed_time_seconds": 600,
                "distance_meters": 2000,
                "average_pace_seconds_per_kilometer": 300,
            }
        ],
    }


def show_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "selection": {
            "snapshot_id": str(SNAPSHOT_UUID),
            "newer_unqueryable_snapshot_exists": False,
        },
        "workout": {
            "workout_uuid": str(WORKOUT_UUID),
            "activity": {"code": 37, "name": "Running"},
            "start_at": "2026-09-05T08:00:00Z",
            "end_at": "2026-09-05T08:10:00Z",
            "source": {"name": "Apple Watch"},
            "metrics": {
                "workout_time_seconds": 600,
                "elapsed_time_seconds": 600,
                "distance_meters": 2000,
                "active_energy_kilocalories": 200,
                "elevation_gain_meters": None,
                "average_heart_rate_bpm": 150,
                "average_power_watts": None,
                "average_running_cadence_steps_per_minute": 170,
                "average_cycling_cadence_revolutions_per_minute": None,
                "average_pace_seconds_per_kilometer": 300,
                "step_count": 1700,
                "effort_score": None,
            },
            "running_dynamics": None,
            "activity_components": {
                "availability": "not_recorded",
                "unavailable_reason": None,
                "items": [],
            },
            "distance_splits": {
                "availability": "available",
                "unavailable_reason": None,
                "target_distance_meters": 1000,
                "items": [],
            },
            "heart_rate_zones": {
                "availability": "not_recorded",
                "unavailable_reason": None,
                "profile_id": None,
                "resting_heart_rate_bpm": None,
                "maximum_heart_rate_bpm": None,
                "lower_boundaries_bpm": [],
                "eligible_duration_seconds": None,
                "covered_duration_seconds": None,
                "coverage_fraction": None,
                "duration_seconds_by_zone": None,
            },
            "route": {
                "availability": "available",
                "route_count": 1,
                "point_count": 42,
            },
            "diving": None,
            "available_series": ["heart_rate_bpm"],
            "quality": {"capture_complete": True, "issue_codes": []},
        },
    }


def sleep_projection() -> dict[str, object]:
    return {"schema_version": 2, "stale": True}


def sleep_day_summary_payload() -> dict[str, object]:
    return {
        "sleep_date": "2026-09-06",
        "time_zone_identifier": "Europe/London",
        "source_names": ["Apple Watch"],
        "main_sleep": {
            "sleep_started_at": "2026-09-05T22:00:00Z",
            "sleep_ended_at": "2026-09-06T06:00:00Z",
            "sleep_window_seconds": 28800,
            "total_asleep_seconds": 27000,
            "awake_seconds": 1800,
            "stages": {
                "asleep_unspecified_seconds": None,
                "core_seconds": 14400,
                "deep_seconds": 5400,
                "rem_seconds": 7200,
            },
        },
        "additional_sleep": {
            "episode_count": 1,
            "total_asleep_seconds": 1800,
        },
        "total_asleep_seconds": 28800,
        "issue_codes": ["internal_projection_warning"],
    }


def sleep_day_detail_payload() -> dict[str, object]:
    return {
        "sleep_date": "2026-09-06",
        "time_zone_identifier": "Europe/London",
        "source_names": ["Apple Watch"],
        "total_asleep_seconds": 28800,
        "main_sleep": {
            "sleep_started_at": "2026-09-05T22:00:00Z",
            "sleep_ended_at": "2026-09-06T06:00:00Z",
            "time_zone_identifier": "Europe/London",
            "source_names": ["Apple Watch"],
            "sleep_window_seconds": 28800,
            "total_asleep_seconds": 27000,
            "awake_seconds": 1800,
            "stages": {
                "asleep_unspecified_seconds": None,
                "core_seconds": 14400,
                "deep_seconds": 5400,
                "rem_seconds": 7200,
            },
            "stage_intervals": [
                {
                    "stage": "core",
                    "start_at": "2026-09-05T22:00:00Z",
                    "end_at": "2026-09-06T02:00:00Z",
                }
            ],
            "issue_codes": ["internal_projection_warning"],
        },
        "additional_sleep": [
            {
                "sleep_started_at": "2026-09-06T13:00:00Z",
                "sleep_ended_at": "2026-09-06T13:30:00Z",
                "time_zone_identifier": "Europe/London",
                "source_names": ["Apple Watch"],
                "sleep_window_seconds": 1800,
                "total_asleep_seconds": 1800,
                "awake_seconds": None,
                "stages": {
                    "asleep_unspecified_seconds": 1800,
                    "core_seconds": None,
                    "deep_seconds": None,
                    "rem_seconds": None,
                },
                "stage_intervals": [
                    {
                        "stage": "asleep_unspecified",
                        "start_at": "2026-09-06T13:00:00Z",
                        "end_at": "2026-09-06T13:30:00Z",
                    }
                ],
                "issue_codes": [],
            }
        ],
        "issue_codes": ["internal_projection_warning"],
    }


def sleep_search_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "projection": sleep_projection(),
        "sleep_days": [sleep_day_summary_payload()],
        "next_cursor": "next-sleep-page",
    }


def sleep_summary_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "start_date": "2026-09-01",
        "end_date": "2026-09-08",
        "projection": sleep_projection(),
        "days_with_sleep_data": 1,
        "averages": {
            "main_asleep_seconds": 27000,
            "total_asleep_seconds": 28800,
            "main_sleep_start": {
                "local_time": "23:00:00",
                "day_offset_from_sleep_date": -1,
            },
            "main_sleep_end": {
                "local_time": "07:00:00",
                "day_offset_from_sleep_date": 0,
            },
        },
        "variability": {
            "main_asleep_standard_deviation_seconds": 0,
            "total_asleep_standard_deviation_seconds": 0,
            "main_sleep_start_standard_deviation_seconds": 0,
            "main_sleep_end_standard_deviation_seconds": 0,
        },
        "additional_sleep": {
            "episode_count": 1,
            "total_asleep_seconds": 1800,
            "average_asleep_seconds_per_sleep_day": 1800,
        },
    }


def sleep_show_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "projection": sleep_projection(),
        "sleep_day": sleep_day_detail_payload(),
    }


def sleep_latest_payload(*, present: bool = True) -> dict[str, object]:
    return {
        "schema_version": 1,
        "projection": sleep_projection(),
        "sleep_day": sleep_day_detail_payload() if present else None,
    }


def test_search_uses_bearer_post_and_normalizes_configured_timezone() -> None:
    opener = Opener(Response(search_payload()))
    client = IthacaClient(
        "https://ithaca.example/",
        "private-read-token",
        timezone="Europe/London",
        timeout_seconds=17,
        opener=opener,
    )

    response = client.search_workouts(
        start="2026-09-01",
        end="2026-10-01",
        activity_types=(WorkoutActivityType.RUNNING,),
        limit=12,
    )

    assert isinstance(response, WorkoutSearchResponse)
    assert response.items[0].workout_uuid == WORKOUT_UUID
    request, timeout = opener.requests[0]
    assert request.full_url == "https://ithaca.example/v1/health/workouts/search"
    assert request.method == "POST"
    assert request.get_header("Authorization") == "Bearer private-read-token"
    assert request.get_header("Content-type") == "application/json"
    assert timeout == 17
    assert json.loads(request.data or b"") == {
        "start_at": "2026-08-31T23:00:00Z",
        "end_at": "2026-09-30T23:00:00Z",
        "activity_types": ["running"],
        "limit": 12,
    }


def test_summary_and_show_use_the_typed_v1_contract() -> None:
    opener = Opener(
        Response(summary_payload()),
        Response(show_payload()),
    )
    client = IthacaClient(
        "https://ithaca.example", "read-token", timezone="Europe/London", opener=opener
    )

    summary = client.summarize_workouts(start="2026-09-01", end="2026-10-01")
    shown = client.show_workout(WORKOUT_UUID, snapshot_id=SNAPSHOT_UUID)

    assert isinstance(summary, WorkoutSummarizeResponse)
    assert isinstance(shown, WorkoutShowResponse)
    assert [request.full_url.rsplit("/", 1)[-1] for request, _ in opener.requests] == [
        "summarize",
        "show",
    ]
    assert json.loads(opener.requests[1][0].data or b"") == {
        "workout_uuid": str(WORKOUT_UUID),
        "snapshot_id": str(SNAPSHOT_UUID),
    }


def test_sleep_reads_use_dates_bearer_authentication_and_a_bodyless_latest() -> None:
    opener = Opener(
        Response(sleep_search_payload()),
        Response(sleep_summary_payload()),
        Response(sleep_show_payload()),
        Response(sleep_latest_payload()),
    )
    client = IthacaClient(
        "https://ithaca.example/", "private-read-token", opener=opener
    )

    searched = client.search_sleep(
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 8),
        limit=12,
        cursor="sleep-cursor",
    )
    summarized = client.summarize_sleep(
        start_date=date(2026, 9, 1), end_date=date(2026, 9, 8)
    )
    shown = client.show_sleep(date(2026, 9, 6))
    latest = client.latest_sleep()

    assert isinstance(searched, SleepSearchResponse)
    assert isinstance(summarized, SleepSummarizeResponse)
    assert isinstance(shown, SleepShowResponse)
    assert isinstance(latest, SleepLatestResponse)
    assert [request.full_url for request, _ in opener.requests] == [
        "https://ithaca.example/v1/health/sleep/search",
        "https://ithaca.example/v1/health/sleep/summarize",
        "https://ithaca.example/v1/health/sleep/show",
        "https://ithaca.example/v1/health/sleep/latest",
    ]
    assert json.loads(opener.requests[0][0].data or b"") == {
        "start_date": "2026-09-01",
        "end_date": "2026-09-08",
        "limit": 12,
        "cursor": "sleep-cursor",
    }
    assert json.loads(opener.requests[2][0].data or b"") == {"sleep_date": "2026-09-06"}
    latest_request = opener.requests[3][0]
    assert latest_request.data is None
    assert latest_request.get_header("Content-type") is None
    assert all(
        request.get_header("Authorization") == "Bearer private-read-token"
        for request, _ in opener.requests
    )


def test_iris_views_remove_transport_plumbing_and_absent_metrics() -> None:
    listed = list_view(WorkoutSearchResponse.model_validate(search_payload()))
    summary = summarize_view(WorkoutSummarizeResponse.model_validate(summary_payload()))
    shown = show_view(WorkoutShowResponse.model_validate(show_payload()))

    assert listed["period_data_coverage"] == {
        "canonical_workouts": 1,
        "queryable_workouts": 1,
        "unqueryable_workouts": 0,
        "workouts_with_newer_unqueryable_data": 0,
    }
    first = listed["workouts"][0]
    assert first["workout_id"] == str(WORKOUT_UUID)
    assert first["activity"] == "Running"
    assert first["source"] == "Apple Watch"
    assert "snapshot_id" not in first
    assert "schema_version" not in listed

    assert summary["period"] == {
        "start_at": "2026-08-31T23:00:00Z",
        "end_at": "2026-09-30T23:00:00Z",
    }
    assert summary["by_activity"][0]["activity"] == "Running"
    assert "active_energy_kilocalories" not in summary["by_activity"][0]

    assert shown["workout_id"] == str(WORKOUT_UUID)
    assert shown["activity"] == "Running"
    assert shown["source"] == "Apple Watch"
    assert shown["components"]["availability"] == "not_recorded"
    assert shown["data_quality"] == {
        "capture_complete": True,
        "issues": [],
        "newer_data_unavailable": False,
    }
    assert "snapshot_id" not in shown
    assert "available_series" not in shown
    assert "elevation_gain_meters" not in shown["metrics"]


def test_sleep_views_keep_facts_and_hide_projection_quality_metadata() -> None:
    listed = sleep_list_view(SleepSearchResponse.model_validate(sleep_search_payload()))
    summarized = sleep_summarize_view(
        SleepSummarizeResponse.model_validate(sleep_summary_payload())
    )
    shown = sleep_show_view(SleepShowResponse.model_validate(sleep_show_payload()))
    latest = sleep_latest_view(
        SleepLatestResponse.model_validate(sleep_latest_payload())
    )
    empty_latest = sleep_latest_view(
        SleepLatestResponse.model_validate(sleep_latest_payload(present=False))
    )

    assert listed["next_cursor"] == "next-sleep-page"
    assert listed["sleep_days"][0]["sleep_date"] == "2026-09-06"
    assert (
        listed["sleep_days"][0]["main_sleep"]["stages"]["asleep_unspecified_seconds"]
        is None
    )
    assert summarized["period"] == {
        "start_date": "2026-09-01",
        "end_date": "2026-09-08",
    }
    assert summarized["days_with_sleep_data"] == 1
    assert shown["main_sleep"]["stage_intervals"][0]["stage"] == "core"
    assert "stage_intervals" not in latest["sleep_day"]["main_sleep"]
    assert latest["sleep_day"]["additional_sleep"] == {
        "episode_count": 1,
        "total_asleep_seconds": 1800,
    }
    assert empty_latest == {"sleep_day": None}

    public_json = json.dumps(
        {"list": listed, "summarize": summarized, "show": shown, "latest": latest}
    )
    assert "schema_version" not in public_json
    assert "projection" not in public_json
    assert "issue" not in public_json
    assert "stale" not in public_json


def http_error(status: int, payload: object) -> HTTPError:
    return HTTPError(
        "https://ithaca.example/private",
        status,
        "failure",
        hdrs=None,
        fp=BytesIO(json.dumps(payload).encode("utf-8")),
    )


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (http_error(401, {"detail": "private"}), IthacaAuthenticationError),
        (http_error(404, {"detail": "Workout not found."}), IthacaNotFoundError),
        (
            http_error(
                422,
                {"detail": [{"loc": ["body", "metric"], "msg": "Unknown metric."}]},
            ),
            IthacaRequestError,
        ),
        (http_error(302, {}), IthacaRequestError),
        (http_error(503, {"detail": "database-secret"}), IthacaUnavailableError),
        (URLError("offline"), IthacaUnavailableError),
    ],
)
def test_http_failures_have_safe_stable_types(
    failure: BaseException, expected: type[Exception]
) -> None:
    client = IthacaClient(
        "https://ithaca.example", "top-secret", opener=Opener(failure)
    )

    with pytest.raises(expected) as raised:
        client.show_workout(WORKOUT_UUID)

    assert "top-secret" not in str(raised.value)
    assert "database-secret" not in str(raised.value)


def test_local_period_validation_fails_before_network_access() -> None:
    opener = Opener()
    client = IthacaClient("https://ithaca.example", "read-token", opener=opener)

    with pytest.raises(IthacaRequestError, match="ISO 8601"):
        client.search_workouts(start="recently", end="2026-09-02")
    with pytest.raises(IthacaRequestError, match="end must be later"):
        client.summarize_workouts(start="2026-09-02", end="2026-09-01")
    with pytest.raises(IthacaRequestError, match="end_date must be later"):
        client.summarize_sleep(start_date=date(2026, 9, 2), end_date=date(2026, 9, 1))

    assert opener.requests == []


def test_unknown_or_oversized_responses_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid = IthacaClient(
        "https://ithaca.example",
        "read-token",
        opener=Opener(Response({"schema_version": 2})),
    )
    with pytest.raises(IthacaResponseError, match="outside"):
        invalid.search_workouts(start="2026-09-01", end="2026-09-02")

    monkeypatch.setattr(client_module, "MAX_RESPONSE_BYTES", 10)
    oversized = IthacaClient(
        "https://ithaca.example",
        "read-token",
        opener=Opener(Response({"long": "response"})),
    )
    with pytest.raises(IthacaResponseError, match="bounded"):
        oversized.search_workouts(start="2026-09-01", end="2026-09-02")


def test_default_transport_does_not_forward_the_token_across_redirects() -> None:
    requests: list[tuple[str, str | None]] = []

    class RedirectingHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header("Location", "/unexpected-redirect-target")
            self.end_headers()

        def log_message(self, *_: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), RedirectingHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            host, port = server.server_address
            client = IthacaClient(
                f"http://{host}:{port}",
                "private-read-token",
                timeout_seconds=2,
            )
            with pytest.raises(IthacaRequestError, match="redirected"):
                client.show_workout(WORKOUT_UUID)
        finally:
            server.shutdown()
            thread.join()

    assert requests == [
        (
            "/v1/health/workouts/show",
            "Bearer private-read-token",
        )
    ]
