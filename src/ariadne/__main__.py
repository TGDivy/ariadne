"""Run Ariadne's Telegram conversation and optional mail loops."""

import argparse
import asyncio
import json
import logging
import secrets
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from pydantic import ValidationError
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from .codex import CodexConversation
from .codex.resolver import resolve_profile
from .config import Settings, config_path, load_settings, settings_payload
from .mail import MailLoop
from .profile import TELEGRAM_PROFILE
from .revisit.runtime import RevisitLoop
from .strava import (
    StravaAuthorizationRequired,
    StravaClient,
    StravaError,
    StravaTokenState,
)
from .telegram.bot import AriadneBot
from .telemetry import configure_telemetry

LOGGER = logging.getLogger(__name__)

AriadneApplication = Application[Any, Any, Any, Any, Any, Any]

COMMANDS = (
    BotCommand("new", "Start a fresh conversation"),
    BotCommand("stop", "Interrupt the turn Ariadne is working on"),
    BotCommand("settings", "Model, reasoning effort, and web research"),
)


async def publish_commands(application: AriadneApplication) -> None:
    """Put Ariadne's commands in Telegram's menu so they can be found."""
    try:
        await application.bot.set_my_commands(COMMANDS)
    except Exception:
        LOGGER.exception("Telegram command menu could not be published")


def configure_logging() -> None:
    """Configure Ariadne logs without exposing Telegram request URLs."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _load_configuration(path: Path | None) -> Settings:
    try:
        return load_settings(path)
    except ValidationError as error:
        LOGGER.error("Configuration error: %s", error)
        raise SystemExit(2) from error
    except ValueError as error:
        LOGGER.error("Configuration error: %s", error)
        raise SystemExit(2) from error


def _strava_client(settings: Settings) -> StravaClient:
    if not settings.strava.enabled:
        raise ValueError("Strava is disabled. Set [strava].enabled = true first.")
    assert settings.strava.client_id is not None
    assert settings.strava.client_secret is not None
    return StravaClient(
        settings.strava.client_id,
        settings.strava.client_secret.get_secret_value(),
        StravaTokenState(settings.strava.state.resolve()),
    )


def _strava_status(settings: Settings) -> dict[str, object]:
    tokens = _strava_client(settings).state.load()
    return {
        "configured": True,
        "connected": tokens is not None,
        "athlete_id": tokens.athlete_id if tokens is not None else None,
        "scope": tokens.scope if tokens is not None else None,
        "expires_at": tokens.expires_at if tokens is not None else None,
    }


def _authorize_strava(settings: Settings) -> None:
    """Receive one localhost OAuth callback and exchange its short-lived code."""
    client = _strava_client(settings)
    redirect = urlsplit(settings.strava.redirect_uri)
    assert redirect.hostname is not None
    assert redirect.port is not None
    expected_state = secrets.token_urlsafe(32)
    callback: dict[str, str] = {}

    class CallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != redirect.path:
                self.send_error(404)
                return
            if query.get("state", [None])[0] != expected_state:
                self.send_error(400, "The Strava authorization state did not match.")
                return
            if "error" in query:
                callback["error"] = query["error"][0]
                self.send_error(400, "Strava authorization was not granted.")
                return
            code = query.get("code", [None])[0]
            if code is None:
                self.send_error(400, "Strava did not return an authorization code.")
                return
            callback["code"] = code
            callback["scope"] = query.get("scope", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<p>Strava is connected to Ariadne. You can close this tab.</p>"
            )

        def log_message(self, *_: object) -> None:
            return None

    server = HTTPServer((redirect.hostname, redirect.port), CallbackHandler)
    server.timeout = 300
    try:
        print("Open this private Strava authorization URL in a browser:")
        print(client.authorization_url(settings.strava.redirect_uri, expected_state))
        print("Waiting up to five minutes for the localhost callback…")
        server.handle_request()
    finally:
        server.server_close()
    if "error" in callback:
        raise ValueError(f"Strava authorization was not granted: {callback['error']}")
    code = callback.get("code")
    if code is None:
        raise ValueError(
            "No Strava authorization callback arrived within five minutes."
        )
    result = client.exchange_authorization_code(code)
    print(json.dumps({"connected": True, **result}, indent=2))


def main() -> None:
    """Start Ariadne or inspect its typed configuration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Path to Ariadne's TOML config")
    subparsers = parser.add_subparsers(dest="command")
    config_parser = subparsers.add_parser("config", help="Validate configuration")
    config_parser.add_argument("action", choices=("check", "show"))
    strava_parser = subparsers.add_parser("strava", help="Connect or inspect Strava")
    strava_parser.add_argument("action", choices=("authorize", "status"))
    args = parser.parse_args()

    configure_logging()
    settings = _load_configuration(args.config)
    if args.command == "config":
        if args.action == "show":
            print(json.dumps(settings_payload(settings), indent=2))
        else:
            print(f"Configuration is valid: {config_path(args.config)}")
        return
    if args.command == "strava":
        try:
            if args.action == "authorize":
                _authorize_strava(settings)
            else:
                print(json.dumps(_strava_status(settings), indent=2))
        except (StravaAuthorizationRequired, StravaError, ValueError) as error:
            LOGGER.error("Strava setup failed: %s", error)
            raise SystemExit(2) from error
        return

    mail_settings = settings.mail_settings
    telemetry = configure_telemetry(settings.telemetry)

    conversation = CodexConversation(
        resolve_profile(
            TELEGRAM_PROFILE,
            vault=settings.vault,
            settings=settings.codex_turn_settings,
            human=settings.human_name,
            personality=settings.personality,
            mcp_environment=settings.mcp_environment,
            knowledge_root=settings.vault,
        ),
        telemetry=telemetry,
    )
    ariadne = AriadneBot(
        settings.allowed_user_id,
        conversation,
        bot_token=settings.telegram_bot_token,
        question_state=settings.telegram.state.resolve(),
    )
    try:
        mail_loop = (
            MailLoop(
                mail_settings,
                settings.vault,
                settings.mail_turn_settings,
                human=settings.human_name,
                personality=settings.personality,
                mcp_environment=settings.mcp_environment,
                telemetry=telemetry,
            )
            if mail_settings is not None
            else None
        )
        revisit_loop = RevisitLoop(
            settings.revisit_settings,
            settings.vault,
            settings.revisit_turn_settings,
            human=settings.human_name,
            personality=settings.personality,
            mcp_environment=settings.mcp_environment,
            telemetry=telemetry,
        )
    except ValueError as error:
        telemetry.shutdown()
        LOGGER.error("Configuration error: %s", error)
        raise SystemExit(2) from error
    mail_task: asyncio.Task[None] | None = None
    revisit_task: asyncio.Task[None] | None = None

    async def start_services(application: AriadneApplication) -> None:
        nonlocal mail_task, revisit_task
        ariadne.bind_bot(application.bot)
        await ariadne.recover_questions()
        await publish_commands(application)
        if mail_loop is not None:
            mail_task = asyncio.create_task(mail_loop.run_forever())
            LOGGER.info("Started iCloud Mail source")
        revisit_task = asyncio.create_task(revisit_loop.run_forever())
        LOGGER.info("Started one-off revisit source")

    async def close_services(_: object) -> None:
        if mail_loop is not None:
            mail_loop.stop()
        revisit_loop.stop()
        if mail_task is not None:
            mail_task.cancel()
            with suppress(asyncio.CancelledError):
                await mail_task
        if revisit_task is not None:
            revisit_task.cancel()
            with suppress(asyncio.CancelledError):
                await revisit_task
        try:
            await conversation.close()
        except Exception:
            LOGGER.exception("Failed to close Codex client")
        await asyncio.to_thread(telemetry.shutdown)

    application = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(True)
        .post_init(start_services)
        .post_shutdown(close_services)
        .build()
    )
    application.add_handler(CommandHandler("start", ariadne.start))
    application.add_handler(CommandHandler("new", ariadne.new))
    application.add_handler(CommandHandler("stop", ariadne.stop))
    application.add_handler(CommandHandler("settings", ariadne.settings))
    application.add_handler(
        CallbackQueryHandler(ariadne.settings_callback, pattern=r"^settings:")
    )
    application.add_handler(
        CallbackQueryHandler(ariadne.file_delivery_callback, pattern=r"^file-delivery:")
    )
    application.add_handler(
        CallbackQueryHandler(ariadne.turn_callback, pattern=r"^turn:")
    )
    application.add_handler(
        CallbackQueryHandler(ariadne.question_callback, pattern=r"^question:")
    )
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, ariadne.text)
    )
    application.add_handler(
        MessageHandler(filters.PHOTO | filters.Document.IMAGE, ariadne.image)
    )
    application.add_handler(
        MessageHandler(filters.Document.ALL & ~filters.Document.IMAGE, ariadne.document)
    )
    # PTB 22.8 predates Rich Messages, but retains the raw field in
    # Message.api_kwargs. A second handler group lets us inspect those updates
    # without competing with the native text/media handlers above.
    application.add_handler(TypeHandler(Update, ariadne.rich_message), group=1)

    LOGGER.info("Starting Ariadne with private knowledge at %s", settings.vault)
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES)
    except Exception:
        LOGGER.exception("Telegram polling stopped unexpectedly")
        raise


if __name__ == "__main__":
    main()
