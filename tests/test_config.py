import json
from pathlib import Path

import pytest
from openai_codex.generated.v2_all import ReasoningEffort
from pydantic import ValidationError

from ariadne.codex import CodexTurnSettings
from ariadne.config import load_settings, settings_payload


def write_config(
    tmp_path: Path,
    *,
    telegram: str = 'bot_token = "token"\nallowed_user_id = 12345',
    extra: str = "",
) -> Path:
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True, exist_ok=True)
    config = tmp_path / "config.toml"
    config.write_text(
        f'''\
version = 1
human_name = "Example User"
vault = "{vault}"

[telegram]
{telegram}
{extra}
''',
        encoding="utf-8",
    )
    return config


def test_toml_loads_structured_settings_and_profile_overrides(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        extra="""\

[profiles.mail]
model = "gpt-mail"
effort = "high"
web_search = "live"
""",
    )

    settings = load_settings(config, environ={})

    assert settings.human_name == "Example User"
    assert settings.codex_turn_settings == CodexTurnSettings(
        model="gpt-5.6-luna",
        effort=ReasoningEffort.low,
        web_search="disabled",
    )
    assert settings.mail_turn_settings == CodexTurnSettings(
        model="gpt-mail",
        effort=ReasoningEffort.high,
        web_search="live",
    )
    assert settings.mail_settings is None


def test_ariadne_config_selects_an_alternate_toml(tmp_path: Path) -> None:
    config = write_config(tmp_path)

    settings = load_settings(environ={"ARIADNE_CONFIG": str(config)})

    assert settings.allowed_user_id == 12345


def test_environment_variables_do_not_override_toml(tmp_path: Path) -> None:
    config = write_config(tmp_path)

    settings = load_settings(
        config,
        environ={
            "ARIADNE_HUMAN_NAME": "Legacy",
            "TELEGRAM_BOT_TOKEN": "legacy-token",
        },
    )

    assert settings.human_name == "Example User"
    assert settings.telegram_bot_token == "token"


def test_missing_config_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing.toml"

    with pytest.raises(ValueError, match="Ariadne config does not exist"):
        load_settings(missing, environ={})


def test_disabled_mail_does_not_require_credentials(tmp_path: Path) -> None:
    config = write_config(tmp_path, extra="\n[mail]\nenabled = false\n")

    settings = load_settings(config, environ={})

    assert settings.mail_settings is None
    assert "ARIADNE_MAIL_USERNAME" not in settings.mcp_environment
    assert "ARIADNE_MAIL_APP_PASSWORD" not in settings.mcp_environment


def test_enabled_incomplete_mail_is_rejected(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        extra="""\

[mail]
enabled = true
username = "person@example.com"
""",
    )

    with pytest.raises(ValidationError, match="app_password, routes"):
        load_settings(config, environ={})


def test_enabled_mail_expands_paths(tmp_path: Path) -> None:
    routes = tmp_path / "routes.yaml"
    routes.write_text("version: 1\n", encoding="utf-8")
    state = tmp_path / "state" / "mail.sqlite3"
    config = write_config(
        tmp_path,
        extra=f'''\

[mail]
enabled = true
username = "person@example.com"
app_password = "app-password"
routes = "{routes}"
state = "{state}"
''',
    )

    configured = load_settings(config, environ={}).mail_settings

    assert configured is not None
    assert configured.routes == routes.resolve()
    assert configured.state == state.resolve()
    assert configured.app_password.get_secret_value() == "app-password"

    settings = load_settings(config, environ={})
    assert settings.mcp_environment["ARIADNE_MAIL_USERNAME"] == "person@example.com"
    assert settings.mcp_environment["ARIADNE_MAIL_APP_PASSWORD"] == "app-password"


def test_default_mail_state_expands_the_home_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    config = write_config(tmp_path)

    settings = load_settings(config, environ={})

    assert settings.mail.state == fake_home / ".local/state/ariadne/mail.sqlite3"
    assert (
        settings.telegram.state == fake_home / ".local/state/ariadne/telegram.sqlite3"
    )
    assert settings.mcp_environment["ARIADNE_TELEGRAM_STATE"] == str(
        settings.telegram.state.resolve()
    )


def test_config_example_is_a_valid_disabled_mail_template(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True)
    source = Path(__file__).parents[1] / "config.example.toml"
    config = tmp_path / "config.toml"
    config.write_text(
        source.read_text(encoding="utf-8")
        .replace('vault = "~/ariadne-thread"', f'vault = "{vault}"')
        .replace('bot_token = ""', 'bot_token = "token"')
        .replace("allowed_user_id = 0", "allowed_user_id = 7"),
        encoding="utf-8",
    )

    settings = load_settings(config, environ={})

    assert settings.mail.enabled is False
    assert settings.mail_settings is None
    assert settings.telemetry.enabled is False


def test_enabled_telemetry_loads_from_toml(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        extra="""\

[telemetry]
enabled = true
endpoint = "https://otlp.example.com/otlp"
authorization = "Basic telemetry-secret"
service_name = "iris"
metrics = true
traces = false
export_interval_seconds = 15
""",
    )

    telemetry = load_settings(config, environ={}).telemetry

    assert str(telemetry.endpoint) == "https://otlp.example.com/otlp"
    assert telemetry.authorization is not None
    assert telemetry.authorization.get_secret_value() == "Basic telemetry-secret"
    assert telemetry.service_name == "iris"
    assert telemetry.metrics is True
    assert telemetry.traces is False
    assert telemetry.export_interval_seconds == 15


def test_enabled_incomplete_telemetry_is_rejected(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        extra="""\

[telemetry]
enabled = true
endpoint = "https://otlp.example.com/otlp"
""",
    )

    with pytest.raises(ValidationError, match="authorization"):
        load_settings(config, environ={})


def test_redacted_configuration_never_contains_secrets(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        telegram='bot_token = "secret"\nallowed_user_id = 7',
        extra="""\

[telemetry]
enabled = true
endpoint = "https://otlp.example.com/otlp"
authorization = "Basic telemetry-secret"
""",
    )

    serialized = json.dumps(settings_payload(load_settings(config, environ={})))

    assert "secret" not in serialized
    assert "telemetry-secret" not in serialized
    assert "<redacted>" in serialized


def test_validation_errors_never_contain_mail_passwords(tmp_path: Path) -> None:
    config = write_config(
        tmp_path,
        extra="""\

[mail]
enabled = true
username = "person@example.com"
app_password = "super-secret-password"
routes = "/does/not/exist.yaml"
""",
    )

    with pytest.raises(ValidationError) as raised:
        load_settings(config, environ={})

    assert "super-secret-password" not in str(raised.value)


def test_unknown_configuration_keys_are_rejected(tmp_path: Path) -> None:
    config = write_config(tmp_path, extra="unknown = true\n")

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        load_settings(config, environ={})


def test_settings_requires_a_positive_user_id(tmp_path: Path) -> None:
    config = write_config(tmp_path, telegram='bot_token = "token"\nallowed_user_id = 0')

    with pytest.raises(ValidationError, match="greater than 0"):
        load_settings(config, environ={})


def test_settings_requires_an_existing_vault(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            str(tmp_path / "vault"), str(tmp_path / "missing")
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="directory"):
        load_settings(config, environ={})


def test_settings_requires_a_git_vault(tmp_path: Path) -> None:
    config = write_config(tmp_path)
    (tmp_path / "vault" / ".git").rmdir()

    with pytest.raises(ValidationError, match="Git repository"):
        load_settings(config, environ={})
