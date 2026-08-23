"""File existing INBOX mail using deterministic routes, without invoking Iris.

The default is a read-only preview. Stop the running Ariadne process and pass
`--apply` to perform the reported moves.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from imapclient import IMAPClient  # type: ignore[import-untyped]

from ariadne.config import load_settings
from ariadne.mail import IMAP_HOST, backfill_inbox, ensure_folders, load_routes
from ariadne.scripts.progress import ProgressBar


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Move deterministic route matches (default: preview only)",
    )
    args = parser.parse_args()

    configured = load_settings(args.config).mail_settings
    if configured is None:
        raise RuntimeError("Mail must be enabled to run backfill.")
    routes = load_routes(configured.routes)

    client = IMAPClient(IMAP_HOST, port=993, ssl=True)
    try:
        client.login(configured.username, configured.app_password.get_secret_value())
        if args.apply:
            ensure_folders(client, routes)
        label = "Applying backfill" if args.apply else "Previewing backfill"
        with ProgressBar(label) as progress:
            summary = backfill_inbox(
                client, routes, apply=args.apply, progress=progress.update
            )
    finally:
        try:
            client.logout()
        except Exception:
            pass

    mode = "applied" if args.apply else "preview"
    print(
        f"Backfill {mode}: scanned={summary.scanned}, "
        f"move_matches={summary.move_matches}, moved={summary.moved}, "
        f"iris_skipped={summary.iris_skipped}, unmatched={summary.unmatched}"
    )
    if not args.apply and summary.move_matches:
        print("Re-run with --apply to perform the deterministic moves.")


if __name__ == "__main__":
    main()
