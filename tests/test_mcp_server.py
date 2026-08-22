import json
from pathlib import Path

from ariadne.mcp_server import mcp, runtime_status


async def test_fastmcp_lists_runtime_and_staged_file_tools() -> None:
    tools = await mcp.list_tools()

    assert [tool.name for tool in tools] == ["runtime_status", "prepare_files"]


async def test_runtime_status_never_returns_environment_values(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("ARIADNE_VAULT", str(vault))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-appear")

    payload = runtime_status()

    assert payload["vault"] == str(vault)
    assert "must-not-appear" not in json.dumps(payload)
