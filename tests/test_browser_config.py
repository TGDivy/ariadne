from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from ariadne.browser.capability import SOCKET_ENVIRONMENT, TOOLS
from ariadne.codex import _mcp_config_overrides
from ariadne.codex.resolver import resolve_profile
from ariadne.config import load_settings, settings_payload
from ariadne.profile import TELEGRAM_PROFILE


def write_config(tmp_path: Path, browser: str) -> Path:
    vault = tmp_path / "vault"
    (vault / ".git").mkdir(parents=True)
    config = tmp_path / "config.toml"
    config.write_text(
        f'''\
version = 1
human_name = "Example User"
vault = "{vault}"

[telegram]
bot_token = "token"
allowed_user_id = 7

{browser}
''',
        encoding="utf-8",
    )
    return config


def test_enabled_browser_exports_only_its_private_socket_to_model_processes(
    tmp_path: Path,
) -> None:
    state = tmp_path / "private" / "browser.sqlite3"
    profiles = tmp_path / "private" / "profiles"
    artifacts = tmp_path / "private" / "artifacts"
    socket = tmp_path / "private" / "browser.sock"
    config = write_config(
        tmp_path,
        f'''\
[browser]
enabled = true
state = "{state}"
profiles = "{profiles}"
artifacts = "{artifacts}"
socket = "{socket}"
headless = true
takeover_url = "https://browser.private.example/view"
''',
    )

    settings = load_settings(config, environ={})
    payload = settings_payload(settings)["browser"]

    assert settings.mcp_environment[SOCKET_ENVIRONMENT] == str(socket.resolve())
    assert payload["enabled"] is True
    assert payload["takeover_url"] == "https://browser.private.example/view"
    assert "profile" not in settings.mcp_environment
    assert "artifact" not in settings.mcp_environment


def test_disabled_browser_does_not_add_a_dead_mcp_server(tmp_path: Path) -> None:
    settings = load_settings(write_config(tmp_path, ""), environ={})
    profile = resolve_profile(
        TELEGRAM_PROFILE,
        workspace=settings.agent_workspace,
        human=settings.human_name,
        mcp_environment=settings.mcp_environment,
    )

    overrides = _mcp_config_overrides(profile)

    assert SOCKET_ENVIRONMENT not in settings.mcp_environment
    assert not any("ariadne_browser" in value for value in overrides)


def test_enabled_browser_adds_dedicated_turn_scoped_mcp_server(
    tmp_path: Path,
) -> None:
    socket = tmp_path / "private" / "browser.sock"
    config = write_config(
        tmp_path,
        f'''\
[browser]
enabled = true
state = "{tmp_path / "private" / "state.sqlite3"}"
profiles = "{tmp_path / "private" / "profiles"}"
artifacts = "{tmp_path / "private" / "artifacts"}"
socket = "{socket}"
headless = true
''',
    )
    settings = load_settings(config, environ={})
    profile = resolve_profile(
        TELEGRAM_PROFILE,
        workspace=settings.agent_workspace,
        human=settings.human_name,
        mcp_environment=settings.mcp_environment,
    )

    overrides = _mcp_config_overrides(profile)

    assert 'mcp_servers.ariadne_browser.args=["-m", "ariadne.browser.mcp"]' in overrides
    assert "mcp_servers.ariadne_browser.enabled_tools=" + json.dumps(TOOLS) in overrides
    assert (
        "mcp_servers.ariadne_browser.env.ARIADNE_BROWSER_SOCKET="
        + json.dumps(str(socket.resolve()))
        in overrides
    )


@pytest.mark.parametrize(
    "takeover_url",
    [
        "http://browser.private.example/view",
        "https://user:secret@browser.private.example/view",
        "https://browser.private.example/view?token=secret",
    ],
)
def test_takeover_view_requires_a_credential_free_https_url(
    tmp_path: Path, takeover_url: str
) -> None:
    config = write_config(
        tmp_path,
        f'''\
[browser]
takeover_url = "{takeover_url}"
''',
    )

    with pytest.raises(ValidationError, match="takeover_url"):
        load_settings(config, environ={})
