"""Bearer-token and request-size guards for the app API.

Both are pure ASGI middleware: Starlette's `BaseHTTPMiddleware` passes
WebSocket connections through untouched, which would leave the event stream
unauthenticated.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from collections.abc import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send

FAILED_AUTH_DELAY_SECONDS = 0.5
PUBLIC_PATHS = frozenset({"/health"})


def generate_token() -> str:
    """Return a new 256-bit bearer token for one paired phone."""
    return secrets.token_urlsafe(32)


def token_hash(token: str) -> str:
    """Return the only form of a token the server stores.

    A fast hash is enough here: tokens are random 256-bit values, not
    passwords a person chose.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_token(scope: Scope) -> str | None:
    for name, value in scope.get("headers", ()):
        if name == b"authorization":
            scheme, _, credentials = bytes(value).decode("latin-1").partition(" ")
            if scheme.lower() == "bearer" and credentials.strip():
                return str(credentials.strip())
    return None


class BearerAuth:
    """Admit only requests that carry a paired phone's token."""

    def __init__(
        self, app: ASGIApp, *, token_hashes: Callable[[], frozenset[str]]
    ) -> None:
        self.app = app
        self._token_hashes = token_hashes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"} or scope["path"] in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return
        token = bearer_token(scope)
        if token is not None and self._valid(token):
            await self.app(scope, receive, send)
            return
        # Slowing every failure makes guessing a 256-bit token even less useful.
        await asyncio.sleep(FAILED_AUTH_DELAY_SECONDS)
        if scope["type"] == "websocket":
            await receive()
            await send({"type": "websocket.close", "code": 4401})
            return
        await _send_error(
            send, 401, "unauthorized", "A paired device token is required."
        )

    def _valid(self, token: str) -> bool:
        digest = token_hash(token)
        return any(hmac.compare_digest(digest, known) for known in self._token_hashes())


class BodyLimit:
    """Reject HTTP bodies above a bound before they reach a route."""

    def __init__(
        self, app: ASGIApp, *, limit: int, exempt_paths: frozenset[str] = frozenset()
    ) -> None:
        self.app = app
        self._limit = limit
        self._exempt = exempt_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] in self._exempt:
            await self.app(scope, receive, send)
            return
        for name, value in scope.get("headers", ()):
            if (
                name == b"content-length"
                and value.isdigit()
                and int(value) > self._limit
            ):
                await _send_error(
                    send, 413, "too_large", "The request body is too large."
                )
                return
        received = 0

        async def bounded_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self._limit:
                    raise BodyTooLarge()
            return message

        try:
            await self.app(scope, bounded_receive, send)
        except BodyTooLarge:
            await _send_error(send, 413, "too_large", "The request body is too large.")


class BodyTooLarge(Exception):
    """A streamed body crossed its limit."""


async def _send_error(send: Send, status: int, code: str, message: str) -> None:
    body = json.dumps({"error": {"code": code, "message": message}}).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if status == 401:
        headers.append((b"www-authenticate", b"Bearer"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})
