"""Transparent ranked search over compact v2 knowledge records."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from difflib import SequenceMatcher
from threading import Lock

from .documents import StoredKnowledge
from .models import (
    KnowledgeLinkSummary,
    KnowledgeSearchError,
    KnowledgeSearchResult,
    KnowledgeValidationError,
)

_WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)


def _words(value: str) -> tuple[str, ...]:
    return tuple(word.casefold() for word in _WORD.findall(value))


def _query_words(value: str) -> tuple[str, ...]:
    words = _words(value)
    substantive = tuple(word for word in words if len(word) > 1)
    return substantive or words


def _search_expression(query: str) -> str:
    return " OR ".join(
        f'"{word.replace(chr(34), chr(34) * 2)}"*' for word in _query_words(query)
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
            or (fuzzy and _close_enough(term, token))
            for token in tokens
        )
    }


class KnowledgeIndex:
    """An in-memory FTS5 index rebuilt from canonical Markdown."""

    def __init__(self, records: Sequence[StoredKnowledge]) -> None:
        self._records = {record.metadata.id: record for record in records}
        self._incoming: dict[str, set[str]] = {}
        for record in records:
            for target in record.metadata.links:
                self._incoming.setdefault(target, set()).add(record.metadata.id)
        self._database_lock = Lock()
        self._database = sqlite3.connect(":memory:", check_same_thread=False)
        try:
            self._database.execute(
                "CREATE VIRTUAL TABLE records USING fts5("
                "id UNINDEXED, title, aliases, folder, summary, body, "
                "tokenize='porter unicode61 remove_diacritics 2')"
            )
        except sqlite3.OperationalError as error:
            raise KnowledgeValidationError(
                "This Python SQLite build does not provide the required FTS5 support."
            ) from error
        self._database.executemany(
            "INSERT INTO records(id, title, aliases, folder, summary, body) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                (
                    record.metadata.id,
                    record.metadata.title,
                    " ".join(record.metadata.aliases),
                    record.folder,
                    record.metadata.summary,
                    record.body,
                )
                for record in records
            ),
        )

    @property
    def records(self) -> dict[str, StoredKnowledge]:
        return self._records

    def links(self, identifier: str) -> tuple[KnowledgeLinkSummary, ...]:
        """Return active direct links and backlinks without invented semantics."""
        record = self._records[identifier]
        identifiers = set(record.metadata.links) | self._incoming.get(identifier, set())
        linked = (
            target
            for target_id in identifiers
            if (target := self._records.get(target_id)) is not None
            and not target.archived
        )
        return tuple(
            KnowledgeLinkSummary(
                id=target.metadata.id,
                title=target.metadata.title,
                summary=target.metadata.summary,
                folder=target.folder,
            )
            for target in sorted(
                linked,
                key=lambda item: (item.metadata.title.casefold(), item.metadata.id),
            )
        )

    def _lexical_evidence(
        self, record: StoredKnowledge, query: str
    ) -> tuple[set[str], tuple[str, ...], float]:
        terms = _query_words(query)
        metadata = record.metadata
        fields = {
            "id": _field_matches(terms, metadata.id, fuzzy=False),
            "title": _field_matches(terms, metadata.title),
            "alias": _field_matches(terms, " ".join(metadata.aliases)),
            "folder": _field_matches(terms, record.folder, fuzzy=False),
            "summary": _field_matches(terms, metadata.summary),
            "body": _field_matches(terms, record.body, fuzzy=False),
        }
        matched = set().union(*fields.values())
        reasons = tuple(name for name, values in fields.items() if values)
        boost = -sum(
            len(fields[name]) * weight
            for name, weight in {
                "id": 1_000.0,
                "title": 1_000.0,
                "alias": 800.0,
                "folder": 150.0,
                "summary": 120.0,
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
        query: str,
        *,
        folder: str | None = None,
        include_archived: bool = False,
        limit: int = 10,
    ) -> tuple[KnowledgeSearchResult, ...]:
        """Return transparent ranked candidates for a concrete text query."""
        if not 1 <= limit <= 50:
            raise KnowledgeValidationError("Knowledge search limit must be 1 to 50.")
        if not query.strip() or not _query_words(query):
            raise KnowledgeValidationError(
                "Knowledge search needs at least one searchable word."
            )

        try:
            with self._database_lock:
                rows = self._database.execute(
                    "SELECT id, bm25(records, 0.0, 10.0, 8.0, 4.0, 6.0, 1.0) "
                    "FROM records WHERE records MATCH ? ORDER BY 2",
                    (_search_expression(query),),
                ).fetchall()
        except sqlite3.Error as error:
            raise KnowledgeSearchError(
                "Private knowledge search is temporarily unavailable. "
                "Retry the search once."
            ) from error

        scores = {identifier: float(score) for identifier, score in rows}
        for identifier, record in self._records.items():
            matched, _, _ = self._lexical_evidence(record, query)
            if matched and identifier not in scores:
                scores[identifier] = 1.0

        matches: list[tuple[float, StoredKnowledge, set[str], tuple[str, ...]]] = []
        query_terms = _query_words(query)
        for identifier, score in scores.items():
            record = self._records[identifier]
            if record.archived and not include_archived:
                continue
            if folder not in (None, "") and not (
                record.folder == folder or record.folder.startswith(f"{folder}/")
            ):
                continue
            matched_terms, matched_by, boost = self._lexical_evidence(record, query)
            matches.append((score + boost, record, matched_terms, matched_by))

        matches.sort(key=lambda item: (item[0], item[1].metadata.title.casefold()))
        return tuple(
            KnowledgeSearchResult(
                id=record.metadata.id,
                title=record.metadata.title,
                summary=record.metadata.summary,
                aliases=record.metadata.aliases,
                folder=record.folder,
                archived=record.archived,
                links=self.links(record.metadata.id),
                excerpt=_excerpt(record.body, tuple(matched_terms)),
                matched_terms=tuple(
                    term for term in query_terms if term in matched_terms
                ),
                unmatched_terms=tuple(
                    term for term in query_terms if term not in matched_terms
                ),
                matched_by=matched_by or ("full_text",),
            )
            for _, record, matched_terms, matched_by in matches[:limit]
        )
