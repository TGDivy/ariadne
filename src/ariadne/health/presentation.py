"""Compact Iris-facing views over Ithaca's complete typed read contract."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from .client import IthacaClient
from .models import (
    PeriodProjectionCoverage,
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


def search_view(response: WorkoutSearchResponse) -> dict[str, Any]:
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


class WorkoutQueries:
    """Expose only compact workout queries that are useful in an Iris turn."""

    def __init__(self, client: IthacaClient) -> None:
        self.client = client

    def search_workouts(
        self,
        *,
        start: str,
        end: str,
        activity_types: Sequence[WorkoutActivityType] = (),
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return search_view(
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
