"""Inspect or explicitly run Ariadne's checked-in behaviour stories."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ariadne.behavior import SCENARIOS, get_scenario
from ariadne.behavior.runner import render_report, run_scenario
from ariadne.config import load_settings


def _render_scenario(identifier: str) -> str:
    scenario = get_scenario(identifier)
    prompt = scenario.turn_input(Path("/scenario/thread"))
    lines = [
        f"# {scenario.title}",
        "",
        f"ID: `{scenario.identifier}`",
        "Profile: `mail`",
        "",
        scenario.description,
        "",
        "## Disposable Thread files",
        "",
        *(f"- `{fixture.path}`" for fixture in scenario.files),
        "",
        "## Review questions",
        "",
        *(f"- {question}" for question in scenario.review_questions),
        "",
        "## Production-shaped event input",
        "",
        "```text",
        prompt,
        "```",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list", help="list scenarios without starting Codex")
    show = subparsers.add_parser("show", help="inspect one scenario without Codex")
    show.add_argument("scenario", choices=[item.identifier for item in SCENARIOS])
    run = subparsers.add_parser(
        "run", help="explicitly run one scenario using local Codex (not a CI command)"
    )
    run.add_argument("scenario", choices=[item.identifier for item in SCENARIOS])
    run.add_argument("--config", type=Path)
    run.add_argument("--output", type=Path)
    run.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.command == "list":
        for scenario in SCENARIOS:
            print(f"{scenario.identifier}\t{scenario.title}")
        return
    if args.command == "show":
        print(_render_scenario(args.scenario), end="")
        return

    scenario = get_scenario(args.scenario)
    settings = load_settings(args.config)
    print(
        f"Running {scenario.identifier!r} with local Codex; this may incur usage.",
        file=sys.stderr,
    )
    report = asyncio.run(run_scenario(scenario, settings))
    rendered = (
        json.dumps(report.payload(), ensure_ascii=False, indent=2) + "\n"
        if args.json
        else render_report(report)
    )
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
