from pathlib import Path

import pytest

from ariadne.config import ConfigurationError, Settings


def environment_for(workspace: Path) -> dict[str, str]:
    return {
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_ALLOWED_USER_ID": "12345",
        "ARIADNE_WORKSPACE": str(workspace),
    }


def test_settings_loads_a_valid_existing_workspace(tmp_path: Path) -> None:
    settings = Settings.from_environment(environment_for(tmp_path))

    assert settings.telegram_bot_token == "token"
    assert settings.allowed_user_id == 12345
    assert settings.workspace == tmp_path.resolve()


def test_settings_reports_missing_environment_variables() -> None:
    with pytest.raises(ConfigurationError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_environment({})


def test_settings_requires_a_positive_user_id(tmp_path: Path) -> None:
    environment = environment_for(tmp_path)
    environment["TELEGRAM_ALLOWED_USER_ID"] = "0"

    with pytest.raises(ConfigurationError, match="positive integer"):
        Settings.from_environment(environment)


def test_settings_requires_an_existing_workspace(tmp_path: Path) -> None:
    environment = environment_for(tmp_path / "missing")

    with pytest.raises(ConfigurationError, match="existing directory"):
        Settings.from_environment(environment)
