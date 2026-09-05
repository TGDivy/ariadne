import json
from types import SimpleNamespace
from typing import Any

from caldav.lib.error import AuthorizationError
from fastmcp import Client, FastMCP
from fastmcp.tools import ToolResult

from ariadne.mcp.errors import (
    DIAGNOSTIC_PREFIX,
    SafeToolErrorMiddleware,
    format_tool_failure,
)


def _diagnostic(message: str) -> dict[str, Any]:
    return json.loads(message.split(DIAGNOSTIC_PREFIX, 1)[1])


def test_provider_urls_cannot_be_mistaken_for_http_statuses() -> None:
    error = AuthorizationError(
        url="https://p123-caldav.icloud.com/123456/calendars/", reason="Forbidden"
    )

    diagnostic = _diagnostic(format_tool_failure(error, "provider_read", {}))

    assert diagnostic["exception_type"] == "AuthorizationError"
    assert diagnostic["http_status"] == 403
    assert diagnostic["provider_response_body"] is None


def test_http_exception_responses_are_detailed_without_exposing_secrets() -> None:
    class ProviderFailure(Exception):
        response: SimpleNamespace

    error = ProviderFailure("Provider rejected password=service-password")
    error.response = SimpleNamespace(
        status_code=502,
        text='{"message":"upstream failed","token":"service-token"}',
    )

    message = format_tool_failure(
        error,
        "provider_write",
        {
            "SERVICE_PASSWORD": "service-password",
            "SERVICE_API_TOKEN": "service-token",
        },
    )
    diagnostic = _diagnostic(message)

    assert "service-password" not in message
    assert "service-token" not in message
    assert diagnostic == {
        "exception_type": "ProviderFailure",
        "operation": "provider_write",
        "http_status": 502,
        "provider_response_body": (
            '{"message":"upstream failed","token":"[REDACTED]"}'
        ),
    }


async def test_explicit_error_results_are_also_safely_diagnosed() -> None:
    server = FastMCP("error-test")
    server.add_middleware(
        SafeToolErrorMiddleware({"PROVIDER_API_TOKEN": "provider-secret"})
    )

    @server.tool
    def failed_result() -> Any:
        return ToolResult(
            content='Provider rejected token="provider-secret".', is_error=True
        )

    async with Client(server) as client:
        result = await client.call_tool_mcp("failed_result", {})

    assert result.isError is True
    text = result.content[0].text
    diagnostic = _diagnostic(text)
    assert "provider-secret" not in text
    assert "[REDACTED]" in text
    assert diagnostic == {
        "exception_type": "ToolResultError",
        "operation": "failed_result",
        "http_status": None,
        "provider_response_body": None,
    }
