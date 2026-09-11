"""Stable names shared by browser profiles and the dedicated MCP server."""

from __future__ import annotations

SOCKET_ENVIRONMENT = "ARIADNE_BROWSER_SOCKET"

TOOLS = (
    "browser_start_session",
    "browser_list_tabs",
    "browser_select_tab",
    "browser_close_tab",
    "browser_navigate",
    "browser_inspect",
    "browser_click",
    "browser_type",
    "browser_select",
    "browser_scroll",
    "browser_upload",
    "browser_download",
    "browser_wait",
    "browser_screenshot",
    "browser_coordinate_click",
    "browser_request_takeover",
    "browser_resume_after_takeover",
    "browser_release_session",
)

__all__ = ["SOCKET_ENVIRONMENT", "TOOLS"]
