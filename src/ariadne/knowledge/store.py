"""Git-backed implementation of Ariadne's semantic knowledge capability."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import tempfile
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .documents import StoredKnowledge, markdown_paths, parse_document, render_document
from .models import (
    KnowledgeConflict,
    KnowledgeMetadata,
    KnowledgeRecord,
    KnowledgeRelation,
    KnowledgeSearchResult,
    KnowledgeSyncError,
    KnowledgeValidationError,
)
from .orientation import KnowledgeOrientation
from .paths import slug
from .search import KnowledgeIndex
from .validation import validate_records

_SLUG_CHARACTER = re.compile(r"[^a-z0-9]+")


class KnowledgeStore:
    """Own canonical records and hide all filesystem and Git operations."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if not self.root.is_dir() or not (self.root / ".git").exists():
            raise KnowledgeValidationError(
                "The configured knowledge store is not a Git working tree."
            )
        if Path(self._git("rev-parse", "--show-toplevel")).resolve() != self.root:
            raise KnowledgeValidationError(
                "The knowledge store must be the root of its Git working tree."
            )
        self._index: KnowledgeIndex | None = None
        self._indexed_head: str | None = None

    def _git(self, *arguments: str) -> str:
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=self.root,
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise KnowledgeSyncError(
                "Private knowledge could not be synchronized safely."
            ) from error
        return result.stdout.strip()

    def _head(self) -> str:
        return self._git("rev-parse", "HEAD")

    def _load_index(self) -> KnowledgeIndex:
        head = self._head()
        if self._index is not None and self._indexed_head == head:
            return self._index
        records = tuple(parse_document(path) for path in markdown_paths(self.root))
        validate_records(self.root, records)
        self._index = KnowledgeIndex(records)
        self._indexed_head = head
        return self._index

    @contextmanager
    def _mutation_lock(self) -> Iterator[None]:
        lock_path = self.root / ".git" / "ariadne-knowledge.lock"
        with lock_path.open("a", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _synchronize(self) -> None:
        """Automatically fast-forward, or push a durable local commit."""
        if self._git("status", "--porcelain"):
            raise KnowledgeSyncError(
                "Private knowledge has unmanaged local changes and was not modified."
            )
        try:
            upstream = self._git("rev-parse", "--abbrev-ref", "@{upstream}")
        except KnowledgeSyncError as error:
            raise KnowledgeSyncError(
                "Private knowledge has no configured synchronization target."
            ) from error
        self._git("fetch", "--quiet")
        counts = self._git(
            "rev-list", "--left-right", "--count", f"HEAD...{upstream}"
        ).split()
        if len(counts) != 2:
            raise KnowledgeSyncError(
                "Private knowledge synchronization state could not be understood."
            )
        ahead, behind = (int(value) for value in counts)
        if ahead and behind:
            raise KnowledgeSyncError(
                "Private knowledge changed in two places and needs operator review."
            )
        if ahead:
            self._git("push", "--quiet")
        elif behind:
            self._git("merge", "--ff-only", "--quiet", upstream)
        self._index = None
        self._indexed_head = None

    @staticmethod
    def _validate_relationships(
        identifier: str,
        related: Sequence[KnowledgeRelation],
        records: dict[str, StoredKnowledge],
    ) -> None:
        missing = sorted(
            {relation.record for relation in related if relation.record not in records}
        )
        if missing:
            raise KnowledgeValidationError(
                "Related knowledge does not exist: " + ", ".join(missing)
            )
        if any(relation.record == identifier for relation in related):
            raise KnowledgeValidationError(
                "A knowledge record cannot relate to itself."
            )

    def search(
        self,
        query: str = "",
        *,
        kinds: Iterable[str] = (),
        collections: Iterable[str] = (),
        tags: Iterable[str] = (),
        date_from: str | None = None,
        date_through: str | None = None,
        related_to: str | None = None,
        include_archived: bool = False,
        limit: int = 10,
    ) -> tuple[KnowledgeSearchResult, ...]:
        """Search canonical records without exposing storage mechanics."""
        return self._load_index().search(
            query,
            kinds=kinds,
            collections=collections,
            tags=tags,
            date_from=date_from,
            date_through=date_through,
            related_to=related_to,
            include_archived=include_archived,
            limit=limit,
        )

    def read(self, identifiers: Sequence[str]) -> tuple[KnowledgeRecord, ...]:
        """Read a bounded set of records and compact direct relationships."""
        if not 1 <= len(identifiers) <= 20:
            raise KnowledgeValidationError("Read between 1 and 20 knowledge records.")
        index = self._load_index()
        missing = [
            identifier for identifier in identifiers if identifier not in index.records
        ]
        if missing:
            raise KnowledgeValidationError(
                "Knowledge does not exist: " + ", ".join(missing)
            )
        return tuple(
            KnowledgeRecord(
                metadata=index.records[identifier].metadata,
                body=index.records[identifier].body,
                relationships=index.relationships(identifier),
            )
            for identifier in identifiers
        )

    def _available_identifier(
        self, kind: str, title: str, occupied_names: set[str]
    ) -> str:
        base = f"{kind}:{slug(title)}"
        candidate = base
        suffix = 2
        while candidate.casefold() in occupied_names:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def _available_path(
        self, kind: str, collection: str, title: str, *, exclude: Path | None = None
    ) -> Path:
        directory = self.root / kind / Path(collection)
        base = slug(title)
        candidate = directory / f"{base}.md"
        suffix = 2
        while candidate.exists() and candidate != exclude:
            candidate = directory / f"{base}-{suffix}.md"
            suffix += 1
        return candidate

    @staticmethod
    def _write_atomic(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _commit(self, message: str, paths: Sequence[Path]) -> None:
        relative = [str(path.relative_to(self.root)) for path in paths]
        self._git("add", "--", *relative)
        self._git("commit", "--quiet", "-m", message, "--", *relative)
        self._git("push", "--quiet")
        self._index = None
        self._indexed_head = None

    def _restore_uncommitted(self, previous: dict[Path, bytes | None]) -> None:
        relative = [str(path.relative_to(self.root)) for path in previous]
        self._git("reset", "--quiet", "HEAD", "--", *relative)
        for path, content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                self._write_atomic(path, content)

    def create(
        self,
        *,
        title: str,
        summary: str,
        kind: str,
        collection: str,
        body: str,
        tags: Sequence[str] = (),
        aliases: Sequence[str] = (),
        starts_at: str | None = None,
        ends_at: str | None = None,
        related: Sequence[KnowledgeRelation] = (),
    ) -> KnowledgeRecord:
        """Create and automatically synchronize one canonical record."""
        with self._mutation_lock():
            self._synchronize()
            index = self._load_index()
            occupied = {
                name.casefold()
                for record in index.records.values()
                for name in (record.metadata.id, *record.metadata.aliases)
            }
            identifier = self._available_identifier(kind, title, occupied)
            self._validate_relationships(identifier, related, index.records)
            collisions = sorted(name for name in aliases if name.casefold() in occupied)
            if collisions:
                raise KnowledgeConflict(
                    "A knowledge id or alias already uses: " + ", ".join(collisions)
                )
            now = datetime.now(UTC)
            try:
                metadata = KnowledgeMetadata(
                    id=identifier,
                    title=title,
                    summary=summary,
                    kind=kind,
                    collection=collection,
                    tags=tuple(tags),
                    aliases=tuple(aliases),
                    starts_at=starts_at,
                    ends_at=ends_at,
                    related=tuple(related),
                    created_at=now,
                    updated_at=now,
                )
            except ValidationError as error:
                raise KnowledgeValidationError(
                    f"The proposed knowledge record is invalid: {error}"
                ) from error
            path = self._available_path(
                metadata.kind, metadata.collection, metadata.title
            )
            content = render_document(metadata, body)
            previous_head = self._head()
            try:
                self._write_atomic(path, content)
                self._commit(f"Remember {title}", (path,))
            except (KnowledgeSyncError, OSError):
                if self._head() == previous_head:
                    self._restore_uncommitted({path: None})
                raise
            return self.read((identifier,))[0]

    def update(
        self,
        identifier: str,
        *,
        title: str | None = None,
        summary: str | None = None,
        kind: str | None = None,
        collection: str | None = None,
        body: str | None = None,
        tags: Sequence[str] | None = None,
        aliases: Sequence[str] | None = None,
        starts_at: str | None = None,
        ends_at: str | None = None,
        related: Sequence[KnowledgeRelation] | None = None,
        clear: Sequence[str] = (),
        archive_reason: str | None = None,
    ) -> KnowledgeRecord:
        """Apply supplied semantic fields to the latest version and synchronize."""
        clearable = {"tags", "aliases", "starts_at", "ends_at", "related"}
        unknown_clear = set(clear) - clearable
        if unknown_clear:
            raise KnowledgeValidationError(
                "Unsupported fields to clear: " + ", ".join(sorted(unknown_clear))
            )
        supplied = {
            name
            for name, value in (
                ("tags", tags),
                ("aliases", aliases),
                ("starts_at", starts_at),
                ("ends_at", ends_at),
                ("related", related),
            )
            if value is not None
        }
        contradictory = set(clear) & supplied
        if contradictory:
            raise KnowledgeValidationError(
                "A field cannot be updated and cleared together: "
                + ", ".join(sorted(contradictory))
            )
        with self._mutation_lock():
            self._synchronize()
            index = self._load_index()
            try:
                current = index.records[identifier]
            except KeyError as error:
                raise KnowledgeValidationError(
                    f"Knowledge {identifier!r} does not exist."
                ) from error
            if archive_reason is not None and current.metadata.archived_at is not None:
                raise KnowledgeConflict("This knowledge is already archived.")
            new_related = (
                ()
                if "related" in clear
                else tuple(related)
                if related is not None
                else current.metadata.related
            )
            self._validate_relationships(identifier, new_related, index.records)
            new_aliases = (
                ()
                if "aliases" in clear
                else tuple(aliases)
                if aliases is not None
                else current.metadata.aliases
            )
            occupied = {
                name.casefold(): record.metadata.id
                for record in index.records.values()
                for name in (record.metadata.id, *record.metadata.aliases)
                if record.metadata.id != identifier
            }
            collisions = sorted(
                alias for alias in new_aliases if alias.casefold() in occupied
            )
            if collisions:
                raise KnowledgeConflict(
                    "A knowledge id or alias already uses: " + ", ".join(collisions)
                )
            new_body = body if body is not None else current.body
            if archive_reason is not None:
                new_body += f"\n\n## Archived\n\n{archive_reason.strip()}"
            values = current.metadata.model_dump(by_alias=False)
            values.update(
                {
                    "title": title if title is not None else current.metadata.title,
                    "summary": (
                        summary if summary is not None else current.metadata.summary
                    ),
                    "kind": kind if kind is not None else current.metadata.kind,
                    "collection": (
                        collection
                        if collection is not None
                        else current.metadata.collection
                    ),
                    "tags": (
                        ()
                        if "tags" in clear
                        else tuple(tags)
                        if tags is not None
                        else current.metadata.tags
                    ),
                    "aliases": new_aliases,
                    "starts_at": (
                        None
                        if "starts_at" in clear
                        else starts_at
                        if starts_at is not None
                        else current.metadata.starts_at
                    ),
                    "ends_at": (
                        None
                        if "ends_at" in clear
                        else ends_at
                        if ends_at is not None
                        else current.metadata.ends_at
                    ),
                    "related": new_related,
                    "updated_at": datetime.now(UTC),
                    "archived_at": (
                        datetime.now(UTC)
                        if archive_reason is not None
                        else current.metadata.archived_at
                    ),
                }
            )
            try:
                metadata = KnowledgeMetadata.model_validate(values)
            except ValidationError as error:
                raise KnowledgeValidationError(
                    f"The proposed knowledge update is invalid: {error}"
                ) from error
            destination = self._available_path(
                metadata.kind,
                metadata.collection,
                metadata.title,
                exclude=current.path,
            )
            content = render_document(metadata, new_body)
            previous: dict[Path, bytes | None] = {
                current.path: current.path.read_bytes()
            }
            paths = [current.path]
            if destination != current.path:
                previous[destination] = (
                    destination.read_bytes() if destination.exists() else None
                )
                paths.append(destination)
            previous_head = self._head()
            try:
                self._write_atomic(destination, content)
                if destination != current.path:
                    current.path.unlink()
                self._commit(f"Update {metadata.title}", paths)
            except (KnowledgeSyncError, OSError):
                if self._head() == previous_head:
                    self._restore_uncommitted(previous)
                raise
            return self.read((identifier,))[0]

    def archive(self, identifier: str, reason: str) -> KnowledgeRecord:
        """Archive one record while retaining its content and Git history."""
        if not reason.strip():
            raise KnowledgeValidationError("Archiving knowledge needs a reason.")
        return self.update(identifier, archive_reason=reason)

    def browse(
        self,
        location: str = "",
        *,
        depth: int = 2,
        include_summaries: bool = True,
        include_archived: bool = False,
    ) -> dict[str, Any]:
        """Browse the human-readable hierarchy without using paths as identity."""
        if not 1 <= depth <= 5:
            raise KnowledgeValidationError("Knowledge browse depth must be 1 to 5.")
        if location:
            parts = Path(location).parts
            if any(_SLUG_CHARACTER.search(part) for part in parts):
                raise KnowledgeValidationError(
                    "Knowledge locations use lowercase kebab-case folder names."
                )
        selected = (self.root / location).resolve()
        if not selected.is_relative_to(self.root) or not selected.is_dir():
            raise KnowledgeValidationError(
                f"Knowledge collection {location or '/'} does not exist."
            )
        index = self._load_index()
        by_parent: dict[Path, list[StoredKnowledge]] = {}
        for record in index.records.values():
            if not include_archived and record.metadata.archived_at is not None:
                continue
            by_parent.setdefault(record.path.parent, []).append(record)

        def build(directory: Path, remaining: int) -> dict[str, Any]:
            relative = directory.relative_to(self.root).as_posix()
            direct = sorted(
                by_parent.get(directory, ()),
                key=lambda item: item.metadata.title.casefold(),
            )
            records = [
                {
                    "id": record.metadata.id,
                    "title": record.metadata.title,
                    "kind": record.metadata.kind,
                    "tags": list(record.metadata.tags),
                    **(
                        {"summary": record.metadata.summary}
                        if include_summaries
                        else {}
                    ),
                }
                for record in direct
            ]
            children = []
            if remaining:
                for child in sorted(
                    (
                        path
                        for path in directory.iterdir()
                        if path.is_dir() and path.name != ".git"
                    ),
                    key=lambda path: path.name,
                ):
                    children.append(build(child, remaining - 1))
            return {
                "name": directory.name if directory != self.root else "/",
                "location": "" if directory == self.root else relative,
                "records": records,
                "collections": children,
            }

        return build(selected, depth)

    def orientation(self) -> KnowledgeOrientation:
        """Return compact vocabulary and a two-level tree for generated prompts."""
        index = self._load_index()
        active = [
            record
            for record in index.records.values()
            if record.metadata.archived_at is None
        ]
        kinds = Counter(record.metadata.kind for record in active)
        tags = Counter(tag for record in active for tag in record.metadata.tags)
        relations = Counter(
            relation.relation
            for record in active
            for relation in record.metadata.related
        )
        return {
            "kinds": dict(sorted(kinds.items())),
            "tags": dict(sorted(tags.items())),
            "relationships": dict(sorted(relations.items())),
            "collections": sorted(
                {
                    f"{record.metadata.kind}/{record.metadata.collection}"
                    for record in active
                }
            ),
        }
