"""Run the real `ariadne serve` stack in-process for integration tests."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from websockets.asyncio.client import ClientConnection, connect

from ariadne.app.auth import token_hash
from ariadne.config import load_settings
from ariadne.conversation.push import RecordingPushGateway
from ariadne.devkit.fixtures import write_thread
from ariadne.devkit.scripted import Script, ScriptedCodex
from ariadne.devkit.server import write_config
from ariadne.runtime import Runtime, bind_socket, build_runtime, serve

TOKEN = "integration-token"
EVENT_TIMEOUT = 5.0

Event = dict[str, Any]


@dataclass
class RunningServer:
    """One live server with clients for its HTTP API and event stream."""

    runtime: Runtime
    scripted: ScriptedCodex
    push: RecordingPushGateway
    http: httpx.AsyncClient
    port: int
    directory: Path
    _task: asyncio.Task[None] = field(repr=False)
    _stop: asyncio.Event = field(repr=False)

    @property
    def state(self) -> Path:
        return self.runtime.settings.conversation.state.resolve()

    @asynccontextmanager
    async def stream(
        self, *, after: int | None = None, token: str = TOKEN
    ) -> AsyncIterator[ClientConnection]:
        query = f"?after={after}" if after is not None else ""
        async with connect(
            f"ws://127.0.0.1:{self.port}/v1/stream{query}",
            additional_headers={"Authorization": f"Bearer {token}"},
        ) as websocket:
            yield websocket

    async def send(self, text: str, **fields: Any) -> Event:
        fields.setdefault("client_message_id", f"client-{text}")
        response = await self.http.post("/v1/messages", json={"text": text, **fields})
        assert response.status_code == 200, response.text
        return dict(response.json())

    async def wait_idle(self, timeout: float = EVENT_TIMEOUT) -> None:
        async def idle() -> None:
            while self.runtime.core.busy or self.runtime.core.inputs.count():
                await asyncio.sleep(0.02)

        await asyncio.wait_for(idle(), timeout)

    async def close(self) -> None:
        await self.http.aclose()
        self._stop.set()
        await asyncio.wait_for(self._task, 10)


async def next_event(websocket: ClientConnection) -> Event:
    return dict(json.loads(await asyncio.wait_for(websocket.recv(), EVENT_TIMEOUT)))


async def events_until(
    websocket: ClientConnection, done: Callable[[Event], bool]
) -> list[Event]:
    """Collect events up to and including the first that satisfies `done`."""
    collected: list[Event] = []
    while True:
        event = await next_event(websocket)
        collected.append(event)
        if done(event):
            return collected


def finished(event: Event) -> bool:
    return event["type"] == "turn.finished"


def of_type(events: list[Event], kind: str) -> list[Event]:
    return [event for event in events if event["type"] == kind]


def committed(events: list[Event], speaker: str | None = None) -> list[Event]:
    return [
        event["message"]
        for event in of_type(events, "message.committed")
        if speaker is None or event["message"]["speaker"] == speaker
    ]


async def start_server(
    directory: Path,
    script: Script,
    *,
    steer_error: Exception | None = None,
) -> RunningServer:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "workspace").mkdir(exist_ok=True)
    if not (directory / "thread").exists():
        (directory / "thread").mkdir()
        write_thread(directory / "thread")
    settings = load_settings(
        write_config(directory, human="Divy", token_sha256=token_hash(TOKEN), port=8737)
    )
    scripted = ScriptedCodex(
        script, state=settings.conversation.state.resolve(), steer_error=steer_error
    )
    push = RecordingPushGateway()
    runtime = build_runtime(settings, codex_client=scripted.as_client(), push=push)
    sock = bind_socket("127.0.0.1", 0)
    port = int(sock.getsockname()[1])
    ready = asyncio.Event()
    stop = asyncio.Event()
    task = asyncio.create_task(
        serve(runtime, sock=sock, ready=ready, stop=stop, handle_signals=False)
    )
    await asyncio.wait_for(ready.wait(), 10)
    http = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=10,
    )
    return RunningServer(
        runtime=runtime,
        scripted=scripted,
        push=push,
        http=http,
        port=port,
        directory=directory,
        _task=task,
        _stop=stop,
    )
