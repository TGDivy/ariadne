"""Git-backed implementation of Ariadne's compact knowledge capability."""

from __future__ import annotations

import fcntl
import os
import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

from pydantic import TypeAdapter, ValidationError

from .documents import (
    ARCHIVE_DIRECTORY,
    StoredKnowledge,
    markdown_paths,
    parse_document,
    render_document,
)
from .models import (
    Folder,
    KnowledgeConflict,
    KnowledgeFolderSummary,
    KnowledgeListing,
    KnowledgeListRecord,
    KnowledgeMetadata,
    KnowledgeRecord,
    KnowledgeSearchResult,
    KnowledgeSyncError,
    KnowledgeValidationError,
)
from .paths import slug
from .search import KnowledgeIndex
from .validation import validate_records

CURRENT_CONTEXT_ID = "now"
_FOLDER_ADAPTER = TypeAdapter(Folder)
_RESERVED_FOLDER_ROOTS = {ARCHIVE_DIRECTORY, "records"}


class KnowledgeStore:
    """Own canonical records while hiding files, Git, and derived indexing."""

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
        if not (self.root / ARCHIVE_DIRECTORY).is_dir():
            raise KnowledgeValidationError(
                f"The knowledge store needs an {ARCHIVE_DIRECTORY}/ directory."
            )
        self._index: KnowledgeIndex | None = None
        self._indexed_head: str | None = None
        self._index_lock = RLock()

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
        with self._index_lock:
            head = self._head()
            if self._index is not None and self._indexed_head == head:
                return self._index
            try:
                records = tuple(
                    parse_document(path, root=self.root)
                    for path in markdown_paths(self.root)
                )
                validate_records(self.root, records)
            except KnowledgeValidationError as error:
                raise KnowledgeValidationError(
                    "Private knowledge is invalid and needs operator review."
                ) from error
            self._index = KnowledgeIndex(records)
            self._indexed_head = head
            return self._index

    def _invalidate_index(self) -> None:
        with self._index_lock:
            self._index = None
            self._indexed_head = None

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
        self._invalidate_index()

    @staticmethod
    def _validate_links(
        identifier: str,
        links: Sequence[str],
        records: dict[str, StoredKnowledge],
    ) -> None:
        missing = sorted(set(links) - records.keys())
        if missing:
            raise KnowledgeValidationError(
                "Linked knowledge does not exist: " + ", ".join(missing)
            )
        if identifier in links:
            raise KnowledgeValidationError("A knowledge record cannot link to itself.")

    @staticmethod
    def _record(index: KnowledgeIndex, record: StoredKnowledge) -> KnowledgeRecord:
        return KnowledgeRecord(
            metadata=record.metadata,
            body=record.body,
            folder=record.folder,
            archived=record.archived,
            links=index.links(record.metadata.id),
        )

    def search(
        self,
        query: str,
        *,
        folder: str | None = None,
        include_archived: bool = False,
        limit: int = 10,
    ) -> tuple[KnowledgeSearchResult, ...]:
        """Search canonical records without exposing storage mechanics."""
        normalized = self._folder(folder) if folder is not None else None
        return self._load_index().search(
            query,
            folder=normalized,
            include_archived=include_archived,
            limit=limit,
        )

    def list_folder(
        self,
        folder: str = "",
        *,
        archived: bool = False,
        limit: int = 50,
    ) -> KnowledgeListing:
        """List only immediate semantic child folders and direct records."""
        if not 1 <= limit <= 50:
            raise KnowledgeValidationError("Knowledge list limit must be 1 to 50.")
        normalized = self._folder(folder)
        records = tuple(
            record
            for record in self._load_index().records.values()
            if record.archived == archived
        )
        direct = sorted(
            (record for record in records if record.folder == normalized),
            key=lambda record: (
                record.metadata.title.casefold(),
                record.metadata.id,
            ),
        )
        child_counts: dict[str, int] = {}
        prefix = f"{normalized}/" if normalized else ""
        for record in records:
            if normalized and not record.folder.startswith(prefix):
                continue
            remainder = record.folder[len(prefix) :] if prefix else record.folder
            if not remainder:
                continue
            child = remainder.split("/", 1)[0]
            child_folder = f"{prefix}{child}"
            child_counts[child_folder] = child_counts.get(child_folder, 0) + 1
        if normalized and not direct and not child_counts:
            state = "archived " if archived else ""
            raise KnowledgeValidationError(
                f"The {state}knowledge folder {normalized!r} does not exist."
            )
        folders = tuple(
            KnowledgeFolderSummary(folder=name, record_count=count)
            for name, count in sorted(child_counts.items())[:limit]
        )
        listed_records = tuple(
            KnowledgeListRecord(id=record.metadata.id, title=record.metadata.title)
            for record in direct[:limit]
        )
        return KnowledgeListing(
            folder=normalized,
            archived=archived,
            folders=folders,
            folder_count=len(child_counts),
            folders_truncated=len(child_counts) > limit,
            records=listed_records,
            record_count=len(direct),
            records_truncated=len(direct) > limit,
        )

    def read(self, identifiers: Sequence[str]) -> tuple[KnowledgeRecord, ...]:
        """Read a bounded set of records and compact direct links."""
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
            self._record(index, index.records[identifier]) for identifier in identifiers
        )

    def current_context(self) -> KnowledgeRecord | None:
        """Return the concise current context when the Thread defines one."""
        index = self._load_index()
        record = index.records.get(CURRENT_CONTEXT_ID)
        if record is None or record.archived:
            return None
        return self._record(index, record)

    @staticmethod
    def _available_identifier(title: str, occupied_names: set[str]) -> str:
        base = slug(title)
        candidate = base
        suffix = 2
        while candidate.casefold() in occupied_names:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _folder(value: str) -> str:
        try:
            normalized = _FOLDER_ADAPTER.validate_python(value)
        except ValidationError as error:
            raise KnowledgeValidationError(
                "A knowledge folder must be a relative lowercase kebab-case path."
            ) from error
        root = normalized.partition("/")[0]
        if root in _RESERVED_FOLDER_ROOTS:
            raise KnowledgeValidationError(
                f"Knowledge folder {root!r} is reserved by the repository."
            )
        return normalized

    def _available_path(
        self,
        title: str,
        *,
        folder: str,
        archived: bool,
        exclude: Path | None = None,
    ) -> Path:
        directory = self.root / ARCHIVE_DIRECTORY if archived else self.root
        if folder:
            directory = directory.joinpath(*folder.split("/"))
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

    def _prune_empty_parents(self, directory: Path) -> None:
        stops = {self.root, self.root / ARCHIVE_DIRECTORY}
        while directory not in stops and directory.is_relative_to(self.root):
            try:
                directory.rmdir()
            except OSError:
                return
            directory = directory.parent

    def _commit(self, message: str, paths: Sequence[Path]) -> None:
        relative = [str(path.relative_to(self.root)) for path in paths]
        self._git("add", "--", *relative)
        self._git("commit", "--quiet", "-m", message, "--", *relative)
        self._git("push", "--quiet")
        self._invalidate_index()

    def _restore_uncommitted(self, previous: dict[Path, bytes | None]) -> None:
        relative = [str(path.relative_to(self.root)) for path in previous]
        self._git("reset", "--quiet", "HEAD", "--", *relative)
        for path, content in previous.items():
            if content is None:
                path.unlink(missing_ok=True)
                self._prune_empty_parents(path.parent)
            else:
                self._write_atomic(path, content)

    @staticmethod
    def _metadata(**values: object) -> KnowledgeMetadata:
        try:
            return KnowledgeMetadata.model_validate(values)
        except ValidationError as error:
            raise KnowledgeValidationError(
                f"The proposed knowledge record is invalid: {error}"
            ) from error

    def _persist(
        self,
        current: StoredKnowledge,
        metadata: KnowledgeMetadata,
        body: str,
        *,
        folder: str,
        archived: bool,
        message: str,
    ) -> None:
        destination = self._available_path(
            metadata.title,
            folder=folder,
            archived=archived,
            exclude=current.path,
        )
        content = render_document(metadata, body)
        if destination == current.path and current.path.read_bytes() == content:
            raise KnowledgeConflict("The knowledge update does not change the record.")
        previous: dict[Path, bytes | None] = {current.path: current.path.read_bytes()}
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
                self._prune_empty_parents(current.path.parent)
            self._commit(message, paths)
        except (KnowledgeSyncError, OSError):
            if self._head() == previous_head:
                self._restore_uncommitted(previous)
            raise

    def create(
        self,
        *,
        title: str,
        summary: str,
        body: str,
        folder: str = "",
        aliases: Sequence[str] = (),
        links: Sequence[str] = (),
    ) -> KnowledgeRecord:
        """Create and automatically synchronize one compact record."""
        with self._mutation_lock():
            self._synchronize()
            index = self._load_index()
            occupied = {
                name.casefold()
                for record in index.records.values()
                for name in (record.metadata.id, *record.metadata.aliases)
            }
            identifier = self._available_identifier(title, occupied)
            normalized_folder = self._folder(folder)
            if identifier == CURRENT_CONTEXT_ID and normalized_folder:
                raise KnowledgeValidationError(
                    "The current-context record with id 'now' belongs at the root."
                )
            self._validate_links(identifier, links, index.records)
            collisions = sorted(
                name
                for name in aliases
                if name.casefold() in occupied
                or name.casefold() == identifier.casefold()
            )
            if collisions:
                raise KnowledgeConflict(
                    "A knowledge id or alias already uses: " + ", ".join(collisions)
                )
            metadata = self._metadata(
                id=identifier,
                title=title,
                summary=summary,
                aliases=tuple(aliases),
                links=tuple(links),
            )
            path = self._available_path(
                metadata.title,
                folder=normalized_folder,
                archived=False,
            )
            content = render_document(metadata, body)
            previous_head = self._head()
            try:
                self._write_atomic(path, content)
                self._commit(f"Remember {metadata.title}", (path,))
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
        body: str | None = None,
        folder: str | None = None,
        aliases: Sequence[str] | None = None,
        links: Sequence[str] | None = None,
        clear: Sequence[str] = (),
    ) -> KnowledgeRecord:
        """Replace supplied semantic fields on the latest version."""
        clearable = {"aliases", "links"}
        unknown_clear = set(clear) - clearable
        if unknown_clear:
            raise KnowledgeValidationError(
                "Unsupported fields to clear: " + ", ".join(sorted(unknown_clear))
            )
        supplied = {
            name
            for name, value in (("aliases", aliases), ("links", links))
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
            new_links = (
                ()
                if "links" in clear
                else tuple(links)
                if links is not None
                else current.metadata.links
            )
            self._validate_links(identifier, new_links, index.records)
            new_folder = self._folder(folder) if folder is not None else current.folder
            if identifier == CURRENT_CONTEXT_ID and new_folder:
                raise KnowledgeValidationError(
                    "The current-context record with id 'now' belongs at the root."
                )
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
                alias
                for alias in new_aliases
                if alias.casefold() in occupied
                or alias.casefold() == identifier.casefold()
            )
            if collisions:
                raise KnowledgeConflict(
                    "A knowledge id or alias already uses: " + ", ".join(collisions)
                )
            metadata = self._metadata(
                id=identifier,
                title=title if title is not None else current.metadata.title,
                summary=summary if summary is not None else current.metadata.summary,
                aliases=new_aliases,
                links=new_links,
            )
            self._persist(
                current,
                metadata,
                body if body is not None else current.body,
                folder=new_folder,
                archived=current.archived,
                message=f"Update {metadata.title}",
            )
            return self.read((identifier,))[0]

    def archive(self, identifier: str, reason: str) -> KnowledgeRecord:
        """Move one record out of ordinary recall while retaining its history."""
        if not reason.strip():
            raise KnowledgeValidationError("Archiving knowledge needs a reason.")
        with self._mutation_lock():
            self._synchronize()
            index = self._load_index()
            try:
                current = index.records[identifier]
            except KeyError as error:
                raise KnowledgeValidationError(
                    f"Knowledge {identifier!r} does not exist."
                ) from error
            if current.archived:
                raise KnowledgeConflict("This knowledge is already archived.")
            if identifier == CURRENT_CONTEXT_ID:
                raise KnowledgeValidationError(
                    "Current context cannot be archived; rewrite it instead."
                )
            body = f"{current.body.rstrip()}\n\n## Archived\n\n{reason.strip()}".strip()
            self._persist(
                current,
                current.metadata,
                body,
                folder=current.folder,
                archived=True,
                message=f"Archive {current.metadata.title}",
            )
            return self.read((identifier,))[0]
