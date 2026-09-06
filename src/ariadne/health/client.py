"""Small typed HTTP client for Ithaca's read-only workout API."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, TypeVar
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from .models import (
    APIModel,
    WorkoutActivityType,
    WorkoutSearchRequest,
    WorkoutSearchResponse,
    WorkoutShowRequest,
    WorkoutShowResponse,
    WorkoutSummarizeRequest,
    WorkoutSummarizeResponse,
)

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
ResponseModel = TypeVar("ResponseModel", bound=APIModel)
UrlOpen = Callable[..., Any]


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep the read bearer credential on the explicitly configured origin."""

    def redirect_request(self, *_: Any, **__: Any) -> None:
        return None


_NO_REDIRECT_OPEN = build_opener(_NoRedirectHandler()).open


class IthacaError(RuntimeError):
    """A safe failure at the configured Ithaca boundary."""


class IthacaAuthenticationError(IthacaError):
    """Ithaca rejected the configured read credential."""


class IthacaNotFoundError(IthacaError):
    """The requested workout data does not exist."""


class IthacaRequestError(IthacaError):
    """The caller supplied a request Ithaca cannot answer."""


class IthacaUnavailableError(IthacaError):
    """Ithaca could not be reached or temporarily failed."""


class IthacaResponseError(IthacaError):
    """Ithaca returned data outside its declared read contract."""


def _validation_message(error: ValidationError) -> str:
    issues: list[str] = []
    for issue in error.errors(include_url=False, include_input=False)[:3]:
        location = ".".join(str(part) for part in issue["loc"])
        prefix = f"{location}: " if location else ""
        issues.append(prefix + str(issue["msg"]))
    suffix = "; more validation errors omitted" if error.error_count() > 3 else ""
    return "Invalid workout request: " + "; ".join(issues) + suffix


def _error_detail(error: HTTPError) -> str | None:
    raw = error.read(MAX_ERROR_BYTES + 1)
    if len(raw) > MAX_ERROR_BYTES:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, Mapping):
        return None
    detail = payload.get("detail")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    if not isinstance(detail, list):
        return None
    issues: list[str] = []
    for item in detail[:3]:
        if not isinstance(item, Mapping) or not isinstance(item.get("msg"), str):
            continue
        raw_location = item.get("loc")
        location = (
            ".".join(str(part) for part in raw_location if part != "body")
            if isinstance(raw_location, list)
            else ""
        )
        issues.append(f"{location}: {item['msg']}" if location else item["msg"])
    return "; ".join(issues) or None


class IthacaClient:
    """Validate and perform bounded Workout Metrics Read v1 operations."""

    def __init__(
        self,
        api_url: str,
        read_token: str,
        *,
        timezone: str = "UTC",
        timeout_seconds: int = 30,
        opener: UrlOpen = _NO_REDIRECT_OPEN,
    ) -> None:
        self._api_url = api_url.rstrip("/")
        self._read_token = read_token
        self._timezone = ZoneInfo(timezone)
        self._timeout_seconds = timeout_seconds
        self._opener = opener

    def _boundary(self, value: str) -> datetime:
        candidate = value.strip()
        try:
            parsed = datetime.fromisoformat(
                candidate[:-1] + "+00:00" if candidate.endswith("Z") else candidate
            )
        except ValueError as error:
            raise IthacaRequestError(
                f"Invalid date or date-time {value!r}; use ISO 8601."
            ) from error
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=self._timezone)
        return parsed.astimezone(UTC)

    def _request_model(
        self, model: type[ResponseModel], **values: object
    ) -> ResponseModel:
        try:
            return model.model_validate(values)
        except ValidationError as error:
            raise IthacaRequestError(_validation_message(error)) from error

    def _post(
        self,
        path: str,
        payload: APIModel,
        response_model: type[ResponseModel],
    ) -> ResponseModel:
        body = json.dumps(
            payload.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        request = Request(
            f"{self._api_url}{path}",
            data=body,
            method="POST",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._read_token}",
                "Content-Type": "application/json",
            },
        )
        try:
            with self._opener(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            detail = _error_detail(error)
            if 300 <= error.code < 400:
                raise IthacaRequestError(
                    "Ithaca redirected the configured API URL; configure its final "
                    "URL directly."
                ) from error
            if error.code in {401, 403}:
                raise IthacaAuthenticationError(
                    "Ithaca rejected the configured health read token."
                ) from error
            if error.code == 404:
                raise IthacaNotFoundError(
                    detail or "Ithaca could not find that workout data."
                ) from error
            if 400 <= error.code < 500 and error.code != 429:
                raise IthacaRequestError(
                    detail or "Ithaca rejected the workout request."
                ) from error
            raise IthacaUnavailableError(
                "Ithaca could not complete that health read right now."
            ) from error
        except (URLError, TimeoutError, OSError) as error:
            raise IthacaUnavailableError(
                "The configured Ithaca health service could not be reached."
            ) from error
        if len(raw) > MAX_RESPONSE_BYTES:
            raise IthacaResponseError(
                "Ithaca's response exceeded Ariadne's bounded output limit."
            )
        try:
            decoded: object = json.loads(raw.decode("utf-8"))
            return response_model.model_validate(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
            raise IthacaResponseError(
                "Ithaca returned a response outside Workout Metrics Read v1."
            ) from error

    def search_workouts(
        self,
        *,
        start: str,
        end: str,
        activity_types: Sequence[WorkoutActivityType] = (),
        limit: int = 20,
        cursor: str | None = None,
    ) -> WorkoutSearchResponse:
        request = self._request_model(
            WorkoutSearchRequest,
            start_at=self._boundary(start),
            end_at=self._boundary(end),
            activity_types=list(activity_types),
            limit=limit,
            cursor=cursor,
        )
        return self._post("/v1/health/workouts/search", request, WorkoutSearchResponse)

    def summarize_workouts(
        self,
        *,
        start: str,
        end: str,
        activity_types: Sequence[WorkoutActivityType] = (),
    ) -> WorkoutSummarizeResponse:
        request = self._request_model(
            WorkoutSummarizeRequest,
            start_at=self._boundary(start),
            end_at=self._boundary(end),
            activity_types=list(activity_types),
        )
        return self._post(
            "/v1/health/workouts/summarize", request, WorkoutSummarizeResponse
        )

    def show_workout(
        self, workout_uuid: UUID, *, snapshot_id: UUID | None = None
    ) -> WorkoutShowResponse:
        request = self._request_model(
            WorkoutShowRequest,
            workout_uuid=workout_uuid,
            snapshot_id=snapshot_id,
        )
        return self._post("/v1/health/workouts/show", request, WorkoutShowResponse)
