from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ariadne.config import Settings, settings_payload
from ariadne.grocery.capability import (
    APPROVAL_TTL_ENVIRONMENT,
    BASE_URL_ENVIRONMENT,
    CHECKOUT_ENVIRONMENT,
    ENVIRONMENT_NAMES,
    PROFILE_ENVIRONMENT,
    STATE_ENVIRONMENT,
    TOLERANCE_ENVIRONMENT,
)


def settings(tmp_path: Path, **grocery: Any) -> Settings:
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": 1,
        "human_name": "Example User",
        "vault": str(vault),
        "telegram": {"bot_token": "token", "allowed_user_id": 7},
    }
    if grocery:
        payload["browser"] = {"enabled": grocery.pop("browser_enabled", True)}
        payload["grocery"] = grocery
    return Settings.model_validate(payload)


def test_grocery_is_off_and_harmless_by_default(tmp_path: Path) -> None:
    resolved = settings(tmp_path)

    assert resolved.grocery.enabled is False
    assert resolved.grocery.checkout_enabled is False
    assert resolved.grocery.browser_profile == "personal"
    assert resolved.grocery.weighted_tolerance == Decimal("1.00")
    assert not any(name in resolved.mcp_environment for name in ENVIRONMENT_NAMES)


def test_enabling_grocery_publishes_only_its_own_private_names(
    tmp_path: Path,
) -> None:
    resolved = settings(
        tmp_path,
        enabled=True,
        browser_profile="shopping",
        weighted_tolerance="0.50",
        approval_ttl_seconds=600,
    )

    environment = resolved.mcp_environment
    assert environment[PROFILE_ENVIRONMENT] == "shopping"
    assert environment[TOLERANCE_ENVIRONMENT] == "0.50"
    assert environment[APPROVAL_TTL_ENVIRONMENT] == "600"
    assert environment[CHECKOUT_ENVIRONMENT] == "0"
    assert environment[BASE_URL_ENVIRONMENT].startswith("https://www.waitrose.com")
    assert environment[STATE_ENVIRONMENT].endswith("grocery.sqlite3")


def test_enabled_checkout_is_reported_separately(tmp_path: Path) -> None:
    resolved = settings(tmp_path, enabled=True, checkout_enabled=True)

    assert resolved.mcp_environment[CHECKOUT_ENVIRONMENT] == "1"


def test_grocery_requires_the_browser_capability(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="requires the \\[browser\\] capability"):
        settings(tmp_path, enabled=True, browser_enabled=False)


def test_checkout_cannot_be_enabled_on_its_own(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="requires \\[grocery\\].enabled"):
        settings(tmp_path, enabled=False, checkout_enabled=True)


def test_a_non_https_retailer_url_is_refused_outside_a_local_fixture(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValidationError, match="must use HTTPS"):
        settings(tmp_path, enabled=True, base_url="http://waitrose.example")

    local = settings(tmp_path, enabled=True, base_url="http://127.0.0.1:8080")
    assert str(local.grocery.base_url).startswith("http://127.0.0.1:8080")


def test_a_retailer_url_may_not_carry_credentials(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="must not contain credentials"):
        settings(tmp_path, enabled=True, base_url="https://user:pass@waitrose.example")


def test_limits_are_bounded(tmp_path: Path) -> None:
    with pytest.raises(ValidationError):
        settings(tmp_path, enabled=True, weighted_tolerance="100.00")
    with pytest.raises(ValidationError):
        settings(tmp_path, enabled=True, approval_ttl_seconds=5)
    with pytest.raises(ValidationError, match="simple identifier"):
        settings(tmp_path, enabled=True, browser_profile="../escape")


def test_a_tolerance_may_be_written_as_a_number_or_a_string(tmp_path: Path) -> None:
    numeric = settings(tmp_path, enabled=True, weighted_tolerance=2)
    written = settings(tmp_path, enabled=True, weighted_tolerance="2")

    assert numeric.grocery.weighted_tolerance == Decimal("2.00")
    assert written.grocery.weighted_tolerance == Decimal("2.00")


def test_config_show_reports_grocery_without_inventing_secrets(
    tmp_path: Path,
) -> None:
    payload = settings_payload(settings(tmp_path, enabled=True))

    assert payload["grocery"] == {
        "enabled": True,
        "state": str(settings(tmp_path, enabled=True).grocery.state),
        "browser_profile": "personal",
        "base_url": "https://www.waitrose.com/",
        "checkout_enabled": False,
        "weighted_tolerance": "1.00",
        "approval_ttl_seconds": 900,
    }
