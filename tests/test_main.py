import json
import logging
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from ariadne.__main__ import main
from ariadne.config import Settings
from ariadne.service import configure_logging, status_timezone


def test_configure_logging_suppresses_http_client_request_logs() -> None:
    httpx_logger = logging.getLogger("httpx")
    httpcore_logger = logging.getLogger("httpcore")
    original_httpx_level = httpx_logger.level
    original_httpcore_level = httpcore_logger.level

    try:
        configure_logging()

        assert httpx_logger.level == logging.WARNING
        assert httpcore_logger.level == logging.WARNING
    finally:
        httpx_logger.setLevel(original_httpx_level)
        httpcore_logger.setLevel(original_httpcore_level)


def test_config_show_is_valid_json_and_redacts_secrets(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True)
    config = tmp_path / "config.toml"
    config.write_text(
        f'''\
version = 1
human_name = "Example User"
vault = "{vault}"
[telegram]
bot_token = "super-secret-token"
allowed_user_id = 7
''',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys, "argv", ["ariadne", "--config", str(config), "config", "show"]
    )

    main()

    output = capsys.readouterr().out
    assert json.loads(output)["telegram"]["bot_token"] == "<redacted>"
    assert "super-secret-token" not in output


def test_status_timezone_prefers_the_first_configured_local_zone(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True)

    def settings(**zones: str) -> Settings:
        return Settings.model_validate(
            {
                "version": 1,
                "human_name": "Example User",
                "vault": str(vault),
                "telegram": {"bot_token": "token", "allowed_user_id": 7},
                **{section: {"timezone": zone} for section, zone in zones.items()},
            }
        )

    assert status_timezone(settings()) == ZoneInfo("UTC")
    assert status_timezone(settings(calendar="Europe/London")) == ZoneInfo(
        "Europe/London"
    )
    assert status_timezone(settings(health="Asia/Kolkata")) == ZoneInfo("Asia/Kolkata")
    assert status_timezone(
        settings(stewardship="Europe/London", health="Asia/Kolkata")
    ) == ZoneInfo("Europe/London")
