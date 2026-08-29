"""Inspect or explicitly run Ariadne's checked-in behaviour stories."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path

from openai_codex.generated.v2_all import ReasoningEffort

from ariadne.behavior import SCENARIOS, get_scenario
from ariadne.behavior.runner import (
    BehaviorRunProfile,
    TimelineEntry,
    render_report,
    run_scenario,
)
from ariadne.config import load_settings
from ariadne.profile import MAIL_PROFILE, TELEGRAM_PROFILE, profile_for_attention


def _render_scenario(identifier: str) -> str:
    scenario = get_scenario(identifier)
    prompt = scenario.turn_input(Path("/scenario/thread"))
    calendar = [
        f"- `{event.id}` — {event.title}, {event.start} to {event.end}"
        for event in scenario.calendar
    ] or ["- empty"]
    lines = [
        f"# {scenario.title}",
        "",
        f"ID: `{scenario.identifier}`",
        f"Profile: `{scenario.profile_name}`",
        "",
        scenario.description,
        "",
        "## Disposable Thread files",
        "",
        *(f"- `{fixture.path}`" for fixture in scenario.files),
        "",
        "## Initial private knowledge",
        "",
        *(f"- `{record.id}` — {record.summary}" for record in scenario.knowledge),
        "",
        "## Initial calendar",
        "",
        *calendar,
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


def _show_progress(entry: TimelineEntry) -> None:
    text = entry.text.replace("\n", " ").strip()
    print(f"[{entry.kind}] {text}", file=sys.stderr, flush=True)


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
    run.add_argument("--human", default="Divy")
    run.add_argument("--personality", type=Path)
    run.add_argument("--output", type=Path)
    run.add_argument("--json", action="store_true")
    run.add_argument("--model", help="override the model for this manual run")
    run.add_argument(
        "--effort",
        choices=("low", "medium", "high"),
        help="override reasoning effort for this manual run",
    )
    args = parser.parse_args()

    if args.command == "list":
        for scenario in SCENARIOS:
            print(f"{scenario.identifier}\t{scenario.title}")
        return
    if args.command == "show":
        print(_render_scenario(args.scenario), end="")
        return

    scenario = get_scenario(args.scenario)
    if scenario.telegram_prompt is not None:
        declaration = TELEGRAM_PROFILE
    elif scenario.revisit is not None:
        declaration = profile_for_attention(scenario.revisit.attention)
    else:
        declaration = MAIL_PROFILE
    if args.config is not None:
        settings = load_settings(args.config)
        run_profile = BehaviorRunProfile(
            human_name=settings.human_name,
            personality=settings.personality,
            settings=(
                settings.codex_turn_settings
                if scenario.telegram_prompt is not None
                else (
                    settings.revisit_turn_settings(scenario.revisit.attention)
                    if scenario.revisit is not None
                    else settings.mail_turn_settings
                )
            ),
        )
    else:
        if args.personality is not None and not args.personality.is_file():
            parser.error(f"personality file does not exist: {args.personality}")
        run_profile = BehaviorRunProfile(
            human_name=args.human,
            personality=args.personality,
            settings=declaration.settings,
        )
    turn_settings = run_profile.settings
    if args.model is not None:
        turn_settings = replace(turn_settings, model=args.model)
    if args.effort is not None:
        turn_settings = replace(
            turn_settings,
            effort=ReasoningEffort(args.effort),
        )
    if turn_settings is not run_profile.settings:
        run_profile = replace(
            run_profile,
            settings=turn_settings,
        )
    print(
        f"Running {scenario.identifier!r} with local Codex; this may incur usage.",
        file=sys.stderr,
    )
    report = asyncio.run(run_scenario(scenario, run_profile, progress=_show_progress))
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
