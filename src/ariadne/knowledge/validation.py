"""Read-only validation of a complete v2 knowledge collection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .documents import (
    ACTIVE_DIRECTORY,
    ARCHIVE_DIRECTORY,
    StoredKnowledge,
    markdown_paths,
    parse_document,
)
from .models import KnowledgeValidationError
from .paths import filename_matches_title

_KEBAB_FILE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.md$")


@dataclass(frozen=True, slots=True)
class KnowledgeValidationReport:
    """A compact successful validation result for people and automation."""

    records: int
    links: int
    archived: int


def validate_records(root: Path, records: tuple[StoredKnowledge, ...]) -> None:
    """Validate the few collection-wide invariants v2 retains."""
    by_id: dict[str, StoredKnowledge] = {}
    names: dict[str, str] = {}
    allowed_parents = {root / ACTIVE_DIRECTORY, root / ARCHIVE_DIRECTORY}
    for record in records:
        metadata = record.metadata
        if record.path.parent not in allowed_parents:
            raise KnowledgeValidationError(
                f"Knowledge record {metadata.id!r} must live directly under "
                f"{ACTIVE_DIRECTORY}/ or {ARCHIVE_DIRECTORY}/."
            )
        if record.archived != (record.path.parent.name == ARCHIVE_DIRECTORY):
            raise KnowledgeValidationError(
                f"Knowledge record {metadata.id!r} has inconsistent archive state."
            )
        if _KEBAB_FILE.fullmatch(record.path.name) is None:
            raise KnowledgeValidationError(
                f"Knowledge filename {record.path.name!r} must be lowercase kebab-case."
            )
        if not filename_matches_title(record.path.name, metadata.title):
            raise KnowledgeValidationError(
                f"Knowledge filename {record.path.name!r} does not match title "
                f"{metadata.title!r}."
            )
        if metadata.id in by_id:
            raise KnowledgeValidationError(
                f"Knowledge id {metadata.id!r} is used by more than one record."
            )
        by_id[metadata.id] = record
        for name in (metadata.id, *metadata.aliases):
            folded = name.casefold()
            owner = names.get(folded)
            if owner is not None and owner != metadata.id:
                raise KnowledgeValidationError(
                    f"Knowledge name {name!r} is ambiguous between records."
                )
            names[folded] = metadata.id

    for record in records:
        metadata = record.metadata
        if metadata.id in metadata.links:
            raise KnowledgeValidationError(
                f"Knowledge record {metadata.id!r} cannot link to itself."
            )
        missing = sorted(set(metadata.links) - by_id.keys())
        if missing:
            raise KnowledgeValidationError(
                f"Knowledge links from {metadata.id!r} point to missing records: "
                + ", ".join(missing)
            )


def validate_repository(root: Path) -> KnowledgeValidationReport:
    """Validate every v2 record and link without changing the repository."""
    resolved = root.resolve()
    if not resolved.is_dir():
        raise KnowledgeValidationError(
            f"Knowledge repository {resolved} is not a directory."
        )
    active = resolved / ACTIVE_DIRECTORY
    archive = resolved / ARCHIVE_DIRECTORY
    if not active.is_dir() or not archive.is_dir():
        raise KnowledgeValidationError(
            f"Knowledge repository needs {ACTIVE_DIRECTORY}/ and {ARCHIVE_DIRECTORY}/."
        )
    records = tuple(parse_document(path) for path in markdown_paths(resolved))
    validate_records(resolved, records)
    return KnowledgeValidationReport(
        records=len(records),
        links=sum(len(record.metadata.links) for record in records),
        archived=sum(record.archived for record in records),
    )
