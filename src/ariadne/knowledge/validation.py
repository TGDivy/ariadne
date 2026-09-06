"""Read-only validation of a complete v2 knowledge collection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .documents import (
    ARCHIVE_DIRECTORY,
    StoredKnowledge,
    markdown_paths,
    parse_document,
)
from .models import KnowledgeValidationError
from .paths import filename_matches_title

_KEBAB_FILE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.md$")
_KEBAB_FOLDER = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_LEGACY_WRAPPER = "records"
_RESERVED_FOLDER_ROOTS = {ARCHIVE_DIRECTORY, _LEGACY_WRAPPER}


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
    for record in records:
        metadata = record.metadata
        try:
            relative = record.path.relative_to(root)
        except ValueError as error:
            raise KnowledgeValidationError(
                f"Knowledge record {metadata.id!r} is outside the repository."
            ) from error
        archived = relative.parts[0] == ARCHIVE_DIRECTORY
        folder_parts = relative.parts[1:-1] if archived else relative.parts[:-1]
        folder = "/".join(folder_parts)
        if folder_parts and folder_parts[0] in _RESERVED_FOLDER_ROOTS:
            raise KnowledgeValidationError(
                f"Knowledge folder {folder_parts[0]!r} is reserved by the repository."
            )
        invalid_folders = [
            part for part in folder_parts if _KEBAB_FOLDER.fullmatch(part) is None
        ]
        if invalid_folders:
            raise KnowledgeValidationError(
                "Knowledge folders must be lowercase kebab-case: "
                + ", ".join(invalid_folders)
            )
        if record.folder != folder or record.archived != archived:
            raise KnowledgeValidationError(
                f"Knowledge record {metadata.id!r} has inconsistent location state."
            )
        if metadata.id == "now" and (folder or archived):
            raise KnowledgeValidationError(
                "The current-context record with id 'now' must be active at the "
                "repository root."
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
    archive = resolved / ARCHIVE_DIRECTORY
    if not archive.is_dir():
        raise KnowledgeValidationError(
            f"Knowledge repository needs an {ARCHIVE_DIRECTORY}/ directory."
        )
    records = tuple(
        parse_document(path, root=resolved) for path in markdown_paths(resolved)
    )
    validate_records(resolved, records)
    return KnowledgeValidationReport(
        records=len(records),
        links=sum(len(record.metadata.links) for record in records),
        archived=sum(record.archived for record in records),
    )
