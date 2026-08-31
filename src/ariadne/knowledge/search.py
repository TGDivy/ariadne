"""Transparent ranked search over canonical knowledge records."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import date
from difflib import SequenceMatcher
from threading import Lock

from .documents import StoredKnowledge
from .models import (
    KnowledgeRelationshipSummary,
    KnowledgeSearchError,
    KnowledgeSearchResult,
    KnowledgeValidationError,
)

_WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)


def _words(value: str) -> tuple[str, ...]:
    return tuple(word.casefold() for word in _WORD.findall(value))


def _search_expression(query: str) -> str:
    return " OR ".join(
        f'"{word.replace(chr(34), chr(34) * 2)}"*' for word in _words(query)
    )


def _validate_date_bounds(date_from: str | None, date_through: str | None) -> None:
    try:
        for value in (date_from, date_through):
            if value is not None:
                date.fromisoformat(value)
    except ValueError as error:
        raise KnowledgeValidationError(
            "Knowledge search date bounds must be ISO dates."
        ) from error
    if date_from is not None and date_through is not None and date_from > date_through:
        raise KnowledgeValidationError(
            "Knowledge search date_from must not be after date_through."
        )


def _overlaps(
    record: StoredKnowledge, date_from: str | None, date_through: str | None
) -> bool:
    starts = record.metadata.starts_at
    ends = record.metadata.ends_at or starts
    if (date_from is not None or date_through is not None) and starts is None:
        return False
    if date_from is not None and ends is not None and ends[:10] < date_from:
        return False
    return not (
        date_through is not None and starts is not None and starts[:10] > date_through
    )


def _excerpt(body: str, matched_terms: Sequence[str], *, length: int = 240) -> str:
    compact = " ".join(body.split())
    if not compact:
        return ""
    folded = compact.casefold()
    positions = [folded.find(term.casefold()) for term in matched_terms]
    found = [position for position in positions if position >= 0]
    centre = min(found) if found else 0
    start = max(0, centre - length // 3)
    end = min(len(compact), start + length)
    return (
        ("…" if start else "")
        + compact[start:end].strip()
        + ("…" if end < len(compact) else "")
    )


def _close_enough(left: str, right: str) -> bool:
    if min(len(left), len(right)) < 4:
        return False
    return SequenceMatcher(None, left, right).ratio() >= 0.84


def _stem(value: str) -> str:
    if value.endswith("ing") and len(value) > 5:
        return value[:-3]
    if value.endswith("ies") and len(value) > 4:
        return value[:-3] + "y"
    if value.endswith("es") and len(value) > 4:
        return value[:-2]
    if value.endswith("s") and len(value) > 3:
        return value[:-1]
    return value


def _field_matches(
    query_terms: Sequence[str], value: str, *, fuzzy: bool = True
) -> set[str]:
    tokens = _words(value)
    return {
        term
        for term in query_terms
        if any(
            _stem(token).startswith(_stem(term))
            or _stem(term).startswith(_stem(token))
            or (fuzzy and _close_enough(term, token))
            for token in tokens
        )
    }


class KnowledgeIndex:
    """An in-memory FTS5 index rebuilt from canonical Markdown."""

    def __init__(self, records: Sequence[StoredKnowledge]) -> None:
        self._records = {record.metadata.id: record for record in records}
        self._incoming: dict[str, list[tuple[str, str]]] = {}
        for record in records:
            for relation in record.metadata.related:
                self._incoming.setdefault(relation.record, []).append(
                    (record.metadata.id, relation.relation)
                )
        self._database_lock = Lock()
        self._database = sqlite3.connect(":memory:", check_same_thread=False)
        try:
            self._database.execute(
                "CREATE VIRTUAL TABLE records USING fts5("
                "id UNINDEXED, title, aliases, summary, tags, metadata, body, "
                "tokenize='porter unicode61 remove_diacritics 2')"
            )
        except sqlite3.OperationalError as error:
            raise KnowledgeValidationError(
                "This Python SQLite build does not provide the required FTS5 support."
            ) from error
        self._database.executemany(
            "INSERT INTO records(id, title, aliases, summary, tags, metadata, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    record.metadata.id,
                    record.metadata.title,
                    " ".join(record.metadata.aliases),
                    record.metadata.summary,
                    " ".join(record.metadata.tags),
                    " ".join(
                        value
                        for value in (
                            record.metadata.kind,
                            record.metadata.collection,
                            record.metadata.starts_at,
                            record.metadata.ends_at,
                        )
                        if value is not None
                    ),
                    record.body,
                )
                for record in records
            ),
        )

    @property
    def records(self) -> dict[str, StoredKnowledge]:
        return self._records

    def relationships(
        self, identifier: str
    ) -> tuple[KnowledgeRelationshipSummary, ...]:
        record = self._records[identifier]
        relationships = [
            KnowledgeRelationshipSummary(
                id=target.metadata.id,
                title=target.metadata.title,
                summary=target.metadata.summary,
                kind=target.metadata.kind,
                relation=relation.relation,
                direction="outgoing",
            )
            for relation in record.metadata.related
            if (target := self._records.get(relation.record)) is not None
        ]
        relationships.extend(
            KnowledgeRelationshipSummary(
                id=source.metadata.id,
                title=source.metadata.title,
                summary=source.metadata.summary,
                kind=source.metadata.kind,
                relation=relation,
                direction="incoming",
            )
            for source_id, relation in self._incoming.get(identifier, ())
            if (source := self._records.get(source_id)) is not None
        )
        return tuple(
            sorted(
                relationships,
                key=lambda item: (item.direction, item.title.casefold(), item.id),
            )
        )

    def _lexical_evidence(
        self, record: StoredKnowledge, query: str
    ) -> tuple[set[str], tuple[str, ...], float]:
        terms = _words(query)
        metadata = record.metadata
        fields = {
            "title": _field_matches(terms, metadata.title),
            "alias": _field_matches(terms, " ".join(metadata.aliases)),
            "summary": _field_matches(terms, metadata.summary),
            "tag": _field_matches(terms, " ".join(metadata.tags)),
            "collection": _field_matches(terms, metadata.collection),
            "body": _field_matches(terms, record.body, fuzzy=False),
        }
        matched = set().union(*fields.values())
        reasons = tuple(name for name, values in fields.items() if values)
        boost = -sum(
            len(fields[name]) * weight
            for name, weight in {
                "title": 1_000.0,
                "alias": 800.0,
                "summary": 120.0,
                "tag": 300.0,
                "collection": 100.0,
                "body": 10.0,
            }.items()
        )
        folded = query.strip().casefold()
        if folded == metadata.id.casefold():
            matched.update(terms)
            reasons = ("exact_id", *reasons)
            boost -= 1_000_000
        elif folded == metadata.title.casefold():
            reasons = ("exact_title", *reasons)
            boost -= 900_000
        elif folded in {alias.casefold() for alias in metadata.aliases}:
            reasons = ("exact_alias", *reasons)
            boost -= 800_000
        elif folded and folded in metadata.title.casefold():
            reasons = ("title_phrase", *reasons)
            boost -= 10_000
        return matched, tuple(dict.fromkeys(reasons)), boost

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
        """Return transparent ranked records satisfying exact filters."""
        if not 1 <= limit <= 50:
            raise KnowledgeValidationError("Knowledge search limit must be 1 to 50.")
        _validate_date_bounds(date_from, date_through)
        if query.strip() and not _words(query):
            raise KnowledgeValidationError(
                "Knowledge search query needs at least one searchable word."
            )
        kind_filter = {value.casefold() for value in kinds}
        collection_filter = {value.casefold() for value in collections}
        tag_filter = {value.casefold() for value in tags}
        if not query.strip() and not any(
            (
                kind_filter,
                collection_filter,
                tag_filter,
                date_from,
                date_through,
                related_to,
            )
        ):
            raise KnowledgeValidationError(
                "Knowledge search needs query text or at least one filter."
            )

        scores: dict[str, float] = {}
        expression = _search_expression(query)
        if expression:
            try:
                with self._database_lock:
                    rows = self._database.execute(
                        "SELECT id, "
                        "bm25(records, 0.0, 10.0, 8.0, 6.0, 5.0, 2.0, 1.0) "
                        "FROM records WHERE records MATCH ? ORDER BY 2",
                        (expression,),
                    ).fetchall()
            except sqlite3.Error as error:
                raise KnowledgeSearchError(
                    "Private knowledge search is temporarily unavailable. "
                    "Retry the search once."
                ) from error
            scores.update((identifier, float(score)) for identifier, score in rows)
            for identifier, record in self._records.items():
                matched, _, _ = self._lexical_evidence(record, query)
                if matched and identifier not in scores:
                    scores[identifier] = 1.0
        else:
            scores.update((identifier, 0.0) for identifier in self._records)

        related_ids: set[str] | None = None
        if related_to is not None:
            if related_to not in self._records:
                raise KnowledgeValidationError(
                    f"Related knowledge record {related_to!r} does not exist."
                )
            related_ids = {
                relationship.id for relationship in self.relationships(related_to)
            }

        matches: list[tuple[float, StoredKnowledge, set[str], tuple[str, ...]]] = []
        query_terms = _words(query)
        for identifier, score in scores.items():
            record = self._records[identifier]
            metadata = record.metadata
            if not include_archived and metadata.archived_at is not None:
                continue
            if kind_filter and metadata.kind.casefold() not in kind_filter:
                continue
            if (
                collection_filter
                and metadata.collection.casefold() not in collection_filter
            ):
                continue
            if tag_filter and not tag_filter.issubset(
                {tag.casefold() for tag in metadata.tags}
            ):
                continue
            if not _overlaps(record, date_from, date_through):
                continue
            if related_ids is not None and identifier not in related_ids:
                continue
            matched_terms, matched_by, boost = self._lexical_evidence(record, query)
            matches.append((score + boost, record, matched_terms, matched_by))

        matches.sort(key=lambda item: (item[0], item[1].metadata.title.casefold()))
        return tuple(
            KnowledgeSearchResult(
                id=record.metadata.id,
                title=record.metadata.title,
                summary=record.metadata.summary,
                kind=record.metadata.kind,
                collection=record.metadata.collection,
                tags=record.metadata.tags,
                starts_at=record.metadata.starts_at,
                ends_at=record.metadata.ends_at,
                archived=record.metadata.archived_at is not None,
                relationships=self.relationships(record.metadata.id),
                excerpt=_excerpt(record.body, tuple(matched_terms)),
                matched_terms=tuple(
                    term for term in query_terms if term in matched_terms
                ),
                unmatched_terms=tuple(
                    term for term in query_terms if term not in matched_terms
                ),
                matched_by=matched_by or ("filters",),
            )
            for _, record, matched_terms, matched_by in matches[:limit]
        )
