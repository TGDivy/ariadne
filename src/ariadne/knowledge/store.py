"""Git-backed implementation of Ariadne's semantic knowledge capability."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
import tempfile
import unicodedata
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from .documents import (
    StoredKnowledge,
    markdown_paths,
    parse_document,
    render_document,
    revision_for,
)
from .models import (
    KnowledgeConflict,
    KnowledgeMetadata,
    KnowledgeRecord,
    KnowledgeRelation,
    KnowledgeSearchResult,
    KnowledgeSource,
    KnowledgeSyncError,
    KnowledgeValidationError,
)
from .search import KnowledgeIndex

_SLUG_CHARACTER = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    slug = _SLUG_CHARACTER.sub("-", normalized.casefold()).strip("-")
    return slug[:80].rstrip("-") or "record"


def _merge_sources(
    existing: Sequence[KnowledgeSource], additions: Iterable[KnowledgeSource]
) -> tuple[KnowledgeSource, ...]:
    merged = list(existing)
    known = {source.source for source in existing}
    for source in additions:
        if source.source not in known:
            merged.append(source)
            known.add(source.source)
    return tuple(merged)


class KnowledgeStore:
    """Own canonical records and hide all path and Git mechanics."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        if not self.root.is_dir() or not (self.root / ".git").exists():
            raise KnowledgeValidationError(
                "The configured knowledge store is not a Git working tree."
            )
        top_level = self._git("rev-parse", "--show-toplevel")
        if Path(top_level).resolve() != self.root:
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
        self._validate_collection(records)
        self._index = KnowledgeIndex(records)
        self._indexed_head = head
        return self._index

    @staticmethod
    def _validate_collection(records: Sequence[StoredKnowledge]) -> None:
        by_id: dict[str, StoredKnowledge] = {}
        names: dict[str, str] = {}
        for record in records:
            identifier = record.metadata.id
            if identifier in by_id:
                raise KnowledgeValidationError(
                    f"Knowledge id {identifier!r} is used by more than one record."
                )
            by_id[identifier] = record
            for name in (identifier, *record.metadata.aliases):
                folded = name.casefold()
                owner = names.get(folded)
                if owner is not None and owner != identifier:
                    raise KnowledgeValidationError(
                        f"Knowledge name {name!r} is ambiguous between records."
                    )
                names[folded] = identifier
        for record in records:
            for relation in record.metadata.related:
                if relation.record not in by_id:
                    raise KnowledgeValidationError(
                        f"Knowledge relation from {record.metadata.id!r} points to "
                        f"missing record {relation.record!r}."
                    )

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
    def _assert_revision(record: StoredKnowledge, expected: str) -> None:
        if record.revision != expected:
            raise KnowledgeConflict(
                "This knowledge changed since it was read. Read it again before "
                "updating it."
            )

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
        states: Iterable[str] = (),
        date_from: str | None = None,
        date_through: str | None = None,
        related_to: str | None = None,
        include_archived: bool = False,
        limit: int = 10,
    ) -> tuple[KnowledgeSearchResult, ...]:
        """Search canonical records without exposing their storage."""
        return self._load_index().search(
            query,
            kinds=kinds,
            states=states,
            date_from=date_from,
            date_through=date_through,
            related_to=related_to,
            include_archived=include_archived,
            limit=limit,
        )

    def read(self, identifiers: Sequence[str]) -> tuple[KnowledgeRecord, ...]:
        """Read a bounded set of records and their immediate relationships."""
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
        result = []
        for identifier in identifiers:
            stored = index.records[identifier]
            incoming = tuple(
                KnowledgeRelation(record=source, relation=relation.relation)
                for source in index.incoming_ids(identifier)
                for relation in index.records[source].metadata.related
                if relation.record == identifier
            )
            result.append(
                KnowledgeRecord(
                    metadata=stored.metadata,
                    body=stored.body,
                    revision=stored.revision,
                    incoming=incoming,
                )
            )
        return tuple(result)

    def _available_identifier(
        self, kind: str, title: str, occupied_names: set[str]
    ) -> str:
        base = f"{kind}:{_slug(title)}"
        candidate = base
        suffix = 2
        while candidate.casefold() in occupied_names:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def _available_path(self, kind: str, title: str) -> Path:
        directory = self.root / "Knowledge" / kind
        base = _slug(title)
        candidate = directory / f"{base}.md"
        suffix = 2
        while candidate.exists():
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

    def _restore_uncommitted(self, path: Path, previous: bytes | None) -> None:
        relative = str(path.relative_to(self.root))
        self._git("reset", "--quiet", "HEAD", "--", relative)
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            self._write_atomic(path, previous)

    def create(
        self,
        *,
        title: str,
        kind: str,
        body: str,
        state: str | None = None,
        aliases: Sequence[str] = (),
        starts_at: str | None = None,
        ends_at: str | None = None,
        related: Sequence[KnowledgeRelation] = (),
        sources: Sequence[KnowledgeSource] = (),
    ) -> KnowledgeRecord:
        """Create, synchronize, and return one durable record."""
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
                    kind=kind,
                    state=state,
                    aliases=tuple(aliases),
                    starts_at=starts_at,
                    ends_at=ends_at,
                    related=tuple(related),
                    created_at=now,
                    updated_at=now,
                    sources=tuple(sources),
                )
            except ValidationError as error:
                raise KnowledgeValidationError(
                    f"The proposed knowledge record is invalid: {error}"
                ) from error
            path = self._available_path(metadata.kind, metadata.title)
            content = render_document(metadata, body)
            previous_head = self._head()
            try:
                self._write_atomic(path, content)
                self._commit(f"Remember {title}", (path,))
            except (KnowledgeSyncError, OSError):
                if self._head() == previous_head:
                    self._restore_uncommitted(path, None)
                raise
            stored = StoredKnowledge(
                metadata, body.strip(), revision_for(content), path
            )
            return KnowledgeRecord(
                metadata=metadata,
                body=stored.body,
                revision=stored.revision,
            )

    def update(
        self,
        identifier: str,
        expected_revision: str,
        *,
        title: str | None = None,
        kind: str | None = None,
        body: str | None = None,
        state: str | None = None,
        aliases: Sequence[str] | None = None,
        starts_at: str | None = None,
        ends_at: str | None = None,
        related: Sequence[KnowledgeRelation] | None = None,
        sources: Sequence[KnowledgeSource] = (),
        clear: Sequence[str] = (),
    ) -> KnowledgeRecord:
        """Patch a record while rejecting stale revisions."""
        clearable = {"state", "starts_at", "ends_at", "aliases", "related"}
        unknown_clear = set(clear) - clearable
        if unknown_clear:
            raise KnowledgeValidationError(
                "Unsupported fields to clear: " + ", ".join(sorted(unknown_clear))
            )
        supplied = {
            name
            for name, value in (
                ("state", state),
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
            self._assert_revision(current, expected_revision)
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
            values = current.metadata.model_dump(by_alias=False)
            values.update(
                {
                    "title": title if title is not None else current.metadata.title,
                    "kind": kind if kind is not None else current.metadata.kind,
                    "body": body if body is not None else current.body,
                    "state": (
                        None
                        if "state" in clear
                        else state
                        if state is not None
                        else current.metadata.state
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
                    "sources": _merge_sources(current.metadata.sources, sources),
                }
            )
            new_body = str(values.pop("body"))
            try:
                metadata = KnowledgeMetadata.model_validate(values)
            except ValidationError as error:
                raise KnowledgeValidationError(
                    f"The proposed knowledge update is invalid: {error}"
                ) from error
            content = render_document(metadata, new_body)
            previous = current.path.read_bytes()
            previous_head = self._head()
            try:
                self._write_atomic(current.path, content)
                self._commit(f"Update {metadata.title}", (current.path,))
            except (KnowledgeSyncError, OSError):
                if self._head() == previous_head:
                    self._restore_uncommitted(current.path, previous)
                raise
            return KnowledgeRecord(
                metadata=metadata,
                body=new_body.strip(),
                revision=revision_for(content),
            )

    def archive(
        self, identifier: str, expected_revision: str, reason: str
    ) -> KnowledgeRecord:
        """Archive one record while retaining its content and history."""
        if not reason.strip():
            raise KnowledgeValidationError("Archiving knowledge needs a reason.")
        records = self.read((identifier,))
        record = records[0]
        if record.metadata.state == "archived":
            raise KnowledgeConflict("This knowledge is already archived.")
        note = f"\n\n## Archived\n\n{reason.strip()}"
        body = record.body + note
        return self.update(
            identifier,
            expected_revision,
            body=body,
            state="archived",
        )
