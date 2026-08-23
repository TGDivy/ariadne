import logging
import sys
from pathlib import Path

from ariadne.__main__ import configure_logging, main


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
human_name = "Divy"
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
    assert '"bot_token": "<redacted>"' in output
    assert "super-secret-token" not in output
