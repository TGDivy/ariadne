"""Administrative CLI for Ariadne's private browser daemon."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Never

from pydantic import ValidationError

from ..config import load_settings
from .client import BrowserClient, BrowserRemoteError
from .daemon import run_daemon
from .models import BrowserConfigurationError, BrowserError


class BrowserArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        _emit_failure("invalid_arguments", message)
        raise SystemExit(2)


def _emit_failure(code: str, message: str, *, retryable: bool = False) -> None:
    print(
        json.dumps(
            {"error": {"code": code, "message": message, "retryable": retryable}},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        file=sys.stderr,
    )


def build_parser() -> BrowserArgumentParser:
    parser = BrowserArgumentParser(
        prog="ariadne-browser",
        description="Administer Ariadne's private persistent Chromium service.",
    )
    parser.add_argument("--config", type=Path, help="private Ariadne TOML path")
    parser.add_argument("--pretty", action="store_true", help="indent JSON output")
    actions = parser.add_subparsers(dest="command", required=True)
    actions.add_parser("serve", help="run the long-lived browser service")
    actions.add_parser("health", help="check the running service")
    actions.add_parser("shutdown", help="deliberately stop the running service")

    profile = actions.add_parser("profile", help="manage named persistent profiles")
    profile_actions = profile.add_subparsers(dest="profile_command", required=True)
    create = profile_actions.add_parser("create", help="create a private profile")
    create.add_argument("name")
    profile_actions.add_parser("list", help="list configured profiles")
    inspect = profile_actions.add_parser("inspect", help="inspect profile ownership")
    inspect.add_argument("name")

    journal = actions.add_parser("journal", help="read the concise private action log")
    journal.add_argument("--limit", type=int, default=100)
    recovery = actions.add_parser(
        "recovery", help="reconcile a final action after checking definitive site state"
    )
    recovery_actions = recovery.add_subparsers(dest="recovery_command", required=True)
    recovery_actions.add_parser("list", help="list uncertain final operations")
    resolve = recovery_actions.add_parser(
        "resolve", help="record the owner-verified outcome and release the profile"
    )
    resolve.add_argument("operation_id")
    resolve.add_argument(
        "--outcome",
        required=True,
        choices=("confirmed_succeeded", "not_submitted"),
    )
    return parser


async def _execute(args: argparse.Namespace) -> dict[str, Any] | None:
    settings = load_settings(args.config)
    if not settings.browser.enabled:
        raise BrowserConfigurationError(
            "Browser control is disabled in Ariadne's private configuration."
        )
    if args.command == "serve":
        await run_daemon(settings.browser)
        return None
    client = BrowserClient(
        settings.browser.socket,
        timeout_seconds=settings.browser.action_timeout_seconds + 5,
    )
    if args.command == "health":
        return await client.call("health")
    if args.command == "shutdown":
        return await client.call("shutdown")
    if args.command == "journal":
        return await client.call("journal.list", limit=args.limit)
    if args.command == "recovery":
        if args.recovery_command == "list":
            return await client.call("recovery.list")
        return await client.call(
            "recovery.resolve",
            operation_id=args.operation_id,
            outcome=args.outcome,
        )
    if args.command == "profile":
        if args.profile_command == "create":
            return await client.call("profiles.create", name=args.name)
        if args.profile_command == "list":
            return await client.call("profiles.list")
        return await client.call("profiles.inspect", name=args.name)
    raise AssertionError("argparse accepted an unknown browser command")


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        payload = asyncio.run(_execute(args))
        if payload is not None:
            print(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    indent=2 if args.pretty else None,
                    separators=None if args.pretty else (",", ":"),
                )
            )
    except BrowserRemoteError as error:
        _emit_failure(error.code, error.message, retryable=error.retryable)
        raise SystemExit(6 if error.retryable else 1) from error
    except (BrowserError, ValidationError, ValueError) as error:
        _emit_failure("browser_configuration", str(error))
        raise SystemExit(2) from error


__all__ = ["build_parser", "main"]
