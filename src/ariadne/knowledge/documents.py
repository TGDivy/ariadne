"""Parse and render the compact Markdown representation of knowledge."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError

from .models import KnowledgeMetadata, KnowledgeValidationError

ARCHIVE_DIRECTORY = "archive"


@dataclass(frozen=True, slots=True)
class StoredKnowledge:
    """A validated record plus its private storage location."""

    metadata: KnowledgeMetadata
    body: str
    path: Path
    folder: str = ""
    archived: bool = False


def _front_matter(text: str, path: Path) -> tuple[dict[object, object], str]:
    if not text.startswith("---\n"):
        raise KnowledgeValidationError(f"Managed record {path} has no front matter.")
    boundary = text.find("\n---\n", 4)
    if boundary < 0:
        raise KnowledgeValidationError(
            f"Managed record {path} has unterminated front matter."
        )
    try:
        loaded = yaml.safe_load(text[4:boundary])
    except yaml.YAMLError as error:
        raise KnowledgeValidationError(
            f"Managed record {path} has invalid YAML front matter."
        ) from error
    if not isinstance(loaded, dict):
        raise KnowledgeValidationError(
            f"Managed record {path} front matter must be a mapping."
        )
    return loaded, text[boundary + 5 :].strip()


def _content(value: str, path: Path) -> tuple[str, str, str]:
    if not value.startswith("# "):
        raise KnowledgeValidationError(
            f"Managed record {path} must begin with one level-one title."
        )
    title, separator, remainder = value[2:].partition("\n")
    if not separator or not title.strip():
        raise KnowledgeValidationError(
            f"Managed record {path} must include content after its title."
        )
    remainder = remainder.strip()
    summary_block, separator, body = remainder.partition("\n\n")
    summary = " ".join(summary_block.split())
    if not summary or summary.startswith("#"):
        raise KnowledgeValidationError(
            f"Managed record {path} needs a summary paragraph after its title."
        )
    return title.strip(), summary, body.strip() if separator else ""


def _location(path: Path, root: Path) -> tuple[str, bool]:
    try:
        relative = path.relative_to(root)
    except ValueError as error:
        raise KnowledgeValidationError(
            f"Managed record {path} is outside the knowledge repository."
        ) from error
    archived = bool(relative.parts and relative.parts[0] == ARCHIVE_DIRECTORY)
    folder_parts = relative.parts[1:-1] if archived else relative.parts[:-1]
    return "/".join(folder_parts), archived


def parse_document(path: Path, *, root: Path | None = None) -> StoredKnowledge:
    """Load one v2 record and derive title and summary from its Markdown."""
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise KnowledgeValidationError(f"Cannot read managed record {path}.") from error
    loaded, content = _front_matter(text, path)
    title, summary, body = _content(content, path)
    try:
        metadata = KnowledgeMetadata.model_validate(
            {**loaded, "title": title, "summary": summary}
        )
    except ValidationError as error:
        raise KnowledgeValidationError(
            f"Managed record {path} is invalid: {error}"
        ) from error
    folder, archived = _location(path, (root or path.parent).resolve())
    return StoredKnowledge(metadata, body, path, folder=folder, archived=archived)


def render_document(metadata: KnowledgeMetadata, body: str) -> bytes:
    """Render deterministic v2 Markdown with minimal front matter."""
    values: dict[str, object] = {"id": metadata.id}
    if metadata.aliases:
        values["aliases"] = list(metadata.aliases)
    if metadata.links:
        values["links"] = list(metadata.links)
    front_matter = yaml.safe_dump(
        values,
        allow_unicode=True,
        default_flow_style=False,
        sort_keys=False,
    ).strip()
    sections = [f"# {metadata.title}", metadata.summary]
    if normalized_body := body.strip():
        sections.append(normalized_body)
    rendered_content = "\n\n".join(sections)
    return f"---\n{front_matter}\n---\n\n{rendered_content}\n".encode()


def markdown_paths(root: Path) -> tuple[Path, ...]:
    """Return every non-hidden Markdown record in the Thread repository."""
    paths: list[Path] = []
    for directory, directories, filenames in root.walk():
        directories[:] = sorted(
            name for name in directories if not name.startswith(".")
        )
        paths.extend(
            directory / name
            for name in filenames
            if not name.startswith(".") and name.endswith(".md")
        )
    return tuple(sorted(paths))
