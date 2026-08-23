from pathlib import Path

import pytest
from openai_codex.generated.v2_all import ReasoningEffort
from pydantic import ValidationError

from ariadne.codex import CodexTurnSettings
from ariadne.config import Settings


@pytest.fixture
def settings_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_ID", "12345")
    monkeypatch.setenv("ARIADNE_VAULT", str(tmp_path))
    monkeypatch.setenv("ARIADNE_HUMAN_NAME", "Divy")
    return tmp_path


def test_settings_loads_a_valid_git_vault(settings_environment: Path) -> None:
    settings = Settings()

    assert settings.telegram_bot_token == "token"
    assert settings.allowed_user_id == 12345
    assert settings.vault == settings_environment.resolve()
    assert settings.codex_turn_settings == CodexTurnSettings(
        model="gpt-5.6-luna",
        effort=ReasoningEffort.low,
        web_search="disabled",
    )


def test_settings_accepts_explicit_codex_defaults(
    settings_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARIADNE_CODEX_MODEL", "gpt-test")
    monkeypatch.setenv("ARIADNE_REASONING_EFFORT", "high")
    monkeypatch.setenv("ARIADNE_WEB_SEARCH", "live")

    settings = Settings()

    assert settings.codex_turn_settings == CodexTurnSettings(
        model="gpt-test",
        effort=ReasoningEffort.high,
        web_search="live",
    )


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


def test_mail_is_opt_in_and_requires_a_complete_configuration(
    settings_environment: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert Settings().mail_settings is None

    monkeypatch.setenv("ICLOUD_USERNAME", "person@icloud.com")
    with pytest.raises(ValueError, match="must be set together"):
        _ = Settings().mail_settings


def test_mail_configuration_expands_external_paths(
    settings_environment: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    routes = tmp_path / "routes.yaml"
    routes.write_text("version: 1\n", encoding="utf-8")
    state = tmp_path / "state" / "mail.sqlite3"
    monkeypatch.setenv("ICLOUD_USERNAME", "person@icloud.com")
    monkeypatch.setenv("ICLOUD_APP_PASSWORD", "app-password")
    monkeypatch.setenv("ARIADNE_MAIL_ROUTES", str(routes))
    monkeypatch.setenv("ARIADNE_MAIL_STATE", str(state))

    configured = Settings().mail_settings

    assert configured is not None
    assert configured.routes == routes.resolve()
    assert configured.state == state.resolve()
    assert configured.app_password.get_secret_value() == "app-password"
