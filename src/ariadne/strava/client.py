"""Small standard-library client for the private, read-only Strava surface."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .state import StravaTokens, StravaTokenState

API_ROOT = "https://www.strava.com/api/v3"
AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
REQUIRED_SCOPE = "activity:read_all"
REFRESH_MARGIN_SECONDS = 3_600


class StravaError(RuntimeError):
    """A safe, actionable Strava integration failure."""


class StravaAuthorizationRequired(StravaError):
    """The local OAuth exchange has not been completed yet."""


class StravaHTTPError(StravaError):
    """Provider response retained for Ariadne's redacted MCP diagnostics."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"Strava returned HTTP {status_code}.")
        self.status_code = status_code
        self.body = body


UrlOpen = Callable[..., Any]


def _payload(response: object) -> dict[str, Any] | list[dict[str, Any]]:
    raw = cast(Any, response).read().decode("utf-8")
    value = json.loads(raw)
    if not isinstance(value, (dict, list)):
        raise StravaError("Strava returned an unexpected response.")
    return value


def _require_string(payload: Mapping[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise StravaError("Strava returned incomplete authorization data.")
    return value


def _require_integer(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise StravaError("Strava returned incomplete authorization data.")
    return value


def _scopes(value: object) -> str:
    """Normalize either documented space-separated or legacy comma-separated scopes."""
    return " ".join(str(value).replace(",", " ").split())


def _safe_activity(activity: Mapping[str, Any]) -> dict[str, object]:
    """Expose training signals, never location traces or free-form notes."""
    fields = (
        "id",
        "name",
        "sport_type",
        "type",
        "start_date",
        "start_date_local",
        "timezone",
        "distance",
        "moving_time",
        "elapsed_time",
        "total_elevation_gain",
        "average_speed",
        "max_speed",
        "average_heartrate",
        "max_heartrate",
        "average_cadence",
        "average_watts",
        "max_watts",
        "weighted_average_watts",
        "kilojoules",
        "suffer_score",
        "calories",
        "workout_type",
        "trainer",
        "commute",
        "manual",
        "private",
        "achievement_count",
        "pr_count",
    )
    return {field: activity[field] for field in fields if field in activity}


class StravaClient:
    """Refresh credentials as needed and expose a deliberately narrow API."""

    def __init__(
        self,
        client_id: int,
        client_secret: str,
        state: StravaTokenState,
        *,
        opener: UrlOpen = urlopen,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.state = state
        self._opener = opener
        self._clock = clock

    def authorization_url(self, redirect_uri: str, state: str) -> str:
        query = urlencode(
            {
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "approval_prompt": "auto",
                "scope": "read,activity:read_all",
                "state": state,
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    def _request(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        form: Mapping[str, str | int] | None = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        data = urlencode(form).encode() if form is not None else None
        request = Request(url, data=data, method=method, headers=dict(headers or {}))
        if form is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        try:
            with self._opener(request, timeout=30) as response:
                return _payload(response)
        except HTTPError as error:
            raise StravaHTTPError(
                error.code, error.read().decode("utf-8", errors="replace")
            ) from error
        except URLError as error:
            raise StravaError("Strava could not be reached right now.") from error

    def _tokens_from_response(
        self, payload: Mapping[str, Any], *, fallback_scope: str = ""
    ) -> StravaTokens:
        athlete = payload.get("athlete")
        athlete_id = athlete.get("id") if isinstance(athlete, Mapping) else None
        return StravaTokens(
            access_token=_require_string(payload, "access_token"),
            refresh_token=_require_string(payload, "refresh_token"),
            expires_at=_require_integer(payload, "expires_at"),
            scope=_scopes(payload.get("scope") or fallback_scope),
            athlete_id=athlete_id if isinstance(athlete_id, int) else None,
        )

    def exchange_authorization_code(self, code: str) -> dict[str, object]:
        payload = self._request(
            TOKEN_URL,
            method="POST",
            form={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
        if not isinstance(payload, Mapping):
            raise StravaError("Strava returned an unexpected authorization response.")
        tokens = self._tokens_from_response(payload)
        if REQUIRED_SCOPE not in tokens.scope.split():
            raise StravaAuthorizationRequired(
                "Strava authorization did not grant activity:read_all. "
                "Approve that scope and try again."
            )
        self.state.save(tokens)
        athlete = payload.get("athlete")
        return {
            "athlete": _safe_athlete(athlete) if isinstance(athlete, Mapping) else None,
            "scope": tokens.scope,
            "expires_at": datetime.fromtimestamp(tokens.expires_at, UTC).isoformat(),
        }

    def _refresh(self, tokens: StravaTokens) -> StravaTokens:
        payload = self._request(
            TOKEN_URL,
            method="POST",
            form={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": tokens.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if not isinstance(payload, Mapping):
            raise StravaError("Strava returned an unexpected token refresh response.")
        refreshed = self._tokens_from_response(payload, fallback_scope=tokens.scope)
        self.state.save(refreshed)
        return refreshed

    def _access_token(self, *, force_refresh: bool = False) -> str:
        tokens = self.state.load()
        if tokens is None:
            raise StravaAuthorizationRequired(
                "Strava is configured but not connected yet. Run "
                "`python -m ariadne strava authorize`."
            )
        if force_refresh or tokens.expires_at <= self._clock() + REFRESH_MARGIN_SECONDS:
            tokens = self._refresh(tokens)
        if REQUIRED_SCOPE not in tokens.scope.split():
            raise StravaAuthorizationRequired(
                "Strava needs activity:read_all permission. Run "
                "`python -m ariadne strava authorize`."
            )
        return tokens.access_token

    def _get(self, path: str, query: Mapping[str, str | int] | None = None) -> Any:
        suffix = f"?{urlencode(query)}" if query else ""
        for attempt in range(2):
            token = self._access_token(force_refresh=attempt == 1)
            try:
                return self._request(
                    f"{API_ROOT}{path}{suffix}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            except StravaHTTPError as error:
                if error.status_code != 401 or attempt:
                    raise
        raise AssertionError("unreachable")

    def athlete(self) -> dict[str, object]:
        response = self._get("/athlete")
        if not isinstance(response, Mapping):
            raise StravaError("Strava returned an unexpected athlete response.")
        return _safe_athlete(response)

    def activities(
        self, *, after: int | None, before: int | None, page: int, per_page: int
    ) -> dict[str, object]:
        query: dict[str, int] = {"page": page, "per_page": per_page}
        if after is not None:
            query["after"] = after
        if before is not None:
            query["before"] = before
        response = self._get("/athlete/activities", query)
        if not isinstance(response, list):
            raise StravaError("Strava returned an unexpected activities response.")
        return {
            "activities": [
                _safe_activity(item) for item in response if isinstance(item, Mapping)
            ],
            "page": page,
            "per_page": per_page,
            "count": len(response),
        }

    def activity(self, activity_id: int) -> dict[str, object]:
        response = self._get(f"/activities/{activity_id}")
        if not isinstance(response, Mapping):
            raise StravaError("Strava returned an unexpected activity response.")
        return _safe_activity(response)

    def athlete_stats(self) -> dict[str, object]:
        tokens = self.state.load()
        if tokens is None or tokens.athlete_id is None:
            athlete = self.athlete()
            athlete_id = athlete.get("id")
            if not isinstance(athlete_id, int):
                raise StravaError("Strava did not provide an athlete identifier.")
        else:
            athlete_id = tokens.athlete_id
        response = self._get(f"/athletes/{athlete_id}/stats")
        if not isinstance(response, Mapping):
            raise StravaError("Strava returned an unexpected statistics response.")
        return {
            key: response[key]
            for key in (
                "recent_run_totals",
                "recent_ride_totals",
                "ytd_run_totals",
                "ytd_ride_totals",
                "all_run_totals",
                "all_ride_totals",
            )
            if key in response
        }


def _safe_athlete(athlete: Mapping[str, Any]) -> dict[str, object]:
    return {
        field: athlete[field]
        for field in ("id", "firstname", "lastname", "profile_medium", "profile")
        if field in athlete
    }
