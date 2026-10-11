"""`ariadne-dev-server`: the real app API over a scripted model and fixtures.

It needs no credentials, Codex login, or network, so the iOS app can be built
and exercised on any machine with uv and git.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import tempfile
from collections.abc import Coroutine, Sequence
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route

from ..app.auth import generate_token, token_hash
from ..config import load_settings
from ..conversation.core import ConversationCore, SendRequest
from ..conversation.push import RecordingPushGateway
from ..handoff import HandoffCoordinator
from ..runtime import bind_socket, build_runtime, serve
from .fixtures import FixtureActivity, FixtureUpcoming, seed_conversation, write_thread
from .scripted import Script, ScriptedCodex, load_script, parse_script

LOGGER = logging.getLogger(__name__)

SHORTLIST = """# Dinner shortlist

| Place | Why |
| --- | --- |
| Gostilna Sokol | Traditional, seats six |
| Monstera | Seasonal tasting menu |
| Vander | Quiet terrace by the river |
"""


def default_script() -> Script:
    import yaml  # type: ignore[import-untyped]

    text = (
        files(__package__).joinpath("default_script.yaml").read_text(encoding="utf-8")
    )
    return parse_script(yaml.safe_load(text))


def write_config(directory: Path, *, human: str, token_sha256: str, port: int) -> Path:
    """Write a complete private config that points only inside `directory`."""
    config = directory / "config.toml"
    values = {
        "thread": directory / "thread",
        "workspace": directory / "workspace",
        "conversation": directory / "conversation.sqlite3",
        "revisits": directory / "revisits.sqlite3",
        "stewardship": directory / "stewardship.sqlite3",
        "attachments": directory / "attachments",
    }
    config.write_text(
        f"""version = 1
human_name = {json.dumps(human)}
vault = {json.dumps(str(values["thread"]))}
workspace = {json.dumps(str(values["workspace"]))}

[conversation]
state = {json.dumps(str(values["conversation"]))}

[app]
port = {port}
agent_name = "Iris"
attachments = {json.dumps(str(values["attachments"]))}

[[app.devices]]
id = "dev"
name = "Dev device"
token_sha256 = "{token_sha256}"

[revisits]
state = {json.dumps(str(values["revisits"]))}

[stewardship]
enabled = false
state = {json.dumps(str(values["stewardship"]))}
""",
        encoding="utf-8",
    )
    return config


def dev_routes(
    core: ConversationCore, handoffs: HandoffCoordinator, push: RecordingPushGateway
) -> tuple[BaseRoute, ...]:
    """Endpoints that force each app state on demand."""
    pending: set[asyncio.Task[Any]] = set()

    def spawn(coroutine: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coroutine)
        pending.add(task)
        task.add_done_callback(pending.discard)

    async def pushes(request: Request) -> Response:
        return JSONResponse(
            [
                {
                    "title": item.title,
                    "body": item.body,
                    "level": item.level,
                    "message_id": item.message_id,
                }
                for item in push.notifications
            ]
        )

    async def proactive(request: Request) -> Response:
        handoffs.state.stage(
            activation_key=f"dev-{datetime.now(UTC).timestamp()}",
            source="mail",
            body="The train company moved Saturday's departure to 07:42; calendar "
            "updated.",
        )
        for item in handoffs.state.list_status("staged"):
            handoffs.state.release(item.activation_key)
        spawn(handoffs.deliver_now(core))
        return JSONResponse({"status": "triggered"}, status_code=202)

    def sender(text: str) -> Route:
        async def trigger(request: Request) -> Response:
            spawn(core.send(SendRequest(text=text)))
            return JSONResponse({"status": "triggered"}, status_code=202)

        return Route(f"/dev/trigger/{text.split()[-1]}", trigger, methods=["POST"])

    return (
        Route("/dev/pushes", pushes, methods=["GET"]),
        Route("/dev/trigger/proactive", proactive, methods=["POST"]),
        sender("[dev] question"),
        sender("[dev] fail"),
        sender("[dev] long"),
        sender("[dev] stop"),
        sender("[dev] share"),
    )


async def run_dev_server(
    *,
    host: str,
    port: int,
    directory: Path,
    script: Script,
    token: str,
    human: str,
) -> None:
    (directory / "workspace").mkdir(parents=True, exist_ok=True)
    (directory / "workspace" / "dinner-shortlist.md").write_text(
        SHORTLIST, encoding="utf-8"
    )
    if not (directory / "thread").exists():
        (directory / "thread").mkdir()
        write_thread(directory / "thread")
    settings = load_settings(
        write_config(directory, human=human, token_sha256=token_hash(token), port=port)
    )
    state = settings.conversation.state.resolve()
    scripted = ScriptedCodex(script, state=state)
    push = RecordingPushGateway()
    runtime = build_runtime(
        settings,
        codex_client=scripted.as_client(),
        push=push,
        upcoming=FixtureUpcoming(),
        activity=FixtureActivity(),
    )
    # The middleware stack is built on the first request, so routes added now
    # are authenticated like every other route.
    runtime.app.router.routes.extend(dev_routes(runtime.core, runtime.handoffs, push))
    if runtime.core.log.latest_id() is None:
        seed_conversation(runtime.core.log, now=datetime.now(UTC))
    sock = bind_socket(host, port)
    bound_port = sock.getsockname()[1]
    print(
        "\nAriadne dev server\n"
        f"  URL:   http://{host}:{bound_port}\n"
        f"  Token: {token}\n"
        f"  State: {directory}\n\n"
        "Force app states with POST /dev/trigger/{proactive,question,fail,long,"
        "stop,share}; read recorded pushes at GET /dev/pushes.\n",
        flush=True,
    )
    await serve(runtime, sock=sock)


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ariadne-dev-server",
        description="Run the Ari app API over a scripted model and fixtures.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Use 0.0.0.0 for a phone.")
    parser.add_argument("--port", type=int, default=8737)
    parser.add_argument("--script", type=Path, help="YAML script of scripted replies.")
    parser.add_argument("--token", help="Reuse a fixed token instead of a new one.")
    parser.add_argument("--state", type=Path, help="Keep state in this directory.")
    parser.add_argument("--human", default="Divy", help="The owner's name.")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    cache = Path.home() / ".cache" / "ariadne-dev-server"
    cache.mkdir(parents=True, exist_ok=True)
    directory = (
        args.state.expanduser().resolve()
        if args.state is not None
        else Path(tempfile.mkdtemp(prefix="run-", dir=cache))
    )
    directory.mkdir(parents=True, exist_ok=True)
    asyncio.run(
        run_dev_server(
            host=args.host,
            port=args.port,
            directory=directory,
            script=load_script(args.script) if args.script else default_script(),
            token=args.token or generate_token(),
            human=args.human,
        )
    )


if __name__ == "__main__":
    main()
