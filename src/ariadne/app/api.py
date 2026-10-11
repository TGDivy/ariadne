"""HTTP routes and the event stream for the Ari app."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import BaseRoute, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from ..codex.models import CodexTurnSettings
from ..conversation.attachments import (
    SUPPORTED_IMAGE_MIME_TYPES,
    AttachmentError,
    safe_filename,
)
from ..conversation.core import (
    ConversationBusy,
    ConversationCore,
    SendRequest,
    UnknownReference,
)
from ..conversation.events import Heartbeat, MessageView, QuestionView
from ..knowledge.models import KnowledgeError
from .auth import BearerAuth, BodyLimit
from .devices import DeviceStore
from .schemas import (
    ActivityPage,
    AnswerRequest,
    AnswerResponse,
    AttachmentUploaded,
    DeviceRegistration,
    ErrorDetail,
    ErrorResponse,
    GoalsView,
    IdentityView,
    LibraryView,
    MessagesPage,
    ModelView,
    OkResponse,
    ReactionsRequest,
    ReactionsResponse,
    RecordView,
    SendMessageRequest,
    SendMessageResponse,
    SettingsOptions,
    SettingsUpdate,
    SettingsView,
    StatusView,
    StopResponse,
    UpcomingView,
)
from .sources import ActivityReader, StatusReader, ThreadReader, UpcomingReader

LOGGER = logging.getLogger(__name__)

JSON_BODY_LIMIT = 64 * 1024
HEARTBEAT_SECONDS = 25.0
ATTACHMENTS_PATH = "/v1/attachments"
UPLOAD_CHUNK_LIMIT = 1024 * 1024

Model = TypeVar("Model", bound=BaseModel)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AppServices:
    """Everything the API is allowed to touch."""

    core: ConversationCore
    status: StatusReader
    upcoming: UpcomingReader
    activity: ActivityReader
    thread: ThreadReader
    devices: DeviceStore
    identity: IdentityView
    token_hashes: frozenset[str]
    avatar: Path | None = None
    max_image_bytes: int = 10 * 1024 * 1024
    max_document_bytes: int = 50 * 1024 * 1024


def build_app(services: AppServices) -> Starlette:
    """Compose authenticated routes with body limits."""
    identity = services.identity
    if services.avatar is not None:
        identity = identity.model_copy(update={"avatar_url": "/v1/avatar"})
    services = replace(services, identity=identity)
    api = _Api(services)
    routes: list[BaseRoute] = [
        Route("/health", api.health, methods=["GET"]),
        Route("/v1/identity", api.identity, methods=["GET"]),
        Route("/v1/avatar", api.avatar, methods=["GET"]),
        Route("/v1/status", api.status, methods=["GET"]),
        Route("/v1/messages", api.messages, methods=["GET"]),
        Route("/v1/messages", api.send, methods=["POST"]),
        Route("/v1/messages/{message_id:int}/reactions", api.react, methods=["PUT"]),
        Route("/v1/turn/stop", api.stop, methods=["POST"]),
        Route("/v1/questions/{question_id}/answer", api.answer, methods=["POST"]),
        Route(ATTACHMENTS_PATH, api.upload, methods=["PUT"]),
        Route("/v1/files/{attachment_id}", api.file, methods=["GET"]),
        Route("/v1/conversation/new", api.new_conversation, methods=["POST"]),
        Route("/v1/settings", api.settings, methods=["GET"]),
        Route("/v1/settings", api.update_settings, methods=["PUT"]),
        Route("/v1/upcoming", api.upcoming, methods=["GET"]),
        Route("/v1/activity", api.activity, methods=["GET"]),
        Route("/v1/goals", api.goals, methods=["GET"]),
        Route("/v1/library", api.library, methods=["GET"]),
        Route("/v1/records/{record_id}", api.record, methods=["GET"]),
        Route("/v1/devices", api.register_device, methods=["POST"]),
        WebSocketRoute("/v1/stream", api.stream),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(
        BodyLimit, limit=JSON_BODY_LIMIT, exempt_paths=frozenset({ATTACHMENTS_PATH})
    )
    app.add_middleware(BearerAuth, token_hashes=lambda: services.token_hashes)
    return app


def _json(model: BaseModel, status: int = 200) -> JSONResponse:
    return JSONResponse(model.model_dump(mode="json"), status_code=status)


def _error(status: int, code: str, message: str) -> JSONResponse:
    return _json(ErrorResponse(error=ErrorDetail(code=code, message=message)), status)


def _guarded(handler: Callable[[Any, Request], Awaitable[Response]]) -> Any:
    async def wrapped(self: Any, request: Request) -> Response:
        try:
            return await handler(self, request)
        except ApiError as error:
            return _error(error.status, error.code, error.message)
        except ValidationError as error:
            errors = error.errors()
            detail = (
                errors[0].get("msg", "Invalid request.")
                if errors
                else "Invalid request."
            )
            return _error(422, "invalid_request", str(detail))
        except ConversationBusy as error:
            return _error(409, "busy", str(error))
        except UnknownReference as error:
            return _error(404, "not_found", str(error))

    wrapped.__name__ = handler.__name__
    return wrapped


class _Api:
    def __init__(self, services: AppServices) -> None:
        self.s = services

    async def _body(self, request: Request, model: type[Model]) -> Model:
        return model.model_validate_json(await request.body())

    async def health(self, request: Request) -> Response:
        return _json(OkResponse())

    @_guarded
    async def identity(self, request: Request) -> Response:
        return _json(self.s.identity)

    @_guarded
    async def avatar(self, request: Request) -> Response:
        if self.s.avatar is None:
            raise ApiError(404, "not_found", "No avatar is configured.")
        return FileResponse(self.s.avatar)

    @_guarded
    async def status(self, request: Request) -> Response:
        view: StatusView = await asyncio.to_thread(self.s.status.status)
        return _json(view)

    @_guarded
    async def messages(self, request: Request) -> Response:
        before = _optional_int(request.query_params.get("before"), "before")
        limit = _optional_int(request.query_params.get("limit"), "limit") or 50
        if not 1 <= limit <= 200:
            raise ApiError(422, "invalid_request", "limit must be between 1 and 200.")
        messages, has_more = self.s.core.log.page(before=before, limit=limit)
        return _json(
            MessagesPage(
                messages=tuple(MessageView.of(item) for item in messages),
                has_more=has_more,
            )
        )

    @_guarded
    async def send(self, request: Request) -> Response:
        body = await self._body(request, SendMessageRequest)
        try:
            result = await self.s.core.send(
                SendRequest(
                    text=body.text,
                    client_message_id=body.client_message_id,
                    attachment_ids=body.attachment_ids,
                    reply_to_id=body.reply_to_id,
                    voice=body.voice,
                )
            )
        except UnknownReference:
            raise
        except ValueError as error:
            raise ApiError(422, "invalid_request", str(error)) from error
        return _json(
            SendMessageResponse(
                message=MessageView.of(result.message), disposition=result.disposition
            )
        )

    @_guarded
    async def react(self, request: Request) -> Response:
        message_id = int(request.path_params["message_id"])
        body = await self._body(request, ReactionsRequest)
        try:
            changed = self.s.core.react(message_id, body.reactions)
        except ValueError as error:
            raise ApiError(422, "invalid_request", str(error)) from error
        if not changed:
            raise ApiError(
                404, "not_found", "Only Iris's messages can receive reactions."
            )
        message = self.s.core.log.get(message_id)
        return _json(
            ReactionsResponse(
                message_id=message_id,
                reactions=message.reactions if message is not None else (),
            )
        )

    @_guarded
    async def stop(self, request: Request) -> Response:
        return _json(StopResponse(stopping=await self.s.core.stop()))

    @_guarded
    async def answer(self, request: Request) -> Response:
        body = await self._body(request, AnswerRequest)
        selection = self.s.core.answer_question(
            str(request.path_params["question_id"]), body.choice_index
        )
        return _json(
            AnswerResponse(
                outcome=selection.outcome,
                question=(
                    QuestionView.of(selection.question)
                    if selection.question is not None
                    else None
                ),
            )
        )

    @_guarded
    async def upload(self, request: Request) -> Response:
        mime_type = (
            request.headers.get("content-type", "").split(";")[0].strip().lower()
        )
        filename = safe_filename(
            request.headers.get("x-filename", ""), fallback="upload"
        )
        is_image = mime_type in SUPPORTED_IMAGE_MIME_TYPES
        if mime_type.startswith("image/") and not is_image:
            raise ApiError(415, "unsupported", "Send JPEG, PNG, or WebP images.")
        limit = self.s.max_image_bytes if is_image else self.s.max_document_bytes
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            raise ApiError(413, "too_large", "That file is too large.")
        store = self.s.core.attachments
        path = await asyncio.to_thread(store.reserve, filename)
        written = 0
        try:
            with path.open("wb") as handle:
                async for chunk in request.stream():
                    written += len(chunk)
                    if written > limit:
                        raise ApiError(413, "too_large", "That file is too large.")
                    await asyncio.to_thread(handle.write, chunk)
            if written == 0:
                raise ApiError(422, "invalid_request", "The upload was empty.")
            attachment = await asyncio.to_thread(
                store.register,
                path,
                filename=filename,
                mime_type=mime_type or None,
                kind="image" if is_image else "document",
            )
        except (ApiError, AttachmentError, OSError):
            path.unlink(missing_ok=True)
            raise
        return _json(
            AttachmentUploaded(
                id=attachment.id,
                kind="image" if attachment.kind == "image" else "document",
                filename=attachment.filename,
                mime_type=attachment.mime_type,
                size_bytes=attachment.size_bytes,
            ),
            201,
        )

    @_guarded
    async def file(self, request: Request) -> Response:
        attachment = self.s.core.attachments.get(
            str(request.path_params["attachment_id"])
        )
        if attachment is None or not attachment.path.is_file():
            raise ApiError(404, "not_found", "That file is not available.")
        return FileResponse(
            attachment.path,
            filename=attachment.filename,
            media_type=attachment.mime_type,
        )

    @_guarded
    async def new_conversation(self, request: Request) -> Response:
        await self.s.core.new_conversation()
        return _json(OkResponse())

    @_guarded
    async def settings(self, request: Request) -> Response:
        return _json(
            SettingsOptions(
                current=_settings_view(self.s.core.settings),
                models=tuple(
                    ModelView(
                        id=model.identifier,
                        display_name=model.display_name,
                        default_effort=model.default_effort.value,
                        supported_efforts=tuple(
                            effort.value for effort in model.supported_efforts
                        ),
                    )
                    for model in await self.s.core.available_models()
                ),
            )
        )

    @_guarded
    async def update_settings(self, request: Request) -> Response:
        body = await self._body(request, SettingsUpdate)
        current = self.s.core.settings
        models = {
            model.identifier: model for model in await self.s.core.available_models()
        }
        model_id = body.model or current.model
        model = models.get(model_id)
        if model is None:
            raise ApiError(422, "invalid_request", "That model is not available.")
        effort = next(
            (
                item
                for item in model.supported_efforts
                if item.value == (body.effort or current.effort.value)
            ),
            None,
        )
        if effort is None:
            if body.effort is not None:
                raise ApiError(
                    422, "invalid_request", "That effort is not supported by the model."
                )
            effort = model.default_effort
        updated = CodexTurnSettings(
            model=model.identifier,
            effort=effort,
            web_search=body.web_search or current.web_search,
        )
        if updated != current:
            await self.s.core.update_settings(updated)
        return _json(_settings_view(updated))

    @_guarded
    async def upcoming(self, request: Request) -> Response:
        view: UpcomingView = await asyncio.to_thread(self.s.upcoming.upcoming)
        return _json(view)

    @_guarded
    async def activity(self, request: Request) -> Response:
        limit = _optional_int(request.query_params.get("limit"), "limit") or 50
        if not 1 <= limit <= 200:
            raise ApiError(422, "invalid_request", "limit must be between 1 and 200.")
        page: ActivityPage = await asyncio.to_thread(
            self.s.activity.page, before=request.query_params.get("before"), limit=limit
        )
        return _json(page)

    @_guarded
    async def goals(self, request: Request) -> Response:
        view: GoalsView = await asyncio.to_thread(self.s.thread.goals)
        return _json(view)

    @_guarded
    async def library(self, request: Request) -> Response:
        folder = request.query_params.get("folder", "")
        archived = request.query_params.get("archived", "false").lower() == "true"
        try:
            view: LibraryView = await asyncio.to_thread(
                self.s.thread.library, folder, archived=archived
            )
        except KnowledgeError as error:
            raise ApiError(404, "not_found", "That folder does not exist.") from error
        return _json(view)

    @_guarded
    async def record(self, request: Request) -> Response:
        try:
            view: RecordView | None = await asyncio.to_thread(
                self.s.thread.record, str(request.path_params["record_id"])
            )
        except KnowledgeError as error:
            raise ApiError(404, "not_found", "That record is not available.") from error
        if view is None:
            raise ApiError(404, "not_found", "That record does not exist.")
        return _json(view)

    @_guarded
    async def register_device(self, request: Request) -> Response:
        body = await self._body(request, DeviceRegistration)
        await asyncio.to_thread(self.s.devices.register, body.token, body.environment)
        return _json(OkResponse())

    async def stream(self, websocket: WebSocket) -> None:
        """Send a snapshot, replay missed committed events, then stream live ones."""
        after_value = websocket.query_params.get("after")
        try:
            after = int(after_value) if after_value is not None else None
        except ValueError:
            await websocket.close(code=4400)
            return
        await websocket.accept()
        bus = self.s.core.bus
        subscription = bus.subscribe()
        try:
            reset = after is None or not bus.journal.covers(after)
            snapshot = self.s.core.snapshot(reset=reset)
            await websocket.send_json(snapshot.model_dump(mode="json"))
            cutoff = snapshot.seq or 0
            if after is not None and not reset:
                for payload in bus.replay(after):
                    if payload["seq"] <= cutoff:
                        await websocket.send_json(payload)
            sender = asyncio.create_task(self._forward(websocket, subscription, cutoff))
            receiver = asyncio.create_task(_drain_client(websocket))
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await task
            for task in done:
                with suppress(WebSocketDisconnect, RuntimeError):
                    task.result()
        except WebSocketDisconnect:
            pass
        finally:
            subscription.close()

    async def _forward(
        self, websocket: WebSocket, subscription: Any, cutoff: int
    ) -> None:
        heartbeat_at = asyncio.get_running_loop().time() + HEARTBEAT_SECONDS
        while True:
            timeout = max(0.0, heartbeat_at - asyncio.get_running_loop().time())
            try:
                payload = await asyncio.wait_for(subscription.next(), timeout=timeout)
            except TimeoutError:
                heartbeat = Heartbeat(
                    boot_id=self.s.core.bus.boot_id, at=datetime.now(UTC)
                )
                await websocket.send_json(heartbeat.model_dump(mode="json"))
                heartbeat_at = asyncio.get_running_loop().time() + HEARTBEAT_SECONDS
                continue
            if payload is None:
                # The client fell behind; it must reconnect from its cursor.
                await websocket.close(code=4409)
                return
            if payload.get("seq") is not None and payload["seq"] <= cutoff:
                continue
            await websocket.send_json(payload)


async def _drain_client(websocket: WebSocket) -> None:
    """Consume client frames until it disconnects; commands use HTTP."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return


def _settings_view(settings: CodexTurnSettings) -> SettingsView:
    return SettingsView(
        model=settings.model,
        effort=settings.effort.value,
        web_search=settings.web_search,
    )


def _optional_int(value: str | None, name: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError as error:
        raise ApiError(422, "invalid_request", f"{name} must be an integer.") from error


__all__: Sequence[str] = ("AppServices", "build_app")
