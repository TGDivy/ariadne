"""Private Unix-socket daemon that owns the persistent Chromium process."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sqlite3
from collections.abc import Awaitable, Callable
from typing import Any

from playwright.async_api import Error as PlaywrightError

from ..config import BrowserConfig
from .client import MAX_RPC_BYTES
from .models import (
    BrowserApprovalError,
    BrowserBusyError,
    BrowserConfigurationError,
    BrowserError,
    BrowserLeaseError,
    BrowserReferenceError,
    BrowserStateError,
    BrowserUncertainError,
)
from .runtime import BrowserRuntime

LOGGER = logging.getLogger(__name__)


class BrowserDaemon:
    """Dispatch a deliberately finite browser API over a mode-0600 socket."""

    def __init__(self, config: BrowserConfig) -> None:
        self.config = config
        self.runtime = BrowserRuntime(config)
        self._server: asyncio.AbstractServer | None = None
        self._stop = asyncio.Event()

    async def run(self) -> None:
        await self.runtime.start()
        socket = self.config.socket.resolve()
        socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        socket.parent.chmod(0o700)
        if socket.exists():
            try:
                reader, writer = await asyncio.open_unix_connection(socket)
            except OSError:
                socket.unlink()
            else:
                writer.close()
                await writer.wait_closed()
                del reader
                await self.runtime.close()
                raise BrowserConfigurationError(
                    "Another browser daemon already owns the configured socket."
                )
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=socket, limit=MAX_RPC_BYTES
        )
        socket.chmod(0o600)
        loop = asyncio.get_running_loop()
        for selected in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(selected, self._stop.set)
            except NotImplementedError:
                pass
        LOGGER.info("Private browser service ready at %s", socket)
        try:
            await self._stop.wait()
        finally:
            self._server.close()
            await self._server.wait_closed()
            await self.runtime.close()
            socket.unlink(missing_ok=True)

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        request_id: object = None
        try:
            line = await reader.readline()
            if not line or len(line) > MAX_RPC_BYTES:
                raise BrowserStateError("Browser request exceeded its size limit.")
            request = json.loads(line)
            if not isinstance(request, dict):
                raise BrowserStateError("Browser request must be an object.")
            request_id = request.get("id")
            method = request.get("method")
            parameters = request.get("params", {})
            if not isinstance(request_id, str) or not request_id:
                raise BrowserStateError("Browser request id is missing.")
            if not isinstance(method, str) or not isinstance(parameters, dict):
                raise BrowserStateError("Browser method or parameters are invalid.")
            result = await self._dispatch(method, parameters)
            response: dict[str, Any] = {"id": request_id, "result": result}
        except Exception as error:
            code, message, retryable = self._safe_failure(error)
            response = {
                "id": request_id,
                "error": {
                    "code": code,
                    "message": message,
                    "retryable": retryable,
                },
            }
        encoded = (
            json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            + b"\n"
        )
        if len(encoded) > MAX_RPC_BYTES:
            encoded = (
                json.dumps(
                    {
                        "id": request_id,
                        "error": {
                            "code": "browser_response_too_large",
                            "message": (
                                "Browser observation exceeded its response limit."
                            ),
                            "retryable": False,
                        },
                    },
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
        writer.write(encoded)
        try:
            await writer.drain()
        except (ConnectionError, BrokenPipeError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(
        self, method: str, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        methods: dict[str, Callable[..., Awaitable[dict[str, Any]]]] = {
            "health": self.runtime.health,
            "shutdown": self._shutdown,
            "profiles.create": self.runtime.create_profile,
            "profiles.list": self.runtime.list_profiles,
            "profiles.inspect": self.runtime.inspect_profile,
            "journal.list": self.runtime.journal,
            "recovery.list": self.runtime.list_uncertain,
            "recovery.resolve": self.runtime.resolve_uncertain,
            "session.start": self.runtime.start_session,
            "session.release": self.runtime.release_session,
            "tabs.list": self.runtime.list_tabs,
            "tabs.select": self.runtime.select_tab,
            "tabs.close": self.runtime.close_tab,
            "page.navigate": self.runtime.navigate,
            "page.inspect": self.runtime.inspect,
            "page.click": self.runtime.click,
            "page.type": self.runtime.type_text,
            "page.select": self.runtime.select,
            "page.scroll": self.runtime.scroll,
            "page.upload": self.runtime.upload,
            "page.download": self.runtime.download,
            "page.wait": self.runtime.wait,
            "page.screenshot": self.runtime.screenshot,
            "page.coordinate_click": self.runtime.coordinate_click,
            "takeover.request": self.runtime.request_takeover,
            "takeover.resume": self.runtime.resume_takeover,
            "confirmation.record": self.runtime.record_confirmation,
        }
        operation = methods.get(method)
        if operation is None:
            raise BrowserStateError("Unknown browser service method.")
        return await operation(**parameters)

    async def _shutdown(self) -> dict[str, Any]:
        asyncio.get_running_loop().call_soon(self._stop.set)
        return {"status": "stopping"}

    @staticmethod
    def _safe_failure(error: Exception) -> tuple[str, str, bool]:
        if isinstance(error, BrowserBusyError):
            return "profile_busy", str(error), True
        if isinstance(error, BrowserLeaseError):
            return "lease_invalid", str(error), False
        if isinstance(error, BrowserReferenceError):
            return "reference_stale", str(error), False
        if isinstance(error, BrowserApprovalError):
            return "confirmation_required", str(error), False
        if isinstance(error, BrowserUncertainError):
            return "outcome_uncertain", str(error), False
        if isinstance(error, BrowserConfigurationError):
            return "browser_configuration", str(error), False
        if isinstance(error, BrowserStateError):
            return "invalid_browser_state", str(error), False
        if isinstance(error, json.JSONDecodeError | TypeError | ValueError):
            return "invalid_request", "Browser request arguments are invalid.", False
        if isinstance(error, sqlite3.Error):
            return "browser_state_unavailable", "Browser state is unavailable.", True
        if isinstance(error, PlaywrightError):
            return (
                "browser_operation_failed",
                "Chromium could not complete the operation.",
                True,
            )
        if isinstance(error, BrowserError):
            return "browser_error", str(error), False
        LOGGER.exception("Unhandled browser daemon failure")
        return "internal_error", "Browser service encountered an internal error.", False


async def run_daemon(config: BrowserConfig) -> None:
    await BrowserDaemon(config).run()


__all__ = ["BrowserDaemon", "run_daemon"]
