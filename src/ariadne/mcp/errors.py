"""Safe, actionable diagnostics for MCP tool failures."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult

from ..redaction import redact_sensitive_text

DIAGNOSTIC_PREFIX = "Diagnostic (credentials redacted): "
MAX_FRIENDLY_MESSAGE_LENGTH = 1_000
MAX_PROVIDER_BODY_LENGTH = 4_000

_HTTP_STATUS = re.compile(r"(?<!\d)(?P<status>[1-5]\d{2})(?!\d)")
_EMBEDDED_HTTP_STATUS = re.compile(
    r"(?im)(?:^|HTTP(?:/\d+(?:\.\d+)?)?\s+|at\s+['\"])"
    r"(?P<status>[1-5]\d{2})(?=\s|$)"
)


def _as_text(value: object) -> str | None:
    if value is None or callable(value):
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _attribute(value: object, name: str) -> object | None:
    try:
        return getattr(value, name, None)
    except Exception:
        return None


def _status(value: object) -> int | None:
    if isinstance(value, int) and 100 <= value <= 599:
        return value
    if isinstance(value, str):
        match = _HTTP_STATUS.fullmatch(value.strip())
        if match is not None:
            return int(match.group("status"))
    return None


def _response_details(error: Exception) -> tuple[int | None, str | None]:
    response = _attribute(error, "response")
    if response is not None:
        status = _status(_attribute(response, "status_code")) or _status(
            _attribute(response, "status")
        )
        for name in ("text", "content", "body", "data", "raw"):
            raw_body = _attribute(response, name)
            body = _as_text(raw_body)
            if body is not None:
                return status, body
        if status is not None:
            return status, None

    status = _status(_attribute(error, "status_code")) or _status(
        _attribute(error, "status")
    )
    for name in ("body", "response_body"):
        body = _as_text(_attribute(error, name))
        if body is not None:
            return status, body
    return status, None


def _embedded_http_details(error: Exception) -> tuple[int | None, str | None]:
    candidates: list[str] = []
    for name in ("url", "reason"):
        value = _as_text(_attribute(error, name))
        if value and value not in candidates:
            candidates.append(value)
    candidates.extend(
        value
        for argument in error.args
        if (value := _as_text(argument)) and value not in candidates
    )
    rendered = str(error)
    if rendered not in candidates:
        candidates.append(rendered)

    for candidate in candidates:
        match = _EMBEDDED_HTTP_STATUS.search(candidate)
        if match is None:
            continue
        status = int(match.group("status"))
        response = candidate[match.start("status") :]
        if "\n\n" in response:
            return status, response.split("\n\n", 1)[1]
        if " - " in response:
            return status, response.split(" - ", 1)[1]
        return status, None

    reason = _as_text(_attribute(error, "reason")) or ""
    if type(error).__name__ == "AuthorizationError":
        if "unauthorized" in reason.casefold():
            return 401, None
        if "forbidden" in reason.casefold():
            return 403, None
    return None, None


def _exception_chain(error: Exception) -> tuple[Exception, ...]:
    chain = []
    current: Exception | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        cause = current.__cause__ or current.__context__
        current = cause if isinstance(cause, Exception) else None
    return tuple(chain)


def _failure_details(error: Exception) -> tuple[str, int | None, str | None]:
    chain = _exception_chain(error)
    exception_type = type(chain[-1]).__name__
    status: int | None = None
    body: str | None = None
    for item in reversed(chain):
        item_status, item_body = _response_details(item)
        if item_status is None and item_body is None:
            item_status, item_body = _embedded_http_details(item)
        status = status if status is not None else item_status
        body = body if body is not None else item_body
        if status is not None and body is not None:
            break
    return exception_type, status, body


def _limited(value: str, length: int) -> str:
    if len(value) <= length:
        return value
    return value[:length] + "… [truncated]"


def _diagnostic_text(
    *,
    exception_type: str,
    operation: str,
    http_status: int | None,
    provider_response_body: str | None,
    environment: Mapping[str, str],
) -> str:
    body = (
        _limited(
            redact_sensitive_text(provider_response_body, environment),
            MAX_PROVIDER_BODY_LENGTH,
        )
        if provider_response_body is not None
        else None
    )
    diagnostic = {
        "exception_type": exception_type,
        "operation": redact_sensitive_text(operation, environment),
        "http_status": http_status,
        "provider_response_body": body,
    }
    return DIAGNOSTIC_PREFIX + json.dumps(
        diagnostic, ensure_ascii=False, separators=(",", ":")
    )


def _format_failed_result(
    friendly: str,
    operation: str,
    exception_type: str,
    environment: Mapping[str, str],
) -> str:
    safe_friendly = _limited(
        redact_sensitive_text(friendly, environment), MAX_FRIENDLY_MESSAGE_LENGTH
    )
    diagnostic = _diagnostic_text(
        exception_type=exception_type,
        operation=operation,
        http_status=None,
        provider_response_body=None,
        environment=environment,
    )
    return f"{safe_friendly}\n\n{diagnostic}"


def format_tool_failure(
    error: Exception,
    operation: str,
    environment: Mapping[str, str] | None = None,
) -> str:
    """Keep an actionable message and append a redacted provider diagnostic."""
    values = environment if environment is not None else os.environ
    friendly = str(error).strip() or "The tool could not complete that operation."
    friendly = _limited(
        redact_sensitive_text(friendly, values), MAX_FRIENDLY_MESSAGE_LENGTH
    )
    exception_type, status, body = _failure_details(error)
    diagnostic = _diagnostic_text(
        exception_type=exception_type,
        operation=operation,
        http_status=status,
        provider_response_body=body,
        environment=values,
    )
    return f"{friendly}\n\n{diagnostic}"


class SafeToolErrorMiddleware(Middleware):
    """Ensure every failed MCP tool response has safe diagnostic context."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self._environment = environment

    @property
    def environment(self) -> Mapping[str, str]:
        return self._environment if self._environment is not None else os.environ

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        operation = context.message.name
        try:
            result = await call_next(context)
        except Exception as error:
            raise ToolError(
                format_tool_failure(error, operation, self.environment)
            ) from error

        if not result.is_error:
            return result
        friendly_parts = [
            block.text for block in result.content if isinstance(block, mt.TextContent)
        ]
        friendly = "\n".join(friendly_parts).strip() or (
            "The tool could not complete that operation."
        )
        return ToolResult(
            content=_format_failed_result(
                friendly, operation, "ToolResultError", self.environment
            ),
            is_error=True,
        )


def _low_level_exception_type(message: str) -> str:
    if message.startswith(("Input validation error:", "Output validation error:")):
        return "ValidationError"
    if message.startswith("Unexpected return type from tool:"):
        return "ToolResultError"
    return "ToolResponseError"


def _has_complete_diagnostic(message: str, operation: str) -> bool:
    start = 0
    while (index := message.find(DIAGNOSTIC_PREFIX, start)) >= 0:
        encoded = message[index + len(DIAGNOSTIC_PREFIX) :]
        try:
            diagnostic = json.loads(encoded)
        except (json.JSONDecodeError, TypeError):
            start = index + len(DIAGNOSTIC_PREFIX)
            continue
        return (
            isinstance(diagnostic, dict)
            and diagnostic.get("operation") == operation
            and isinstance(diagnostic.get("exception_type"), str)
            and set(diagnostic)
            == {
                "exception_type",
                "operation",
                "http_status",
                "provider_response_body",
            }
        )
    return False


def install_safe_tool_error_handling(server: FastMCP[Any]) -> None:
    """Cover both FastMCP execution errors and SDK-level validation failures.

    The MCP SDK performs strict input and output schema validation outside the
    FastMCP middleware chain. Its handler is wrapped here so those generated
    error results receive the same diagnostic guarantee as raised exceptions.
    """
    if getattr(server, "_ariadne_safe_tool_errors", False):
        return
    server.add_middleware(SafeToolErrorMiddleware())
    original_handler = server._mcp_server.request_handlers[mt.CallToolRequest]

    async def safe_handler(request: mt.CallToolRequest) -> mt.ServerResult:
        response = await original_handler(request)
        result = response.root
        if not isinstance(result, mt.CallToolResult) or not result.isError:
            return response
        friendly = "\n".join(
            block.text for block in result.content if isinstance(block, mt.TextContent)
        ).strip()
        if _has_complete_diagnostic(friendly, request.params.name):
            return response
        friendly = friendly or "The tool could not complete that operation."
        message = _format_failed_result(
            friendly,
            request.params.name,
            _low_level_exception_type(friendly),
            os.environ,
        )
        return mt.ServerResult(
            mt.CallToolResult(
                content=[mt.TextContent(type="text", text=message)],
                isError=True,
            )
        )

    server._mcp_server.request_handlers[mt.CallToolRequest] = safe_handler
    setattr(server, "_ariadne_safe_tool_errors", True)
