"""Semantic models for Ariadne's private knowledge records."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=3,
        max_length=160,
        pattern=r"^[a-z0-9][a-z0-9._-]*(?::[a-z0-9][a-z0-9._-]*)+$",
    ),
]
Kind = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=48,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]
Tag = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    ),
]
Label = Tag
Collection = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=240,
        pattern=(r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)*$"),
    ),
]

_DATE_OR_DATETIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)?$"
)


class KnowledgeRelation(BaseModel):
    """One directed, labelled connection to another record."""

    model_config = ConfigDict(extra="forbid")

    record: Identifier
    relation: Label


class KnowledgeMetadata(BaseModel):
    """Canonical front matter with semantic and Ariadne-owned fields."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_version: Literal[1] = Field(default=1, alias="schema")
    id: Identifier
    title: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    summary: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=600),
    ]
    kind: Kind
    collection: Collection
    tags: tuple[Tag, ...] = ()
    aliases: tuple[
        Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)], ...
    ] = ()
    starts_at: str | None = None
    ends_at: str | None = None
    related: tuple[KnowledgeRelation, ...] = ()
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None = None

    @field_validator("tags", "aliases")
    @classmethod
    def unique_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        folded = [value.casefold() for value in values]
        if len(folded) != len(set(folded)):
            raise ValueError("Values must be unique within a record.")
        return values

    @field_validator("starts_at", "ends_at")
    @classmethod
    def validate_date_or_datetime(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not _DATE_OR_DATETIME.fullmatch(value):
            raise ValueError("Use an ISO 8601 date or timezone-aware date-time.")
        try:
            if "T" in value:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError
            else:
                date.fromisoformat(value)
        except ValueError as error:
            raise ValueError(
                "Use a valid ISO 8601 date or timezone-aware date-time."
            ) from error
        return value

    @field_validator("created_at", "updated_at", "archived_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("Internal timestamps must include a timezone.")
        return value

    @model_validator(mode="after")
    def require_ordered_timestamps(self) -> KnowledgeMetadata:
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot be before created_at.")
        if self.archived_at is not None and self.archived_at < self.created_at:
            raise ValueError("archived_at cannot be before created_at.")
        return self


class KnowledgeRelationshipSummary(BaseModel):
    """Compact context about a directly related record."""

    model_config = ConfigDict(extra="forbid")

    id: Identifier
    title: str
    summary: str
    kind: Kind
    relation: Label
    direction: Literal["incoming", "outgoing"]


class KnowledgeRecord(BaseModel):
    """A complete semantic record returned to Iris."""

    model_config = ConfigDict(extra="forbid")

    metadata: KnowledgeMetadata
    body: str
    relationships: tuple[KnowledgeRelationshipSummary, ...] = ()

    def public_payload(self) -> dict[str, object]:
        """Hide Ariadne-owned storage metadata from the model-facing result."""
        metadata = self.metadata
        return {
            "id": metadata.id,
            "title": metadata.title,
            "summary": metadata.summary,
            "kind": metadata.kind,
            "collection": metadata.collection,
            "tags": list(metadata.tags),
            "aliases": list(metadata.aliases),
            "starts_at": metadata.starts_at,
            "ends_at": metadata.ends_at,
            "archived": metadata.archived_at is not None,
            "relationships": [
                relationship.model_dump(mode="json")
                for relationship in self.relationships
            ],
            "body": self.body,
        }


class KnowledgeSearchResult(BaseModel):
    """A compact ranked search candidate."""

    model_config = ConfigDict(extra="forbid")

    id: Identifier
    title: str
    summary: str
    kind: Kind
    collection: Collection
    tags: tuple[Tag, ...]
    starts_at: str | None
    ends_at: str | None
    archived: bool
    relationships: tuple[KnowledgeRelationshipSummary, ...]
    excerpt: str
    matched_terms: tuple[str, ...]
    unmatched_terms: tuple[str, ...]
    matched_by: tuple[str, ...]


class KnowledgeError(RuntimeError):
    """Base class for stable knowledge capability failures."""


class KnowledgeConflict(KnowledgeError):
    """A requested operation conflicts with existing knowledge."""


class KnowledgeValidationError(KnowledgeError):
    """Stored or proposed knowledge does not satisfy the record contract."""


class KnowledgeSearchError(KnowledgeError):
    """The derived knowledge search index could not serve a query."""


class KnowledgeSyncError(KnowledgeError):
    """The private knowledge repository cannot be synchronized safely."""
