from pathlib import Path

import pytest
from pydantic import ValidationError

from ariadne.config import Settings


@pytest.fixture
def settings_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "12345")
    monkeypatch.setenv("ARIADNE_VAULT", str(tmp_path))
    return tmp_path


def test_settings_loads_a_valid_git_vault(settings_environment: Path) -> None:
    settings = Settings()

    assert settings.telegram_bot_token == "token"
    assert settings.allowed_user_id == 12345
    assert settings.vault == settings_environment.resolve()


def test_settings_requires_a_positive_user_id(
    settings_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "0")

    with pytest.raises(ValidationError, match="greater than 0"):
        Settings()


def test_settings_requires_an_existing_vault(
    settings_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARIADNE_VAULT", str(settings_environment / "missing"))

    with pytest.raises(ValidationError, match="directory"):
        Settings()


def test_settings_requires_a_git_vault(
    settings_environment: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    non_git_directory = tmp_path / "not-a-vault"
    non_git_directory.mkdir()
    monkeypatch.setenv("ARIADNE_VAULT", str(non_git_directory))

    with pytest.raises(ValidationError, match="Git repository"):
        Settings()
