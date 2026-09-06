"""Small semantic models for Ariadne's private knowledge records."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints, field_validator

Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=160,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    ),
]
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Summary = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=600),
]
Alias = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class KnowledgeMetadata(BaseModel):
    """The deliberately tiny metadata represented in one Markdown document."""

    model_config = ConfigDict(extra="forbid")

    id: Identifier
    title: Title
    summary: Summary
    aliases: tuple[Alias, ...] = ()
    links: tuple[Identifier, ...] = ()

    @field_validator("title")
    @classmethod
    def title_is_one_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("A knowledge title must fit on one line.")
        return value

    @field_validator("aliases", "links")
    @classmethod
    def unique_values(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        folded = [value.casefold() for value in values]
        if len(folded) != len(set(folded)):
            raise ValueError("Values must be unique within a record.")
        return values


class KnowledgeLinkSummary(BaseModel):
    """Compact context for one untyped direct link or backlink."""

    model_config = ConfigDict(extra="forbid")

    id: Identifier
    title: str
    summary: str


class KnowledgeRecord(BaseModel):
    """A complete semantic record returned to Iris."""

    model_config = ConfigDict(extra="forbid")

    metadata: KnowledgeMetadata
    body: str
    archived: bool = False
    links: tuple[KnowledgeLinkSummary, ...] = ()

    def public_payload(self) -> dict[str, object]:
        """Return the small model-facing record without storage details."""
        metadata = self.metadata
        return {
            "id": metadata.id,
            "title": metadata.title,
            "summary": metadata.summary,
            "aliases": list(metadata.aliases),
            "archived": self.archived,
            "links": [link.model_dump(mode="json") for link in self.links],
            "body": self.body,
        }


class KnowledgeSearchResult(BaseModel):
    """A compact ranked search candidate."""

    model_config = ConfigDict(extra="forbid")

    id: Identifier
    title: str
    summary: str
    aliases: tuple[str, ...]
    archived: bool
    links: tuple[KnowledgeLinkSummary, ...]
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
