"""Parse and render the canonical Markdown representation of knowledge."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from .models import KnowledgeMetadata, KnowledgeValidationError


@dataclass(frozen=True, slots=True)
class StoredKnowledge:
    """A validated record plus its private storage location."""

    metadata: KnowledgeMetadata
    body: str
    revision: str
    path: Path


def revision_for(content: bytes) -> str:
    """Return an opaque content revision, intentionally unrelated to Git."""
    digest = hashlib.blake2s(content, digest_size=20).hexdigest()
    return f"r1:{digest}"


def parse_document(path: Path) -> StoredKnowledge:
    """Load one managed Markdown file and validate its front matter."""
    try:
        content = path.read_bytes()
        text = content.decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise KnowledgeValidationError(f"Cannot read managed record {path}.") from error
    if not text.startswith("---\n"):
        raise KnowledgeValidationError(f"Managed record {path} has no front matter.")
    boundary = text.find("\n---\n", 4)
    if boundary < 0:
        raise KnowledgeValidationError(
            f"Managed record {path} has unterminated front matter."
        )
    raw_front_matter = text[4:boundary]
    try:
        loaded = yaml.safe_load(raw_front_matter)
    except yaml.YAMLError as error:
        raise KnowledgeValidationError(
            f"Managed record {path} has invalid YAML front matter."
        ) from error
    if not isinstance(loaded, dict):
        raise KnowledgeValidationError(
            f"Managed record {path} front matter must be a mapping."
        )
    try:
        metadata = KnowledgeMetadata.model_validate(loaded)
    except ValidationError as error:
        raise KnowledgeValidationError(
            f"Managed record {path} is invalid: {error}"
        ) from error
    body = text[boundary + 5 :].strip()
    return StoredKnowledge(metadata, body, revision_for(content), path)


def render_document(metadata: KnowledgeMetadata, body: str) -> bytes:
    """Render deterministic, human-readable Markdown."""
    values = metadata.model_dump(mode="json", by_alias=True, exclude_none=True)
    for field in ("aliases", "related", "sources"):
        if not values.get(field):
            values.pop(field, None)
    front_matter = yaml.safe_dump(
        values,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).strip()
    normalized_body = body.strip()
    return f"---\n{front_matter}\n---\n\n{normalized_body}\n".encode()


def markdown_paths(root: Path) -> tuple[Path, ...]:
    """Return every canonical Markdown candidate outside Git internals."""
    return tuple(
        path
        for path in sorted(root.rglob("*.md"))
        if ".git" not in path.relative_to(root).parts
    )
