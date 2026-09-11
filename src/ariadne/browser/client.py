"""Async client for the private Unix-socket browser daemon."""

from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any

from .models import BrowserError

MAX_RPC_BYTES = 2 * 1024 * 1024


class BrowserRemoteError(BrowserError):
    """Safe structured failure returned by the browser daemon."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class BrowserClient:
    """Make one bounded request per private local socket connection."""

    def __init__(self, socket_path: Path, *, timeout_seconds: float = 35) -> None:
        self.socket_path = socket_path.expanduser().resolve()
        self.timeout_seconds = timeout_seconds

    async def call(self, method: str, **parameters: Any) -> dict[str, Any]:
        request_id = secrets.token_hex(12)
        request = (
            json.dumps(
                {"id": request_id, "method": method, "params": parameters},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        if len(request) > MAX_RPC_BYTES:
            raise BrowserRemoteError(
                "request_too_large", "Browser request is too large."
            )
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path),
                timeout=self.timeout_seconds,
            )
        except (OSError, TimeoutError) as error:
            raise BrowserRemoteError(
                "browser_unavailable",
                "The private browser service is not reachable.",
                retryable=True,
            ) from error
        try:
            writer.write(request)
            await writer.drain()
            response_line = await asyncio.wait_for(
                reader.readline(), timeout=self.timeout_seconds
            )
        except (OSError, TimeoutError) as error:
            raise BrowserRemoteError(
                "browser_response_lost",
                "The browser service response was lost. Do not repeat a consequential "
                "action until its definitive site status is checked.",
                retryable=False,
            ) from error
        finally:
            writer.close()
            await writer.wait_closed()
        if not response_line or len(response_line) > MAX_RPC_BYTES:
            raise BrowserRemoteError(
                "invalid_browser_response",
                "The browser service returned an invalid bounded response.",
            )
        try:
            response = json.loads(response_line)
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise BrowserRemoteError(
                "invalid_browser_response", "The browser service response was not JSON."
            ) from error
        if not isinstance(response, dict) or response.get("id") != request_id:
            raise BrowserRemoteError(
                "invalid_browser_response",
                "The browser response did not match its request.",
            )
        failure = response.get("error")
        if isinstance(failure, dict):
            raise BrowserRemoteError(
                str(failure.get("code", "browser_error")),
                str(failure.get("message", "Browser operation failed.")),
                retryable=bool(failure.get("retryable", False)),
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise BrowserRemoteError(
                "invalid_browser_response", "The browser result was not an object."
            )
        return result


__all__ = ["BrowserClient", "BrowserRemoteError", "MAX_RPC_BYTES"]
