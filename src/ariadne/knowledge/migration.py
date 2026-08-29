"""Inspect an existing Markdown vault before adopting the knowledge contract."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .documents import markdown_paths, parse_document
from .models import KnowledgeValidationError

_HEADING = re.compile(r"(?m)^#\s+(.+?)\s*$")
_MARKDOWN_LINK = re.compile(r"\[[^]]*]\(([^)]+\.md)(?:#[^)]*)?\)")
_KNOWN_SINGULARS = {
    "people": "person",
    "plans": "plan",
    "projects": "project",
    "goals": "goal",
    "tasks": "task",
    "journals": "journal",
    "experiments": "experiment",
}


def _simple_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:80].rstrip("-") or "record"


def _title(path: Path, text: str) -> str:
    match = _HEADING.search(text)
    return match.group(1).strip() if match is not None else path.stem


def _kind(root: Path, path: Path) -> str:
    relative = path.relative_to(root)
    parent = relative.parts[0].casefold() if len(relative.parts) > 1 else "note"
    return _KNOWN_SINGULARS.get(parent, _simple_slug(parent))


@dataclass(frozen=True, slots=True)
class MigrationCandidate:
    """One proposed conversion, identified by display path for human review."""

    path: str
    status: str
    proposed_id: str | None
    proposed_title: str | None
    proposed_kind: str | None
    problem: str | None


@dataclass(frozen=True, slots=True)
class MigrationReport:
    """A read-only inventory of a prospective migration."""

    root: str
    candidates: tuple[MigrationCandidate, ...]
    duplicate_ids: tuple[str, ...]
    ambiguous_names: tuple[str, ...]
    broken_links: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "root": self.root,
            "summary": {
                "total": len(self.candidates),
                "managed": sum(item.status == "managed" for item in self.candidates),
                "proposed": sum(item.status == "proposed" for item in self.candidates),
                "invalid": sum(item.status == "invalid" for item in self.candidates),
            },
            "candidates": [
                {
                    "path": item.path,
                    "status": item.status,
                    "proposed_id": item.proposed_id,
                    "proposed_title": item.proposed_title,
                    "proposed_kind": item.proposed_kind,
                    "problem": item.problem,
                }
                for item in self.candidates
            ],
            "duplicate_ids": list(self.duplicate_ids),
            "ambiguous_names": list(self.ambiguous_names),
            "broken_links": list(self.broken_links),
        }


def inspect_migration(root: Path) -> MigrationReport:
    """Inspect every Markdown file without changing the repository."""
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"Knowledge root does not exist: {root}")
    candidates: list[MigrationCandidate] = []
    identifiers: list[str] = []
    names: dict[str, set[str]] = {}
    broken_links: list[str] = []
    reserved: set[str] = set()

    paths = markdown_paths(root)
    for path in paths:
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as error:
            candidates.append(
                MigrationCandidate(relative, "invalid", None, None, None, str(error))
            )
            continue
        for target in _MARKDOWN_LINK.findall(text):
            resolved = (path.parent / target).resolve()
            if not resolved.is_relative_to(root) or not resolved.is_file():
                broken_links.append(f"{relative} -> {target}")

        existing_problem: str | None = None
        if text.startswith("---\n"):
            try:
                record = parse_document(path)
            except KnowledgeValidationError as error:
                existing_problem = str(error)
            else:
                identifier = record.metadata.id
                identifiers.append(identifier)
                reserved.add(identifier)
                for name in (
                    record.metadata.id,
                    record.metadata.title,
                    *record.metadata.aliases,
                ):
                    names.setdefault(name.casefold(), set()).add(identifier)
                candidates.append(
                    MigrationCandidate(
                        relative,
                        "managed",
                        identifier,
                        record.metadata.title,
                        record.metadata.kind,
                        None,
                    )
                )
                continue

        title = _title(path, text)
        kind = _kind(root, path)
        base = f"{kind}:{_simple_slug(title)}"
        identifier = base
        suffix = 2
        while identifier in reserved:
            identifier = f"{base}-{suffix}"
            suffix += 1
        reserved.add(identifier)
        identifiers.append(identifier)
        names.setdefault(title.casefold(), set()).add(identifier)
        candidates.append(
            MigrationCandidate(
                relative,
                "proposed",
                identifier,
                title,
                kind,
                existing_problem,
            )
        )

    duplicates = tuple(
        sorted(
            {
                identifier
                for identifier in identifiers
                if identifiers.count(identifier) > 1
            }
        )
    )
    return MigrationReport(
        root=str(root),
        candidates=tuple(candidates),
        duplicate_ids=duplicates,
        ambiguous_names=tuple(
            sorted(name for name, owners in names.items() if len(owners) > 1)
        ),
        broken_links=tuple(sorted(set(broken_links))),
    )
