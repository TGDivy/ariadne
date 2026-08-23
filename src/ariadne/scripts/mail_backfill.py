"""File existing INBOX mail, or restore a named folder back to INBOX.

The default is a read-only preview. Stop the running Ariadne process and pass
`--apply` to perform the reported moves.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from imapclient import IMAPClient  # type: ignore[import-untyped]

from ariadne.config import load_settings
from ariadne.mail import (
    IMAP_HOST,
    backfill_inbox,
    ensure_folders,
    load_routes,
    restore_folder_to_inbox,
)
from ariadne.scripts.progress import ProgressBar


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Perform the previewed moves (default: preview only)",
    )
    parser.add_argument(
        "--restore-folder",
        action="append",
        default=[],
        metavar="FOLDER",
        help="Move every message in FOLDER back to INBOX; may be repeated",
    )
    args = parser.parse_args()

    restore_folders = tuple(dict.fromkeys(args.restore_folder))
    if any(not folder.strip() for folder in restore_folders):
        parser.error("--restore-folder cannot be empty")
    if any(folder.casefold() == "inbox" for folder in restore_folders):
        parser.error("--restore-folder cannot name INBOX")

    configured = load_settings(args.config).mail_settings
    if configured is None:
        raise RuntimeError("Mail must be enabled to run backfill.")

    client = IMAPClient(IMAP_HOST, port=993, ssl=True)
    try:
        client.login(configured.username, configured.app_password.get_secret_value())
        if restore_folders:
            for folder in restore_folders:
                with ProgressBar(f"Restoring {folder!r}") as progress:
                    restored = restore_folder_to_inbox(
                        client, folder, apply=args.apply, progress=progress.update
                    )
                mode = "applied" if args.apply else "preview"
                print(
                    f"Restore {mode}: folder={folder!r}, "
                    f"found={restored.found}, moved={restored.moved}"
                )
        else:
            routes = load_routes(configured.routes)
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

    if restore_folders:
        if not args.apply:
            print(
                "Re-run with --apply to move every message in the named "
                "folder(s) to INBOX."
            )
    else:
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
