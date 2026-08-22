import hashlib
import hmac
import json

import pytest

from ariadne.github_webhook import (
    GitHubWebhookEvent,
    WebhookRejectedError,
    parse_verified_event,
)

SECRET = "test-secret"
ALLOWED_REPOSITORIES = frozenset({"TGDivy/nonce", "TGDivy/learning-workspace"})


def signed_headers(body: bytes, **headers: str) -> dict[str, str]:
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": "pull_request",
        "X-GitHub-Delivery": "delivery-1",
        **headers,
    }


def test_parses_a_signed_allowlisted_event() -> None:
    body = json.dumps(
        {"action": "opened", "repository": {"full_name": "TGDivy/nonce"}}
    ).encode()

    event = parse_verified_event(
        signed_headers(body), body, SECRET, ALLOWED_REPOSITORIES
    )

    assert event == GitHubWebhookEvent(
        delivery_id="delivery-1",
        event_name="pull_request",
        repository="TGDivy/nonce",
        action="opened",
    )


def test_rejects_an_invalid_signature() -> None:
    body = b'{"repository":{"full_name":"TGDivy/nonce"}}'
    headers = signed_headers(body, **{"X-Hub-Signature-256": "sha256=invalid"})

    with pytest.raises(WebhookRejectedError, match="did not match"):
        parse_verified_event(headers, body, SECRET, ALLOWED_REPOSITORIES)


def test_rejects_a_repository_outside_the_allowlist() -> None:
    body = json.dumps({"repository": {"full_name": "TGDivy/career-docs"}}).encode()

    with pytest.raises(WebhookRejectedError, match="allow-list"):
        parse_verified_event(signed_headers(body), body, SECRET, ALLOWED_REPOSITORIES)


def test_rejects_an_unsupported_event() -> None:
    body = json.dumps({"repository": {"full_name": "TGDivy/nonce"}}).encode()

    with pytest.raises(WebhookRejectedError, match="not supported"):
        parse_verified_event(
            signed_headers(body, **{"X-GitHub-Event": "issues"}),
            body,
            SECRET,
            ALLOWED_REPOSITORIES,
        )
