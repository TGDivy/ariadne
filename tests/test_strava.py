from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from fastmcp.exceptions import ToolError

from ariadne.mcp import strava as strava_tools
from ariadne.strava.client import StravaClient
from ariadne.strava.state import StravaTokens, StravaTokenState


class Response:
    def __init__(self, payload: object) -> None:
        self.payload = json.dumps(payload).encode()

    def __enter__(self) -> Response:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_authorization_url_requests_only_read_and_private_activity_scopes(
    tmp_path: Path,
) -> None:
    client = StravaClient(123, "secret", StravaTokenState(tmp_path / "tokens.sqlite3"))

    url = client.authorization_url("http://127.0.0.1:8765/callback", "nonce")
    query = parse_qs(urlsplit(url).query)

    assert query == {
        "client_id": ["123"],
        "redirect_uri": ["http://127.0.0.1:8765/callback"],
        "response_type": ["code"],
        "approval_prompt": ["auto"],
        "scope": ["read,activity:read_all"],
        "state": ["nonce"],
    }


def test_expired_token_is_refreshed_and_location_data_is_not_returned(
    tmp_path: Path,
) -> None:
    state = StravaTokenState(tmp_path / "tokens.sqlite3")
    state.save(
        StravaTokens("old", "refresh", 1, "read activity:read_all", athlete_id=9)
    )
    requests = []

    def opener(request, **_: object) -> Response:
        requests.append(request)
        if request.full_url.endswith("/oauth/token"):
            return Response(
                {
                    "access_token": "new",
                    "refresh_token": "new-refresh",
                    "expires_at": 99_999,
                }
            )
        return Response(
            [
                {
                    "id": 1,
                    "name": "Morning run",
                    "distance": 5_000.0,
                    "description": "private note",
                    "start_latlng": [51.5, -0.1],
                    "map": {"summary_polyline": "private trace"},
                }
            ]
        )

    client = StravaClient(123, "secret", state, opener=opener, clock=lambda: 10)

    result = client.activities(after=None, before=None, page=1, per_page=30)

    assert result["activities"] == [
        {"id": 1, "name": "Morning run", "distance": 5_000.0}
    ]
    assert parse_qs(requests[0].data.decode()) == {
        "client_id": ["123"],
        "client_secret": ["secret"],
        "refresh_token": ["refresh"],
        "grant_type": ["refresh_token"],
    }
    assert requests[1].get_header("Authorization") == "Bearer new"
    assert state.load() is not None
    assert state.load().refresh_token == "new-refresh"  # type: ignore[union-attr]


def test_authorization_exchange_normalizes_granted_scopes(tmp_path: Path) -> None:
    state = StravaTokenState(tmp_path / "tokens.sqlite3")

    def opener(*_: object, **__: object) -> Response:
        return Response(
            {
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_at": 99_999,
                "scope": "read,activity:read_all",
                "athlete": {"id": 9, "firstname": "Divy"},
            }
        )

    client = StravaClient(123, "secret", state, opener=opener)
    result = client.exchange_authorization_code("code")

    assert result["scope"] == "read activity:read_all"
    assert state.load() is not None


def test_mcp_strava_is_unavailable_without_toml_derived_environment(
    monkeypatch,
) -> None:
    for name in (
        "ARIADNE_STRAVA_CLIENT_ID",
        "ARIADNE_STRAVA_CLIENT_SECRET",
        "ARIADNE_STRAVA_STATE",
    ):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ToolError, match="not configured"):
        strava_tools.get_strava_athlete()


def test_mcp_activity_window_is_converted_to_strava_epoch_seconds(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Client:
        def activities(self, **kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {"activities": [], "count": 0}

    monkeypatch.setattr(strava_tools, "_client", lambda: Client())

    assert strava_tools.list_strava_activities(
        after="2026-08-01", before="2026-08-02T12:00:00+00:00"
    ) == {"activities": [], "count": 0}
    assert captured == {
        "after": 1_785_542_400,
        "before": 1_785_672_000,
        "page": 1,
        "per_page": 30,
    }


def test_mcp_rejects_an_inverted_activity_window() -> None:
    with pytest.raises(ToolError, match="after must be earlier"):
        strava_tools.list_strava_activities(after="2026-08-02", before="2026-08-01")
