"""Operator command that pairs a phone with this Ariadne instance."""

from __future__ import annotations

import argparse
import re
from collections.abc import Sequence

from .auth import generate_token, token_hash

DEVICE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def pairing_snippet(device_id: str, name: str, token: str) -> str:
    """Return the private TOML entry that admits exactly this token."""
    escaped = name.replace("\\", "\\\\").replace('"', '\\"')
    return (
        "[[app.devices]]\n"
        f'id = "{device_id}"\n'
        f'name = "{escaped}"\n'
        f'token_sha256 = "{token_hash(token)}"\n'
    )


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="ariadne-app",
        description="Administer phones that may use this Ariadne instance.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    pair = commands.add_parser(
        "pair", help="Create a token for one phone and print its config entry."
    )
    pair.add_argument("--id", default="iphone", help="Short device id, e.g. iphone.")
    pair.add_argument("--name", default="iPhone", help="Readable device name.")
    args = parser.parse_args(argv)
    if not DEVICE_ID.fullmatch(args.id):
        parser.error("--id must be lowercase letters, digits, and hyphens.")
    token = generate_token()
    print("Enter this token in Ari once. It is not stored anywhere else:\n")
    print(f"    {token}\n")
    print("Add this entry to the private configuration, then restart Ariadne:\n")
    print(pairing_snippet(args.id, args.name, token))


if __name__ == "__main__":
    main()
