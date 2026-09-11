from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from ariadne.browser.capability import SOCKET_ENVIRONMENT, TOOLS
from ariadne.browser.client import BrowserClient
from ariadne.browser.daemon import BrowserDaemon
from ariadne.browser.mcp import browser_inspect, mcp
from ariadne.config import BrowserConfig


def config(tmp_path: Path) -> BrowserConfig:
    return BrowserConfig(
        enabled=True,
        state=tmp_path / "state" / "browser.sqlite3",
        profiles=tmp_path / "profiles",
        artifacts=tmp_path / "artifacts",
        socket=tmp_path / "run" / "browser.sock",
        headless=True,
        lease_seconds=60,
        journal_max_entries=100,
    )


async def wait_for_socket(path: Path) -> None:
    for _ in range(200):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("browser daemon did not create its socket")


async def test_daemon_client_health_profile_and_deliberate_shutdown(
    tmp_path: Path,
) -> None:
    settings = config(tmp_path)
    daemon = BrowserDaemon(settings)
    running = asyncio.create_task(daemon.run())
    await wait_for_socket(settings.socket)
    client = BrowserClient(settings.socket)

    health = await client.call("health")
    socket_mode = settings.socket.stat().st_mode & 0o777
    created = await client.call("profiles.create", name="personal")
    listed = await client.call("profiles.list")
    stopping = await client.call("shutdown")
    await asyncio.wait_for(running, timeout=5)

    assert health["status"] == "ready"
    assert socket_mode == 0o600
    assert created["profile"]["name"] == "personal"
    assert listed["count"] == 1
    assert stopping == {"status": "stopping"}
    assert not settings.socket.exists()


async def test_dedicated_mcp_exposes_only_bounded_browser_operations() -> None:
    async with Client(mcp) as client:
        tools = await client.list_tools()

    assert tuple(tool.name for tool in tools) == TOOLS
    assert "browser_record_confirmation" not in {tool.name for tool in tools}
    assert all("javascript" not in tool.name for tool in tools)


async def test_browser_mcp_requires_an_explicit_private_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(SOCKET_ENVIRONMENT, raising=False)

    with pytest.raises(ToolError, match="not configured"):
        await browser_inspect("task", "token")
