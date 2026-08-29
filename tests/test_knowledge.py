import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastmcp import Client

from ariadne.knowledge import (
    KnowledgeConflict,
    KnowledgeMetadata,
    KnowledgeRelation,
    KnowledgeSource,
    KnowledgeSyncError,
    KnowledgeValidationError,
)
from ariadne.knowledge.documents import (
    StoredKnowledge,
    parse_document,
    render_document,
    revision_for,
)
from ariadne.knowledge.migration import inspect_migration
from ariadne.knowledge.search import KnowledgeIndex
from ariadne.knowledge.store import KnowledgeStore
from ariadne.mcp import knowledge as knowledge_tools
from ariadne.mcp.server import create_server

NOW = datetime(2026, 8, 29, 10, tzinfo=UTC)


def git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def metadata(
    identifier: str,
    title: str,
    kind: str,
    *,
    aliases: tuple[str, ...] = (),
    state: str | None = None,
    starts_at: str | None = None,
    related: tuple[KnowledgeRelation, ...] = (),
) -> KnowledgeMetadata:
    return KnowledgeMetadata(
        id=identifier,
        title=title,
        kind=kind,
        aliases=aliases,
        state=state,
        starts_at=starts_at,
        related=related,
        created_at=NOW,
        updated_at=NOW,
    )


def write_record(
    root: Path, relative: str, record: KnowledgeMetadata, body: str
) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_document(record, body))


@pytest.fixture
def knowledge_repository(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    origin = tmp_path / "origin.git"
    root.mkdir()
    git(tmp_path, "init", "--bare", str(origin))
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Knowledge Test")
    git(root, "config", "user.email", "knowledge@example.test")
    git(root, "remote", "add", "origin", str(origin))
    write_record(
        root,
        "Goals/Running.md",
        metadata("goal:running", "Running", "goal", aliases=("half marathon",)),
        "Build consistency and complete a half marathon comfortably.",
    )
    write_record(
        root,
        "Plans/Windsor.md",
        metadata(
            "plan:windsor-2026",
            "Windsor Trail Run",
            "plan",
            state="confirmed",
            starts_at="2026-08-30T09:20:00+01:00",
            related=(KnowledgeRelation(record="goal:running", relation="supports"),),
        ),
        "Collect the bib before the race. Transport is not arranged yet.",
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial knowledge")
    git(root, "push", "-u", "origin", "main")
    return root


def test_markdown_round_trip_has_stable_semantic_metadata(tmp_path: Path) -> None:
    path = tmp_path / "record.md"
    expected = metadata(
        "person:lily",
        "Lily Example",
        "person",
        aliases=("Lil",),
        related=(KnowledgeRelation(record="person:divy", relation="friend_of"),),
    )

    content = render_document(expected, "# Lily\n\nMet at university.")
    path.write_bytes(content)
    loaded = parse_document(path)

    assert loaded.metadata == expected
    assert loaded.body == "# Lily\n\nMet at university."
    assert loaded.revision == revision_for(content)
    assert str(tmp_path) not in loaded.revision


def test_search_ranks_names_and_supports_filters_and_relationships(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    by_title = store.search("Windsor Trail Run")
    by_alias = store.search("half marathon")
    related = store.search(kinds=("plan",), related_to="goal:running")
    dated = store.search(date_from="2026-08-30", date_through="2026-08-30")

    assert by_title[0].id == "plan:windsor-2026"
    assert "exact_title" in by_title[0].matched_by
    assert by_alias[0].id == "goal:running"
    assert "exact_alias" in by_alias[0].matched_by
    assert [result.id for result in related] == ["plan:windsor-2026"]
    assert [result.id for result in dated] == ["plan:windsor-2026"]


def test_reads_summarize_incoming_and_outgoing_relationships(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    goal, plan = store.read(("goal:running", "plan:windsor-2026"))

    assert goal.incoming == (
        KnowledgeRelation(record="plan:windsor-2026", relation="supports"),
    )
    assert plan.metadata.related == (
        KnowledgeRelation(record="goal:running", relation="supports"),
    )
    assert "path" not in json.dumps(goal.public_payload())


def test_create_update_and_archive_are_synchronized_and_revision_checked(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)
    created = store.create(
        title="Race breakfast",
        kind="scratch",
        body="Try oats, banana, and a bagel.",
        related=(KnowledgeRelation(record="plan:windsor-2026", relation="supports"),),
    )

    updated = store.update(
        created.metadata.id,
        created.revision,
        kind="preference",
        body="M&S is the practical choice for race breakfast.",
        aliases=("race food",),
    )

    with pytest.raises(KnowledgeConflict, match="changed since it was read"):
        store.update(created.metadata.id, created.revision, state="settled")

    archived = store.archive(
        updated.metadata.id, updated.revision, "The race is complete."
    )

    assert archived.metadata.state == "archived"
    assert "The race is complete." in archived.body
    assert store.search("race food") == ()
    assert store.search("race food", include_archived=True)[0].id == created.metadata.id
    assert git(knowledge_repository, "status", "--porcelain") == ""
    assert len(git(knowledge_repository, "log", "--format=%s").splitlines()) == 4
    assert git(knowledge_repository, "rev-list", "@{upstream}..HEAD") == ""

    with pytest.raises(KnowledgeConflict, match="already archived"):
        store.archive(
            archived.metadata.id, archived.revision, "Do not archive this twice."
        )


def test_create_avoids_ids_that_are_already_an_alias(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)
    running = store.read(("goal:running",))[0]
    store.update(
        "goal:running",
        running.revision,
        aliases=("half marathon", "scratch:race-breakfast"),
    )

    created = store.create(
        title="Race breakfast",
        kind="scratch",
        body="An incomplete food thought.",
    )

    assert created.metadata.id == "scratch:race-breakfast-2"


def test_update_rejects_contradictory_patch_instead_of_guessing(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)
    plan = store.read(("plan:windsor-2026",))[0]

    with pytest.raises(KnowledgeValidationError, match="updated and cleared"):
        store.update(
            plan.metadata.id,
            plan.revision,
            state="complete",
            clear=("state",),
        )


def test_unmanaged_changes_block_mutation_without_overwriting_them(
    knowledge_repository: Path,
) -> None:
    unmanaged = knowledge_repository / "notes.txt"
    unmanaged.write_text("mine", encoding="utf-8")
    store = KnowledgeStore(knowledge_repository)

    with pytest.raises(KnowledgeSyncError, match="unmanaged local changes"):
        store.create(title="Blocked", kind="scratch", body="Do not write this.")

    assert unmanaged.read_text(encoding="utf-8") == "mine"
    assert not (knowledge_repository / "Knowledge/scratch/blocked.md").exists()


def test_a_local_commit_is_the_retry_state_after_push_failure(
    knowledge_repository: Path,
) -> None:
    origin = Path(git(knowledge_repository, "remote", "get-url", "origin"))
    hook = origin / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    store = KnowledgeStore(knowledge_repository)

    with pytest.raises(KnowledgeSyncError, match="synchronized safely"):
        store.create(
            title="Durable retry",
            kind="scratch",
            body="The local commit should survive a rejected push.",
        )

    assert git(knowledge_repository, "status", "--porcelain") == ""
    assert git(knowledge_repository, "rev-list", "--count", "@{upstream}..HEAD") == "1"

    hook.unlink()
    store.create(
        title="Next thought",
        kind="scratch",
        body="This operation first synchronizes the durable retry.",
    )

    assert store.search("Durable retry")[0].id == "scratch:durable-retry"
    assert git(knowledge_repository, "rev-list", "@{upstream}..HEAD") == ""


def test_a_long_lived_reader_rebuilds_after_another_writer_changes_head(
    knowledge_repository: Path,
) -> None:
    reader = KnowledgeStore(knowledge_repository)
    writer = KnowledgeStore(knowledge_repository)
    assert reader.search("new thought") == ()

    writer.create(title="New thought", kind="scratch", body="A useful experiment.")

    assert reader.search("new thought")[0].title == "New thought"


def test_migration_preview_inventories_legacy_records_without_writing(
    tmp_path: Path,
) -> None:
    root = tmp_path / "thread"
    (root / "Plans").mkdir(parents=True)
    legacy = root / "Plans/Windsor.md"
    legacy.write_text(
        "# Windsor Trail Run\n\nSee [missing](../People/Missing.md).\n",
        encoding="utf-8",
    )
    old_front_matter = root / "Plans/Breakfast.md"
    old_front_matter.write_text(
        "---\nstatus: considering\n---\n\n# Race breakfast\n",
        encoding="utf-8",
    )
    before = legacy.read_bytes()

    report = inspect_migration(root)

    assert report.payload()["summary"] == {
        "total": 2,
        "managed": 0,
        "proposed": 2,
        "invalid": 0,
    }
    candidates = {candidate.path: candidate for candidate in report.candidates}
    assert candidates["Plans/Windsor.md"].proposed_id == "plan:windsor-trail-run"
    assert candidates["Plans/Breakfast.md"].proposed_id == "plan:race-breakfast"
    assert candidates["Plans/Breakfast.md"].problem is not None
    assert report.broken_links == ("Plans/Windsor.md -> ../People/Missing.md",)
    assert legacy.read_bytes() == before


def test_thousand_record_index_remains_small_and_retrievable(tmp_path: Path) -> None:
    records = []
    for number in range(1_000):
        body = f"Generated context number {number} about training and recovery."
        content = render_document(
            metadata(f"note:generated-{number}", f"Generated {number}", "note"),
            body,
        )
        records.append(
            StoredKnowledge(
                metadata(f"note:generated-{number}", f"Generated {number}", "note"),
                body,
                revision_for(content),
                tmp_path / f"{number}.md",
            )
        )

    started = time.monotonic()
    index = KnowledgeIndex(records)
    results = index.search("Generated 847")
    elapsed = time.monotonic() - started

    assert results[0].id == "note:generated-847"
    assert elapsed < 2


async def test_mcp_tools_hide_storage_and_attach_runtime_provenance(
    knowledge_repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    context = tmp_path / "context.json"
    context.write_text(
        json.dumps(
            {
                "sources": [
                    {
                        "source": "mail:opaque-test-event",
                        "observed_at": "2026-08-29T10:00:00Z",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(knowledge_tools.ROOT_ENVIRONMENT, str(knowledge_repository))
    monkeypatch.setenv(knowledge_tools.CONTEXT_ENVIRONMENT, str(context))
    knowledge_tools._STORES.clear()

    async with Client(create_server()) as client:
        created = await client.call_tool(
            "create_knowledge",
            {
                "title": "Windsor weather",
                "kind": "scratch",
                "body": "Check the forecast before packing.",
                "related": [{"record": "plan:windsor-2026", "relation": "supports"}],
            },
        )

    payload = created.data["record"]
    assert payload["sources"] == [
        {
            "source": "mail:opaque-test-event",
            "observed_at": "2026-08-29T10:00:00Z",
        }
    ]
    rendered = json.dumps(payload)
    assert str(knowledge_repository) not in rendered
    assert "commit" not in rendered.casefold()


async def test_knowledge_tool_annotations_distinguish_reads_from_private_writes() -> (
    None
):
    tools = {tool.name: tool for tool in await create_server().list_tools()}

    for name in ("search_knowledge", "read_knowledge"):
        assert tools[name].annotations is not None
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.destructiveHint is False
    for name in ("create_knowledge", "update_knowledge", "archive_knowledge"):
        assert tools[name].annotations is not None
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is False


def test_source_model_accepts_explicit_semantic_provenance() -> None:
    source = KnowledgeSource(source="web:https://example.test/race")
    assert source.source.startswith("web:")
