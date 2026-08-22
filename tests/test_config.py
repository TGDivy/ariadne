from pathlib import Path

import pytest
from pydantic import ValidationError

from ariadne.config import Settings


@pytest.fixture
def settings_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "12345")
    monkeypatch.setenv("ARIADNE_WORKSPACE", str(tmp_path))
    return tmp_path


def test_settings_loads_a_valid_existing_workspace(settings_environment: Path) -> None:
    settings = Settings()

    assert settings.telegram_bot_token == "token"
    assert settings.allowed_user_id == 12345
    assert settings.workspace == settings_environment.resolve()


def test_settings_requires_a_positive_user_id(
    settings_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "0")

    with pytest.raises(ValidationError, match="greater than 0"):
        Settings()


def test_settings_requires_an_existing_workspace(
    settings_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARIADNE_WORKSPACE", str(settings_environment / "missing"))

    with pytest.raises(ValidationError, match="directory"):
        Settings()
