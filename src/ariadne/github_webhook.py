"""Pure validation and parsing for a future GitHub learning webhook.

This module intentionally performs no network, repository, or Telegram action.
An HTTP adapter may use it only after a GitHub App, allow-list, and delivery
policy have been explicitly configured.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass

SUPPORTED_EVENTS = frozenset({"push", "pull_request", "workflow_run"})


class WebhookRejectedError(ValueError):
    """Raised when a webhook delivery does not meet the narrow trust boundary."""


@dataclass(frozen=True, slots=True)
class GitHubWebhookEvent:
    """A verified event limited to the metadata needed for routing."""

    delivery_id: str
    event_name: str
    repository: str
    action: str | None


def verify_signature(body: bytes, signature: str | None, secret: str) -> None:
    """Verify GitHub's SHA-256 HMAC header without logging sensitive values."""
    if not secret:
        raise WebhookRejectedError("Webhook secret must not be empty.")
    if signature is None or not signature.startswith("sha256="):
        raise WebhookRejectedError("Missing or malformed webhook signature.")

    expected = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise WebhookRejectedError("Webhook signature did not match.")


def parse_verified_event(
    headers: Mapping[str, str],
    body: bytes,
    secret: str,
    allowed_repositories: frozenset[str],
) -> GitHubWebhookEvent:
    """Validate and reduce a GitHub delivery to safe routing metadata."""
    normalized_headers = {key.lower(): value for key, value in headers.items()}
    verify_signature(body, normalized_headers.get("x-hub-signature-256"), secret)

    event_name = normalized_headers.get("x-github-event")
    delivery_id = normalized_headers.get("x-github-delivery")
    if event_name not in SUPPORTED_EVENTS:
        raise WebhookRejectedError("Webhook event is not supported.")
    if not delivery_id:
        raise WebhookRejectedError("Webhook delivery ID is required.")

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise WebhookRejectedError("Webhook body is not valid JSON.") from error
    if not isinstance(payload, dict):
        raise WebhookRejectedError("Webhook payload must be an object.")

    repository_data = payload.get("repository")
    repository = (
        repository_data.get("full_name") if isinstance(repository_data, dict) else None
    )
    if not isinstance(repository, str) or repository not in allowed_repositories:
        raise WebhookRejectedError("Repository is not in the allow-list.")

    action = payload.get("action")
    if action is not None and not isinstance(action, str):
        raise WebhookRejectedError("Webhook action must be a string.")

    return GitHubWebhookEvent(
        delivery_id=delivery_id,
        event_name=event_name,
        repository=repository,
        action=action,
    )
