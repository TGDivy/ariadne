"""Render the app API contract the iOS client is built against.

The document is OpenAPI 3.1 for the HTTP routes plus an `x-stream` section for
the WebSocket event stream. It is generated from the same pydantic models the
server uses and checked in, so a test fails whenever the two drift apart.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, TypeAdapter
from pydantic.json_schema import models_json_schema

from ..conversation.events import ServerEvent
from .schemas import (
    ActivityPage,
    AnswerRequest,
    AnswerResponse,
    AttachmentUploaded,
    DeviceRegistration,
    ErrorResponse,
    GoalsView,
    IdentityView,
    LibraryView,
    MessagesPage,
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

REF_TEMPLATE = "#/components/schemas/{model}"
CONTRACT_PATH = Path("docs/app-api.json")


@dataclass(frozen=True, slots=True)
class Parameter:
    name: str
    location: str
    type: str
    description: str


@dataclass(frozen=True, slots=True)
class Operation:
    method: str
    path: str
    summary: str
    response: type[BaseModel] | None
    request: type[BaseModel] | None = None
    parameters: tuple[Parameter, ...] = ()
    binary_request: bool = False
    binary_response: bool = False
    status: int = 200
    public: bool = False


def _query(name: str, type_: str, description: str) -> Parameter:
    return Parameter(name, "query", type_, description)


def _path(name: str, type_: str = "string") -> Parameter:
    return Parameter(name, "path", type_, f"The {name.replace('_', ' ')}.")


OPERATIONS: tuple[Operation, ...] = (
    Operation(
        "GET", "/health", "Liveness, without authentication.", OkResponse, public=True
    ),
    Operation("GET", "/v1/identity", "The agent's name and avatar.", IdentityView),
    Operation(
        "GET", "/v1/avatar", "The agent's avatar image.", None, binary_response=True
    ),
    Operation("GET", "/v1/status", "What is enabled and running.", StatusView),
    Operation(
        "GET",
        "/v1/messages",
        "Committed messages, newest page first, each page oldest first.",
        MessagesPage,
        parameters=(
            _query("before", "integer", "Only messages with a smaller id."),
            _query("limit", "integer", "Page size, 1 to 200 (default 50)."),
        ),
    ),
    Operation(
        "POST",
        "/v1/messages",
        "Send a message; it starts, steers, queues, or answers.",
        SendMessageResponse,
        request=SendMessageRequest,
    ),
    Operation(
        "PUT",
        "/v1/messages/{message_id}/reactions",
        "Replace the owner's reactions on one of Iris's messages.",
        ReactionsResponse,
        request=ReactionsRequest,
        parameters=(_path("message_id", "integer"),),
    ),
    Operation("POST", "/v1/turn/stop", "Interrupt the active turn.", StopResponse),
    Operation(
        "POST",
        "/v1/questions/{question_id}/answer",
        "Answer a presented question by choice; typed answers are messages.",
        AnswerResponse,
        request=AnswerRequest,
        parameters=(_path("question_id"),),
    ),
    Operation(
        "PUT",
        "/v1/attachments",
        "Upload one file as the raw body; name it with an X-Filename header.",
        AttachmentUploaded,
        binary_request=True,
        status=201,
        parameters=(
            Parameter("X-Filename", "header", "string", "The original file name."),
        ),
    ),
    Operation(
        "GET",
        "/v1/files/{attachment_id}",
        "Download an attachment or a file Iris shared.",
        None,
        binary_response=True,
        parameters=(_path("attachment_id"),),
    ),
    Operation(
        "POST", "/v1/conversation/new", "Start a fresh Codex conversation.", OkResponse
    ),
    Operation("GET", "/v1/settings", "Current settings and choices.", SettingsOptions),
    Operation(
        "PUT",
        "/v1/settings",
        "Change model, effort, or web research; starts a fresh conversation.",
        SettingsView,
        request=SettingsUpdate,
    ),
    Operation("GET", "/v1/upcoming", "Scheduled activations.", UpcomingView),
    Operation(
        "GET",
        "/v1/activity",
        "What Iris did, newest first.",
        ActivityPage,
        parameters=(
            _query("before", "string", "Only items older than this item id."),
            _query("limit", "integer", "Page size, 1 to 200 (default 50)."),
        ),
    ),
    Operation(
        "GET", "/v1/goals", "Active and completed goals from the Thread.", GoalsView
    ),
    Operation(
        "GET",
        "/v1/library",
        "One level of the Thread.",
        LibraryView,
        parameters=(
            _query("folder", "string", "Semantic folder; empty for the root."),
            _query("archived", "boolean", "Browse archived records instead."),
        ),
    ),
    Operation(
        "GET",
        "/v1/records/{record_id}",
        "One Thread record with Markdown body.",
        RecordView,
        parameters=(_path("record_id"),),
    ),
    Operation(
        "POST",
        "/v1/devices",
        "Register this phone's push token.",
        OkResponse,
        request=DeviceRegistration,
    ),
)


def build_contract() -> dict[str, Any]:
    models: set[type[BaseModel]] = {ErrorResponse}
    for operation in OPERATIONS:
        models.update(item for item in (operation.request, operation.response) if item)
    _, model_schemas = models_json_schema(
        [
            (model, "serialization")
            for model in sorted(models, key=lambda item: item.__name__)
        ],
        ref_template=REF_TEMPLATE,
    )
    schemas: dict[str, Any] = dict(model_schemas.get("$defs", {}))
    events = TypeAdapter(ServerEvent).json_schema(
        ref_template=REF_TEMPLATE, mode="serialization"
    )
    schemas.update(events.pop("$defs", {}))
    schemas["ServerEvent"] = events
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Ariadne app API",
            "version": "1",
            "description": (
                "Commands are HTTP; the conversation streams over the WebSocket "
                "described in x-stream. Every route except /health needs a "
                "paired device's bearer token."
            ),
        },
        "security": [{"bearer": []}],
        "paths": _paths(),
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "schemas": dict(sorted(schemas.items())),
        },
        "x-stream": {
            "path": "/v1/stream",
            "protocol": "websocket",
            "query": {
                "after": (
                    "Optional sequence number of the last committed event the "
                    "client applied. Without it, or when the server no longer "
                    "has those events, the snapshot has reset=true and the "
                    "client should refetch /v1/messages."
                )
            },
            "events": {"$ref": f"{REF_TEMPLATE.format(model='ServerEvent')}"},
            "notes": [
                "The first frame is always a snapshot.",
                "Committed events carry a monotonic seq; live events carry null.",
                "boot_id changes when the server restarts.",
                "A heartbeat arrives at least every 25 seconds.",
                "Close code 4401 means unauthorized; 4409 means the client fell "
                "behind and must reconnect with its cursor.",
            ],
        },
    }


def _paths() -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for operation in OPERATIONS:
        entry: dict[str, Any] = {
            "summary": operation.summary,
            "responses": {
                str(operation.status): _response(operation),
                "4XX": {
                    "description": "A refused request.",
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": REF_TEMPLATE.format(model="ErrorResponse")
                            }
                        }
                    },
                },
            },
        }
        if operation.public:
            entry["security"] = []
        if operation.parameters:
            entry["parameters"] = [
                {
                    "name": parameter.name,
                    "in": parameter.location,
                    "required": parameter.location == "path",
                    "description": parameter.description,
                    "schema": {"type": parameter.type},
                }
                for parameter in operation.parameters
            ]
        if operation.request is not None:
            entry["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": _ref(operation.request)}},
            }
        elif operation.binary_request:
            entry["requestBody"] = {
                "required": True,
                "content": {
                    "application/octet-stream": {
                        "schema": {"type": "string", "format": "binary"}
                    }
                },
            }
        paths.setdefault(operation.path, {})[operation.method.lower()] = entry
    return paths


def _response(operation: Operation) -> dict[str, Any]:
    if operation.binary_response or operation.response is None:
        return {
            "description": "File contents.",
            "content": {"application/octet-stream": {"schema": {"type": "string"}}},
        }
    return {
        "description": operation.summary,
        "content": {"application/json": {"schema": _ref(operation.response)}},
    }


def _ref(model: type[BaseModel]) -> dict[str, str]:
    return {"$ref": REF_TEMPLATE.format(model=model.__name__)}


def render_contract() -> str:
    return json.dumps(build_contract(), indent=2, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Write or check the app API contract.")
    parser.add_argument("--path", type=Path, default=CONTRACT_PATH)
    parser.add_argument("--check", action="store_true", help="Fail if out of date.")
    args = parser.parse_args(argv)
    rendered = render_contract()
    if args.check:
        current = args.path.read_text(encoding="utf-8") if args.path.exists() else ""
        if current != rendered:
            raise SystemExit(
                f"{args.path} is out of date; run python -m ariadne.app.contract"
            )
        return
    args.path.write_text(rendered, encoding="utf-8")


if __name__ == "__main__":
    main()
