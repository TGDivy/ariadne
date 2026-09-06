"""Validate a canonical Ariadne knowledge repository."""

from __future__ import annotations

import argparse
from pathlib import Path

from ariadne.knowledge.models import KnowledgeError
from ariadne.knowledge.validation import validate_repository


def main() -> None:
    """Validate a repository for local hooks or continuous integration."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    args = parser.parse_args()
    try:
        report = validate_repository(args.root)
    except KnowledgeError as error:
        parser.exit(1, f"Knowledge validation failed: {error}\n")
    print(
        f"Knowledge is valid: {report.records} records, "
        f"{report.links} links, {report.archived} archived."
    )


if __name__ == "__main__":
    main()
