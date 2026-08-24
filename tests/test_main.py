import logging
import sys
from pathlib import Path
from typing import Any, cast

from telegram import Update

from ariadne.__main__ import _run_polling, configure_logging, main


class RecordingApplication:
    def __init__(self) -> None:
        self.polling_options: dict[str, object] | None = None

    def run_polling(self, **options: object) -> None:
        self.polling_options = options


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


def test_polling_retries_transient_bootstrap_failures_indefinitely() -> None:
    application = RecordingApplication()

    _run_polling(cast(Any, application))

    assert application.polling_options == {
        "allowed_updates": Update.ALL_TYPES,
        "bootstrap_retries": -1,
    }


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
    assert '"bot_token": "<redacted>"' in output
    assert "super-secret-token" not in output
