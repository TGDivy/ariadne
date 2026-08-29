"""Preview adoption of Ariadne's semantic knowledge record format."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ariadne.knowledge.migration import inspect_migration


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path, help="Markdown/Git knowledge repository")
    args = parser.parse_args()
    print(
        json.dumps(
            inspect_migration(args.root).payload(),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
