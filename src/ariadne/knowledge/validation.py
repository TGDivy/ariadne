"""Read-only validation of a complete canonical knowledge collection."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .documents import StoredKnowledge, markdown_paths, parse_document
from .models import KnowledgeValidationError
from .paths import filename_matches_title

_KEBAB_FILE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*\.md$")


@dataclass(frozen=True, slots=True)
class KnowledgeValidationReport:
    """A compact successful validation result for people and automation."""

    records: int
    relationships: int
    archived: int


def validate_records(root: Path, records: tuple[StoredKnowledge, ...]) -> None:
    """Validate whole-collection invariants that one file cannot prove alone."""
    by_id: dict[str, StoredKnowledge] = {}
    names: dict[str, str] = {}
    for record in records:
        metadata = record.metadata
        relative = record.path.relative_to(root)
        expected_parent = Path(metadata.kind) / Path(metadata.collection)
        if relative.parent != expected_parent:
            raise KnowledgeValidationError(
                f"Knowledge record {metadata.id!r} must live under "
                f"{expected_parent.as_posix()}/."
            )
        if _KEBAB_FILE.fullmatch(relative.name) is None:
            raise KnowledgeValidationError(
                f"Knowledge filename {relative.name!r} must be lowercase kebab-case."
            )
        if not filename_matches_title(relative.name, metadata.title):
            raise KnowledgeValidationError(
                f"Knowledge filename {relative.name!r} does not match title "
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
        seen: set[tuple[str, str]] = set()
        for relation in record.metadata.related:
            edge = (relation.record, relation.relation)
            if edge in seen:
                raise KnowledgeValidationError(
                    f"Knowledge record {record.metadata.id!r} repeats relationship "
                    f"{relation.relation!r} to {relation.record!r}."
                )
            seen.add(edge)
            if relation.record == record.metadata.id:
                raise KnowledgeValidationError(
                    f"Knowledge record {record.metadata.id!r} relates to itself."
                )
            if relation.record not in by_id:
                raise KnowledgeValidationError(
                    f"Knowledge relation from {record.metadata.id!r} points to "
                    f"missing record {relation.record!r}."
                )


def validate_repository(root: Path) -> KnowledgeValidationReport:
    """Validate every Markdown record and all references without changing it."""
    resolved = root.resolve()
    if not resolved.is_dir():
        raise KnowledgeValidationError(
            f"Knowledge repository {resolved} is not a directory."
        )
    records = tuple(parse_document(path) for path in markdown_paths(resolved))
    validate_records(resolved, records)
    return KnowledgeValidationReport(
        records=len(records),
        relationships=sum(len(record.metadata.related) for record in records),
        archived=sum(record.metadata.archived_at is not None for record in records),
    )
