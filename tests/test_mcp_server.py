import json
from pathlib import Path

from ariadne.mcp_server import TOOLS, handle_message, runtime_status


async def test_mcp_lists_runtime_and_staged_file_tools() -> None:
    response = await handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})

    assert response == {"jsonrpc": "2.0", "id": 1, "result": {"tools": TOOLS}}


async def test_mcp_runtime_status_never_returns_environment_values(
    tmp_path: Path, monkeypatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    monkeypatch.setenv("ARIADNE_VAULT", str(vault))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "must-not-appear")

    response = await handle_message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "runtime_status"},
        }
    )

    assert response is not None
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload == runtime_status()
    assert "must-not-appear" not in json.dumps(payload)
