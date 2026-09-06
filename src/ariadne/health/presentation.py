"""Compact Iris-facing views over Ithaca's complete typed read contract."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from .client import IthacaClient
from .models import (
    PeriodProjectionCoverage,
    SleepDayDetail,
    SleepDaySummary,
    SleepEpisodeDetail,
    SleepLatestResponse,
    SleepSearchResponse,
    SleepShowResponse,
    SleepSummarizeResponse,
    WorkoutActivityType,
    WorkoutSearchResponse,
    WorkoutShowResponse,
    WorkoutSummarizeResponse,
)


def _json(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def _coverage(value: PeriodProjectionCoverage) -> dict[str, int]:
    return {
        "canonical_workouts": value.canonical_workout_count,
        "queryable_workouts": value.queryable_workout_count,
        "unqueryable_workouts": value.unqueryable_workout_count,
        "workouts_with_newer_unqueryable_data": (
            value.workouts_with_newer_unqueryable_snapshot_count
        ),
    }


def list_view(response: WorkoutSearchResponse) -> dict[str, Any]:
    workouts: list[dict[str, Any]] = []
    for item in response.items:
        workout = _json(item)
        workout["workout_id"] = workout.pop("workout_uuid")
        workout["activity"] = item.activity.name
        workout["source"] = item.source.name
        workout.pop("snapshot_id")
        newer_data_unavailable = workout.pop("newer_unqueryable_snapshot_exists")
        if newer_data_unavailable:
            workout["newer_data_unavailable"] = True
        workouts.append(workout)
    return {
        "period_data_coverage": _coverage(response.projection_coverage),
        "workouts": workouts,
        "next_cursor": response.next_cursor,
    }


def summarize_view(response: WorkoutSummarizeResponse) -> dict[str, Any]:
    activities: list[dict[str, Any]] = []
    for item in response.activities:
        activity = _json(item)
        activity["activity"] = item.activity.name
        activities.append(activity)
    return {
        "period": {
            "start_at": response.start_at.isoformat().replace("+00:00", "Z"),
            "end_at": response.end_at.isoformat().replace("+00:00", "Z"),
        },
        "period_data_coverage": _coverage(response.projection_coverage),
        "totals": _json(response.totals),
        "by_activity": activities,
    }


def show_view(response: WorkoutShowResponse) -> dict[str, Any]:
    workout = _json(response.workout)
    workout["workout_id"] = workout.pop("workout_uuid")
    workout["activity"] = response.workout.activity.name
    workout["source"] = response.workout.source.name
    workout.pop("available_series")

    components = workout.pop("activity_components")
    for item, model in zip(
        components["items"], response.workout.activity_components.items, strict=True
    ):
        item["activity"] = model.activity.name
    workout["components"] = components

    quality = workout.pop("quality")
    quality["issues"] = quality.pop("issue_codes")
    quality["newer_data_unavailable"] = (
        response.selection.newer_unqueryable_snapshot_exists
    )
    workout["data_quality"] = quality
    return workout


def _sleep_day_summary(day: SleepDaySummary) -> dict[str, Any]:
    return day.model_dump(mode="json", exclude={"issue_codes"})


def _sleep_episode_detail(episode: SleepEpisodeDetail) -> dict[str, Any]:
    return episode.model_dump(mode="json", exclude={"issue_codes"})


def _sleep_day_detail(day: SleepDayDetail) -> dict[str, Any]:
    return {
        "sleep_date": day.sleep_date.isoformat(),
        "time_zone_identifier": day.time_zone_identifier,
        "source_names": day.source_names,
        "total_asleep_seconds": day.total_asleep_seconds,
        "main_sleep": _sleep_episode_detail(day.main_sleep),
        "additional_sleep": [
            _sleep_episode_detail(episode) for episode in day.additional_sleep
        ],
    }


def _compact_sleep_day(day: SleepDayDetail) -> dict[str, Any]:
    main_sleep = day.main_sleep.model_dump(
        mode="json",
        exclude={
            "time_zone_identifier",
            "source_names",
            "stage_intervals",
            "issue_codes",
        },
    )
    return {
        "sleep_date": day.sleep_date.isoformat(),
        "time_zone_identifier": day.time_zone_identifier,
        "source_names": day.source_names,
        "main_sleep": main_sleep,
        "additional_sleep": {
            "episode_count": len(day.additional_sleep),
            "total_asleep_seconds": sum(
                episode.total_asleep_seconds for episode in day.additional_sleep
            ),
        },
        "total_asleep_seconds": day.total_asleep_seconds,
    }


def sleep_list_view(response: SleepSearchResponse) -> dict[str, Any]:
    return {
        "sleep_days": [_sleep_day_summary(day) for day in response.sleep_days],
        "next_cursor": response.next_cursor,
    }


def sleep_summarize_view(response: SleepSummarizeResponse) -> dict[str, Any]:
    summary = response.model_dump(
        mode="json",
        exclude={"schema_version", "projection", "start_date", "end_date"},
    )
    return {
        "period": {
            "start_date": response.start_date.isoformat(),
            "end_date": response.end_date.isoformat(),
        },
        **summary,
    }


def sleep_show_view(response: SleepShowResponse) -> dict[str, Any]:
    return _sleep_day_detail(response.sleep_day)


def sleep_latest_view(response: SleepLatestResponse) -> dict[str, Any]:
    return {
        "sleep_day": (
            _compact_sleep_day(response.sleep_day)
            if response.sleep_day is not None
            else None
        )
    }


class HealthQueries:
    """Expose only compact health queries that are useful in an Iris turn."""

    def __init__(self, client: IthacaClient) -> None:
        self.client = client

    def list_workouts(
        self,
        *,
        start: str,
        end: str,
        activity_types: Sequence[WorkoutActivityType] = (),
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return list_view(
            self.client.search_workouts(
                start=start,
                end=end,
                activity_types=activity_types,
                limit=limit,
                cursor=cursor,
            )
        )

    def summarize_workouts(
        self,
        *,
        start: str,
        end: str,
        activity_types: Sequence[WorkoutActivityType] = (),
    ) -> dict[str, Any]:
        return summarize_view(
            self.client.summarize_workouts(
                start=start,
                end=end,
                activity_types=activity_types,
            )
        )

    def show_workout(self, workout_uuid: UUID) -> dict[str, Any]:
        return show_view(self.client.show_workout(workout_uuid))

    def list_sleep(
        self,
        *,
        start_date: date,
        end_date: date,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return sleep_list_view(
            self.client.search_sleep(
                start_date=start_date,
                end_date=end_date,
                limit=limit,
                cursor=cursor,
            )
        )

    def summarize_sleep(self, *, start_date: date, end_date: date) -> dict[str, Any]:
        return sleep_summarize_view(
            self.client.summarize_sleep(start_date=start_date, end_date=end_date)
        )

    def show_sleep(self, sleep_date: date) -> dict[str, Any]:
        return sleep_show_view(self.client.show_sleep(sleep_date))

    def latest_sleep(self) -> dict[str, Any]:
        return sleep_latest_view(self.client.latest_sleep())
