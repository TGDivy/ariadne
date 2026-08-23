"""File existing INBOX mail using deterministic routes, without invoking Iris.

The default is a read-only preview. Stop the running Ariadne process and pass
`--apply` to perform the reported moves.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from imapclient import IMAPClient  # type: ignore[import-untyped]

from ariadne.mail import IMAP_HOST, backfill_inbox, ensure_folders, load_routes


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Move deterministic route matches (default: preview only)",
    )
    args = parser.parse_args()

    username = required_environment("ICLOUD_USERNAME")
    password = required_environment("ICLOUD_APP_PASSWORD")
    routes_path = Path(required_environment("ARIADNE_MAIL_ROUTES")).expanduser()
    routes = load_routes(routes_path)

    client = IMAPClient(IMAP_HOST, port=993, ssl=True)
    try:
        client.login(username, password)
        if args.apply:
            ensure_folders(client, routes)
        summary = backfill_inbox(client, routes, apply=args.apply)
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
