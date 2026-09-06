"""Inspect the complete effective configuration of an Ariadne turn profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ariadne.codex.resolver import resolve_profile
from ariadne.config import load_settings
from ariadne.profile import PROFILES
from ariadne.prompts.inspection import profile_payload, render_profile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=tuple(PROFILES))
    parser.add_argument("--config", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    settings = load_settings(args.config)
    profile = resolve_profile(
        PROFILES[args.profile],
        vault=settings.vault,
        settings=settings.turn_settings(args.profile),
        human=settings.human_name,
        personality=settings.personality,
        mcp_environment=settings.mcp_environment,
        knowledge_root=settings.vault,
        network_domains=settings.health_network_domains,
    )

    if args.json:
        print(json.dumps(profile_payload(profile), indent=2))
    else:
        print(render_profile(profile))


if __name__ == "__main__":
    main()
