"""Deterministic full-text and relationship search over knowledge records."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import date

from .documents import StoredKnowledge
from .models import KnowledgeSearchResult, KnowledgeValidationError

_WORD = re.compile(r"[^\W_]+(?:[-'][^\W_]+)*", re.UNICODE)


def _search_expression(query: str) -> str:
    words = _WORD.findall(query)
    return " AND ".join(f'"{word.replace(chr(34), chr(34) * 2)}"' for word in words)


def _date_part(value: str | None) -> str | None:
    return value[:10] if value is not None else None


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
    starts = _date_part(record.metadata.starts_at)
    ends = _date_part(record.metadata.ends_at) or starts
    if (date_from is not None or date_through is not None) and starts is None:
        return False
    if date_from is not None and ends is not None and ends < date_from:
        return False
    return not (
        date_through is not None and starts is not None and starts > date_through
    )


def _excerpt(body: str, query: str, *, length: int = 240) -> str:
    compact = " ".join(body.split())
    if not compact:
        return ""
    folded = compact.casefold()
    positions = [
        folded.find(word.casefold()) for word in _WORD.findall(query) if word.strip()
    ]
    found = [position for position in positions if position >= 0]
    centre = min(found) if found else 0
    start = max(0, centre - length // 3)
    end = min(len(compact), start + length)
    prefix = "…" if start else ""
    suffix = "…" if end < len(compact) else ""
    return prefix + compact[start:end].strip() + suffix


def _match_reasons(record: StoredKnowledge, query: str) -> tuple[str, ...]:
    if not query.strip():
        return ("filters",)
    folded = query.strip().casefold()
    metadata = record.metadata
    reasons: list[str] = []
    if folded == metadata.id.casefold():
        reasons.append("exact_id")
    if folded == metadata.title.casefold():
        reasons.append("exact_title")
    if folded in {alias.casefold() for alias in metadata.aliases}:
        reasons.append("exact_alias")
    if folded in metadata.title.casefold() and "exact_title" not in reasons:
        reasons.append("title")
    if any(folded in alias.casefold() for alias in metadata.aliases) and (
        "exact_alias" not in reasons
    ):
        reasons.append("alias")
    if folded in record.body.casefold():
        reasons.append("body")
    if not reasons:
        reasons.append("full_text")
    return tuple(reasons)


class KnowledgeIndex:
    """An in-memory index rebuilt entirely from canonical Markdown."""

    def __init__(self, records: Sequence[StoredKnowledge]) -> None:
        self._records = {record.metadata.id: record for record in records}
        self._incoming: dict[str, set[str]] = {}
        for record in records:
            for relation in record.metadata.related:
                self._incoming.setdefault(relation.record, set()).add(
                    record.metadata.id
                )
        self._database = sqlite3.connect(":memory:")
        try:
            self._database.execute(
                "CREATE VIRTUAL TABLE records USING fts5("
                "id UNINDEXED, title, aliases, metadata, body, tokenize='unicode61')"
            )
        except sqlite3.OperationalError as error:
            raise KnowledgeValidationError(
                "This Python SQLite build does not provide the required FTS5 support."
            ) from error
        self._database.executemany(
            "INSERT INTO records(id, title, aliases, metadata, body) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                (
                    record.metadata.id,
                    record.metadata.title,
                    " ".join(record.metadata.aliases),
                    " ".join(
                        value
                        for value in (
                            record.metadata.kind,
                            record.metadata.state,
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

    def incoming_ids(self, identifier: str) -> tuple[str, ...]:
        return tuple(sorted(self._incoming.get(identifier, ())))

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
        """Return ranked records satisfying explicit semantic filters."""
        if not 1 <= limit <= 50:
            raise KnowledgeValidationError("Knowledge search limit must be 1 to 50.")
        _validate_date_bounds(date_from, date_through)
        kind_filter = {value.casefold() for value in kinds}
        state_filter = {value.casefold() for value in states}
        if not query.strip() and not any(
            (kind_filter, state_filter, date_from, date_through, related_to)
        ):
            raise KnowledgeValidationError(
                "Knowledge search needs query text or at least one filter."
            )

        scores: dict[str, float] = {}
        expression = _search_expression(query)
        if expression:
            rows = self._database.execute(
                "SELECT id, bm25(records, 0.0, 10.0, 8.0, 2.0, 1.0) "
                "FROM records WHERE records MATCH ? ORDER BY 2 LIMIT 200",
                (expression,),
            )
            scores.update((identifier, float(score)) for identifier, score in rows)
            folded = query.strip().casefold()
            for record in self._records.values():
                metadata = record.metadata
                if folded == metadata.id.casefold():
                    scores[metadata.id] = -1_000_000.0
                elif folded == metadata.title.casefold():
                    scores[metadata.id] = -900_000.0
                elif folded in {alias.casefold() for alias in metadata.aliases}:
                    scores[metadata.id] = -800_000.0
        else:
            scores.update((identifier, 0.0) for identifier in self._records)

        related_ids: set[str] | None = None
        if related_to is not None:
            if related_to not in self._records:
                raise KnowledgeValidationError(
                    f"Related knowledge record {related_to!r} does not exist."
                )
            related_ids = set(self.incoming_ids(related_to))
            related_ids.update(
                relation.record
                for relation in self._records[related_to].metadata.related
            )

        matches: list[tuple[float, StoredKnowledge]] = []
        for identifier, score in scores.items():
            record = self._records[identifier]
            metadata = record.metadata
            if not include_archived and metadata.state == "archived":
                continue
            if kind_filter and metadata.kind.casefold() not in kind_filter:
                continue
            if state_filter and (metadata.state or "").casefold() not in state_filter:
                continue
            if not _overlaps(record, date_from, date_through):
                continue
            if related_ids is not None and identifier not in related_ids:
                continue
            matches.append((score, record))

        matches.sort(key=lambda item: (item[0], item[1].metadata.title.casefold()))
        return tuple(
            KnowledgeSearchResult(
                id=record.metadata.id,
                title=record.metadata.title,
                kind=record.metadata.kind,
                state=record.metadata.state,
                starts_at=record.metadata.starts_at,
                ends_at=record.metadata.ends_at,
                related=tuple(relation.record for relation in record.metadata.related),
                revision=record.revision,
                excerpt=_excerpt(record.body, query),
                matched_by=_match_reasons(record, query),
            )
            for _, record in matches[:limit]
        )
