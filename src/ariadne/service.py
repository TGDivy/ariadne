"""Run Ariadne's app API and its background services."""

import asyncio
import logging
import os
import sys
from pathlib import Path

from pydantic import ValidationError

from .config import CONFIG_PATH_ENVIRONMENT, Settings, config_path, load_settings
from .runtime import bind_socket, build_runtime, serve
from .telemetry import configure_telemetry

LOGGER = logging.getLogger(__name__)


def configure_logging() -> None:
    """Configure Ariadne logs without exposing request URLs or headers."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _load_configuration(path: Path | None) -> Settings:
    try:
        return load_settings(path)
    except ValidationError as error:
        LOGGER.error("Configuration error: %s", error)
        raise SystemExit(2) from error
    except ValueError as error:
        LOGGER.error("Configuration error: %s", error)
        raise SystemExit(2) from error


def _configure_cli_environment(path: Path | None) -> None:
    """Make the selected config and sibling console script visible to Codex."""
    os.environ[CONFIG_PATH_ENVIRONMENT] = str(config_path(path))
    # Resolve the directory, not the Python symlink, so a virtualenv's sibling
    # `ariadne` console script is not accidentally replaced by /usr/bin.
    executable_directory = str(Path(sys.executable).parent.resolve())
    current = os.environ.get("PATH", "")
    entries = current.split(os.pathsep) if current else []
    if executable_directory not in entries:
        os.environ["PATH"] = os.pathsep.join((executable_directory, *entries))


def run(path: Path | None = None) -> None:
    """Start Ariadne using the selected private TOML configuration."""
    configure_logging()
    settings = _load_configuration(path)
    # The model process inherits this path; provider credentials remain in the
    # private file and are loaded by the `ariadne` CLI on demand.
    _configure_cli_environment(path)
    telemetry = configure_telemetry(settings.telemetry)
    try:
        runtime = build_runtime(settings, telemetry=telemetry)
        sock = bind_socket(settings.app.host, settings.app.port)
    except (OSError, ValueError) as error:
        telemetry.shutdown()
        LOGGER.error("Startup error: %s", error)
        raise SystemExit(2) from error
    if not settings.app.devices:
        LOGGER.warning(
            "No phone is paired yet; run `ariadne-app pair` and add the printed "
            "[[app.devices]] entry to the private configuration."
        )
    LOGGER.info(
        "Starting Ariadne on %s:%d with private knowledge at %s",
        settings.app.host,
        settings.app.port,
        settings.vault,
    )
    try:
        asyncio.run(serve(runtime, sock=sock))
    finally:
        telemetry.shutdown()
