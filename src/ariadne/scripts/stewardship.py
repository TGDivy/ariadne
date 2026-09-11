"""Run one owner-reviewed stewardship cycle without enabling recurrence."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from ariadne.config import load_settings
from ariadne.service import _configure_cli_environment, configure_logging
from ariadne.stewardship import StewardshipRunResult
from ariadne.stewardship.runtime import StewardshipLoop
from ariadne.telemetry import configure_telemetry


async def run_once(path: Path | None) -> StewardshipRunResult:
    """Force one cycle while leaving the configured recurring switch unchanged."""
    settings = load_settings(path)
    _configure_cli_environment(path)
    telemetry = configure_telemetry(settings.telemetry)
    loop = StewardshipLoop(
        settings.stewardship_settings,
        settings.agent_workspace,
        settings.vault,
        settings.stewardship_turn_settings,
        human=settings.human_name,
        personality=settings.personality,
        mcp_environment=settings.mcp_environment,
        network_domains=settings.health_network_domains,
        telemetry=telemetry,
        recover_running=False,
    )
    try:
        return await loop.process_due(force=True)
    finally:
        loop.stop()
        await asyncio.to_thread(telemetry.shutdown)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    configure_logging()
    result = asyncio.run(run_once(args.config))
    payload: dict[str, str] = {"status": result.status}
    if result.cycle_id is not None:
        payload["cycle_id"] = result.cycle_id
    if result.local_day is not None:
        payload["local_day"] = result.local_day.isoformat()
    if result.error is not None:
        payload["error"] = result.error
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
