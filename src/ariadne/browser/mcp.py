"""Dedicated model-facing MCP server for the browser daemon."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..mcp.errors import install_safe_tool_error_handling
from .capability import SOCKET_ENVIRONMENT
from .client import BrowserClient, BrowserRemoteError


def _client() -> BrowserClient:
    value = os.environ.get(SOCKET_ENVIRONMENT, "").strip()
    if not value:
        raise ToolError("Private browser control is not configured for this turn.")
    return BrowserClient(Path(value))


async def _call(method: str, **parameters: Any) -> dict[str, Any]:
    try:
        return await _client().call(method, **parameters)
    except BrowserRemoteError as error:
        suffix = " You may retry this read-only operation." if error.retryable else ""
        raise ToolError(f"{error.message}{suffix}") from error


async def browser_start_session(
    profile: str, task_id: str, lease_token: str | None = None
) -> dict[str, Any]:
    """Create or reattach one bounded task to a named persistent profile.

    Keep the returned task id and private lease token for every later browser call.
    A profile permits one mutating task at a time; busy means wait rather than race it.
    """
    return await _call(
        "session.start", profile=profile, task_id=task_id, lease_token=lease_token
    )


async def browser_list_tabs(task_id: str, lease_token: str) -> dict[str, Any]:
    """List bounded URL/title metadata for tabs in this task's profile."""
    return await _call("tabs.list", task_id=task_id, lease_token=lease_token)


async def browser_select_tab(
    task_id: str, lease_token: str, tab_id: str
) -> dict[str, Any]:
    """Select a tab returned by browser_list_tabs and inspect its fresh state."""
    return await _call(
        "tabs.select", task_id=task_id, lease_token=lease_token, tab_id=tab_id
    )


async def browser_close_tab(
    task_id: str, lease_token: str, tab_id: str
) -> dict[str, Any]:
    """Close a selected non-final tab without closing the persistent profile."""
    return await _call(
        "tabs.close", task_id=task_id, lease_token=lease_token, tab_id=tab_id
    )


async def browser_navigate(task_id: str, lease_token: str, url: str) -> dict[str, Any]:
    """Navigate to an explicit HTTP(S) URL and return bounded semantic state.

    New origins are surfaced. Never embed credentials in the URL.
    """
    return await _call(
        "page.navigate", task_id=task_id, lease_token=lease_token, url=url
    )


async def browser_inspect(task_id: str, lease_token: str) -> dict[str, Any]:
    """Read fresh bounded text and semantic element references for the current page."""
    return await _call("page.inspect", task_id=task_id, lease_token=lease_token)


async def browser_click(
    task_id: str,
    lease_token: str,
    ref: str,
    action_kind: Literal["reversible", "consequential"] = "reversible",
    operation_id: str | None = None,
    approval_id: str | None = None,
) -> dict[str, Any]:
    """Click one fresh semantic reference.

    Use `reversible` for navigation, form preparation, and cart building. A payment,
    submission as the owner, account change, or material commitment is
    `consequential` and only works with confirmation recorded by a trusted Ariadne
    flow. Never retry a consequential call whose outcome is reported uncertain.
    """
    return await _call(
        "page.click",
        task_id=task_id,
        lease_token=lease_token,
        ref=ref,
        action_kind=action_kind,
        operation_id=operation_id,
        approval_id=approval_id,
    )


async def browser_type(
    task_id: str,
    lease_token: str,
    ref: str,
    text: str,
    clear: bool = True,
) -> dict[str, Any]:
    """Enter bounded text in a fresh non-secret field without pressing Enter.

    Password, MFA, and payment fields require private human takeover.
    """
    return await _call(
        "page.type",
        task_id=task_id,
        lease_token=lease_token,
        ref=ref,
        text=text,
        clear=clear,
    )


async def browser_select(
    task_id: str, lease_token: str, ref: str, values: list[str]
) -> dict[str, Any]:
    """Select explicit option values in a fresh semantic select reference."""
    return await _call(
        "page.select",
        task_id=task_id,
        lease_token=lease_token,
        ref=ref,
        values=values,
    )


async def browser_scroll(
    task_id: str,
    lease_token: str,
    direction: Literal["up", "down"],
    amount: int = 700,
) -> dict[str, Any]:
    """Scroll a bounded amount and return fresh semantic page state."""
    return await _call(
        "page.scroll",
        task_id=task_id,
        lease_token=lease_token,
        direction=direction,
        amount=amount,
    )


async def browser_upload(
    task_id: str, lease_token: str, ref: str, path: str
) -> dict[str, Any]:
    """Attach one explicitly named existing local artifact to a file input."""
    return await _call(
        "page.upload",
        task_id=task_id,
        lease_token=lease_token,
        ref=ref,
        path=path,
    )


async def browser_download(task_id: str, lease_token: str, ref: str) -> dict[str, Any]:
    """Click an expected download and retain it as an inert private artifact."""
    return await _call(
        "page.download", task_id=task_id, lease_token=lease_token, ref=ref
    )


async def browser_wait(
    task_id: str,
    lease_token: str,
    text: str | None = None,
    url: str | None = None,
    seconds: float | None = None,
) -> dict[str, Any]:
    """Wait boundedly for exactly one visible text, URL pattern, or duration."""
    return await _call(
        "page.wait",
        task_id=task_id,
        lease_token=lease_token,
        text=text,
        url=url,
        seconds=seconds,
    )


async def browser_screenshot(
    task_id: str, lease_token: str, full_page: bool = False
) -> dict[str, Any]:
    """Capture private visual evidence for the current page revision."""
    return await _call(
        "page.screenshot",
        task_id=task_id,
        lease_token=lease_token,
        full_page=full_page,
    )


async def browser_coordinate_click(
    task_id: str,
    lease_token: str,
    screenshot_id: str,
    x: float,
    y: float,
) -> dict[str, Any]:
    """Fallback click using a current screenshot; never use for final submission."""
    return await _call(
        "page.coordinate_click",
        task_id=task_id,
        lease_token=lease_token,
        screenshot_id=screenshot_id,
        x=x,
        y=y,
    )


async def browser_request_takeover(
    task_id: str, lease_token: str, reason: str
) -> dict[str, Any]:
    """Pause agent mutation so the owner can use the private visual browser."""
    return await _call(
        "takeover.request",
        task_id=task_id,
        lease_token=lease_token,
        reason=reason,
    )


async def browser_resume_after_takeover(
    task_id: str, lease_token: str
) -> dict[str, Any]:
    """Resume after the owner releases control, invalidating every old reference.

    Human takeover is not confirmation for a consequential action.
    """
    return await _call("takeover.resume", task_id=task_id, lease_token=lease_token)


async def browser_release_session(task_id: str, lease_token: str) -> dict[str, Any]:
    """Release task ownership without deleting the persistent login profile."""
    return await _call("session.release", task_id=task_id, lease_token=lease_token)


def create_server() -> FastMCP:
    server = FastMCP(
        "Ariadne Browser",
        instructions=(
            "Private persistent browser control. Inspect before acting, use fresh "
            "semantic references, request takeover for login/MFA/CAPTCHA, and never "
            "repeat an uncertain consequential action."
        ),
        version="0.1.0",
        strict_input_validation=True,
    )
    install_safe_tool_error_handling(server)
    read_only = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
    reversible = {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    consequential = {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    server.tool(browser_start_session, annotations=reversible)
    server.tool(browser_list_tabs, annotations=read_only)
    server.tool(browser_select_tab, annotations=reversible)
    server.tool(browser_close_tab, annotations=reversible)
    server.tool(browser_navigate, annotations=reversible)
    server.tool(browser_inspect, annotations=read_only)
    server.tool(browser_click, annotations=consequential)
    server.tool(browser_type, annotations=reversible)
    server.tool(browser_select, annotations=reversible)
    server.tool(browser_scroll, annotations=reversible)
    server.tool(browser_upload, annotations=reversible)
    server.tool(browser_download, annotations=reversible)
    server.tool(browser_wait, annotations=read_only)
    server.tool(browser_screenshot, annotations=read_only)
    server.tool(browser_coordinate_click, annotations=reversible)
    server.tool(browser_request_takeover, annotations=reversible)
    server.tool(browser_resume_after_takeover, annotations=reversible)
    server.tool(browser_release_session, annotations=reversible)
    return server


mcp = create_server()


def main() -> None:
    mcp.run(show_banner=False)


if __name__ == "__main__":
    main()


__all__ = ["create_server", "mcp"]
