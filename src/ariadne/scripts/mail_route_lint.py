"""Lint ordered mail routes against the current mailbox, without mutations."""

from __future__ import annotations

import argparse
from pathlib import Path

from imapclient import IMAPClient  # type: ignore[import-untyped]

from ariadne.config import load_settings
from ariadne.mail import IMAP_HOST, RouteLintReport, lint_mail_routes, load_routes
from ariadne.scripts.progress import ProgressBar


def render_report(report: RouteLintReport) -> str:
    lines = [
        f"Scanned {report.scanned} messages; unmatched={report.unmatched}",
        "Rules (ordered; selected means first-match winner):",
    ]
    for index, rule in enumerate(report.rules, start=1):
        fully_shadowed = rule.matches > 0 and rule.selected == 0
        suffix = ", fully_shadowed=yes" if fully_shadowed else ""
        lines.append(
            f"{index}. {rule.route_id} [{rule.action}]: matches={rule.matches}, "
            f"selected={rule.selected}, shadowed={rule.shadowed}{suffix}"
        )
        lines.append("   sample subjects:")
        if rule.sample_subjects:
            lines.extend(f"     - {subject}" for subject in rule.sample_subjects)
        else:
            lines.append("     - (none)")

    lines.append("Overlaps:")
    if report.overlaps:
        lines.extend(
            f"- {overlap.earlier_route_id} + {overlap.later_route_id}: "
            f"{overlap.matches}"
            for overlap in report.overlaps
        )
    else:
        lines.append("- none")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--folder", default="INBOX")
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")

    configured = load_settings(args.config).mail_settings
    if configured is None:
        raise RuntimeError("Mail must be enabled to lint routes.")
    routes = load_routes(configured.routes)

    client = IMAPClient(IMAP_HOST, port=993, ssl=True)
    try:
        client.login(configured.username, configured.app_password.get_secret_value())
        with ProgressBar(f"Linting routes in {args.folder!r}") as progress:
            report = lint_mail_routes(
                client,
                routes,
                mailbox=args.folder,
                batch_size=args.batch_size,
                progress=progress.update,
            )
        print(render_report(report))
    finally:
        try:
            client.logout()
        except Exception:
            pass


if __name__ == "__main__":
    main()
