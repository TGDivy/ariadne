"""Compose Ariadne's long-lived services and run them on one event loop."""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from typing import Protocol
from zoneinfo import ZoneInfo

import uvicorn
from openai_codex import AsyncCodex
from starlette.applications import Starlette

from .app.api import AppServices, build_app
from .app.devices import DeviceStore
from .app.schemas import IdentityView, SourceView
from .app.sources import (
    ActivityReader,
    NoActivity,
    RuntimeUpcoming,
    StatusReader,
    ThreadReader,
    UpcomingReader,
)
from .codex import CodexConversation
from .codex.resolver import resolve_profile
from .config import Settings
from .conversation.attachments import AttachmentStore
from .conversation.bus import EventBus
from .conversation.continuity import ConversationThreadStore
from .conversation.core import ConversationCore
from .conversation.journal import EventJournal
from .conversation.push import PushGateway
from .handoff import HandoffCoordinator, HandoffState
from .mail import MailLoop
from .profile import CONVERSATION_PROFILE
from .revisit.runtime import RevisitLoop
from .stewardship.runtime import StewardshipLoop
from .telemetry import Telemetry

LOGGER = logging.getLogger(__name__)


class BackgroundService(Protocol):
    async def run_forever(self) -> None: ...

    def stop(self) -> None: ...


class _Server(uvicorn.Server):
    """Leave signal handling to Ariadne so shutdown can close Codex cleanly."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


@dataclass(slots=True)
class Runtime:
    """Every service `ariadne serve` runs, ready to start."""

    settings: Settings
    core: ConversationCore
    handoffs: HandoffCoordinator
    app: Starlette
    telemetry: Telemetry
    revisits: RevisitLoop
    initiative: StewardshipLoop
    background: tuple[BackgroundService, ...] = ()
    shutdown_hooks: list[Callable[[], None]] = field(default_factory=list)


def status_timezone(settings: Settings) -> ZoneInfo:
    """Choose the owner's local zone from whichever surface actually sets one."""
    for candidate in (
        settings.stewardship.timezone,
        settings.calendar.timezone,
        settings.health.timezone,
    ):
        if candidate != "UTC":
            return ZoneInfo(candidate)
    return ZoneInfo("UTC")


def private_sources(settings: Settings) -> tuple[SourceView, ...]:
    """Report which major private capabilities are switched on, not reachable."""
    return (
        SourceView(name="Mail", enabled=settings.mail.enabled),
        SourceView(name="Calendar", enabled=settings.calendar.enabled),
        SourceView(name="Health", enabled=settings.health.enabled),
        SourceView(name="Browser", enabled=settings.browser.enabled),
        SourceView(name="Knowledge", enabled=True),
    )


def build_runtime(
    settings: Settings,
    *,
    codex_client: AsyncCodex | None = None,
    push: PushGateway | None = None,
    telemetry: Telemetry | None = None,
    token_hashes: frozenset[str] | None = None,
    upcoming: UpcomingReader | None = None,
    activity: ActivityReader | None = None,
) -> Runtime:
    """Wire configuration into services without starting anything."""
    telemetry = telemetry or Telemetry()
    state = settings.conversation.state.resolve()
    profile = resolve_profile(
        CONVERSATION_PROFILE,
        workspace=settings.agent_workspace,
        settings=settings.codex_turn_settings,
        human=settings.human_name,
        personality=settings.personality,
        mcp_environment=settings.mcp_environment,
        knowledge_root=settings.vault,
        network_domains=settings.health_network_domains,
    )
    conversation = CodexConversation(
        profile,
        client=codex_client,
        telemetry=telemetry,
        thread_store=ConversationThreadStore(state, profile_name=profile.name),
    )
    handoffs = HandoffCoordinator(HandoffState(state))
    core = ConversationCore(
        conversation,
        state=state,
        attachments=AttachmentStore(state, settings.app.attachments.resolve()),
        handoffs=handoffs,
        bus=EventBus(EventJournal(state)),
        human_name=settings.human_name,
        agent_name=settings.app.agent_name,
        push=push,
    )
    mail_settings = settings.mail_settings
    mail_loops = (
        tuple(
            MailLoop(
                mail_settings,
                settings.agent_workspace,
                settings.vault,
                settings.mail_turn_settings,
                human=settings.human_name,
                personality=settings.personality,
                mcp_environment=settings.mcp_environment,
                network_domains=settings.health_network_domains,
                account=account,
                telemetry=telemetry,
                codex_client=codex_client,
            )
            for account in mail_settings.accounts
        )
        if mail_settings is not None
        else ()
    )
    revisits = RevisitLoop(
        settings.revisit_settings,
        settings.agent_workspace,
        settings.vault,
        settings.revisit_turn_settings,
        human=settings.human_name,
        personality=settings.personality,
        mcp_environment=settings.mcp_environment,
        network_domains=settings.health_network_domains,
        telemetry=telemetry,
        codex_client=codex_client,
    )
    initiative = StewardshipLoop(
        settings.stewardship_settings,
        settings.agent_workspace,
        settings.vault,
        settings.stewardship_turn_settings,
        human=settings.human_name,
        personality=settings.personality,
        mcp_environment=settings.mcp_environment,
        network_domains=settings.health_network_domains,
        telemetry=telemetry,
        codex_client=codex_client,
    )
    background: tuple[BackgroundService, ...] = (*mail_loops, revisits)
    if settings.stewardship.enabled:
        background = (*background, initiative)
    status = StatusReader(
        core=core,
        handoffs=handoffs,
        timezone=status_timezone(settings),
        sources=private_sources(settings),
        wakeups=revisits.state,
        initiative=initiative,
    )
    app = build_app(
        AppServices(
            core=core,
            status=status,
            upcoming=upcoming
            or RuntimeUpcoming(wakeups=revisits.state, initiative=initiative),
            activity=activity or NoActivity(),
            thread=ThreadReader(settings.vault),
            devices=DeviceStore(state),
            identity=IdentityView(
                agent_name=settings.app.agent_name, human_name=settings.human_name
            ),
            token_hashes=(
                token_hashes
                if token_hashes is not None
                else frozenset(device.token_sha256 for device in settings.app.devices)
            ),
            avatar=settings.app.avatar,
            max_image_bytes=settings.app.max_image_bytes,
            max_document_bytes=settings.app.max_document_bytes,
        )
    )
    return Runtime(
        settings=settings,
        core=core,
        handoffs=handoffs,
        app=app,
        telemetry=telemetry,
        revisits=revisits,
        initiative=initiative,
        background=background,
    )


def bind_socket(host: str, port: int) -> socket.socket:
    """Bind the listening socket before serving, so callers know the port."""
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.set_inheritable(True)
    return sock


async def serve(
    runtime: Runtime,
    *,
    sock: socket.socket,
    ready: asyncio.Event | None = None,
    stop: asyncio.Event | None = None,
    handle_signals: bool = True,
) -> None:
    """Run the app API and every background service until asked to stop."""
    stop = stop or asyncio.Event()
    loop = asyncio.get_running_loop()
    if handle_signals:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
    server = _Server(
        uvicorn.Config(
            runtime.app,
            log_level="warning",
            access_log=False,
            ws="websockets-sansio",
            ws_max_size=256 * 1024,
            lifespan="off",
        )
    )
    await runtime.core.start()
    tasks = [
        asyncio.create_task(server.serve(sockets=[sock]), name="app-api"),
        asyncio.create_task(
            runtime.handoffs.run_forever(runtime.core), name="handoff-coordinator"
        ),
        *(
            asyncio.create_task(service.run_forever(), name=type(service).__name__)
            for service in runtime.background
        ),
    ]
    try:
        while not server.started:
            if tasks[0].done():
                tasks[0].result()
            await asyncio.sleep(0.01)
        LOGGER.info("Ariadne app API listening")
        if ready is not None:
            ready.set()
        waiter = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait(
            {waiter, *tasks}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            if (
                task is not waiter
                and not task.cancelled()
                and task.exception() is not None
            ):
                LOGGER.error("Service stopped unexpectedly: %s", task.get_name())
        waiter.cancel()
    finally:
        server.should_exit = True
        runtime.handoffs.stop()
        for service in runtime.background:
            service.stop()
        await runtime.core.close()
        for task in tasks[1:]:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task
        if handle_signals:
            for signum in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(signum)
        with suppress(Exception):
            await runtime.core.close_codex()
        for hook in runtime.shutdown_hooks:
            hook()


__all__ = ["Runtime", "bind_socket", "build_runtime", "serve"]
