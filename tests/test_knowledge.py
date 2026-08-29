import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastmcp import Client

from ariadne.codex.resolver import resolve_profile
from ariadne.knowledge import (
    KnowledgeConflict,
    KnowledgeMetadata,
    KnowledgeRelation,
    KnowledgeSyncError,
    KnowledgeValidationError,
)
from ariadne.knowledge.capability import ROOT_ENVIRONMENT
from ariadne.knowledge.documents import StoredKnowledge, parse_document, render_document
from ariadne.knowledge.search import KnowledgeIndex
from ariadne.knowledge.store import KnowledgeStore
from ariadne.knowledge.validation import validate_repository
from ariadne.mcp import knowledge as knowledge_tools
from ariadne.mcp.server import create_server
from ariadne.profile import MAIL_PROFILE
from ariadne.prompts.orientation import render_knowledge_orientation
from ariadne.scripts.knowledge import main as validate_knowledge_main

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
    collection: str,
    *,
    summary: str | None = None,
    tags: tuple[str, ...] = (),
    aliases: tuple[str, ...] = (),
    starts_at: str | None = None,
    related: tuple[KnowledgeRelation, ...] = (),
) -> KnowledgeMetadata:
    return KnowledgeMetadata(
        id=identifier,
        title=title,
        summary=summary or f"Useful context about {title}.",
        kind=kind,
        collection=collection,
        tags=tags,
        aliases=aliases,
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
        "goal/health/running.md",
        metadata(
            "goal:running",
            "Running",
            "goal",
            "health",
            summary="Build consistency and complete a half marathon comfortably.",
            tags=("health", "running"),
            aliases=("half marathon",),
        ),
        "Build consistency and complete a half marathon comfortably.",
    )
    write_record(
        root,
        "plan/running/windsor-trail-run.md",
        metadata(
            "plan:windsor-2026",
            "Windsor Trail Run",
            "plan",
            "running",
            summary="Confirmed Windsor half marathon; transport is not arranged.",
            tags=("running", "travel"),
            starts_at="2026-08-30T09:20:00+01:00",
            related=(KnowledgeRelation(record="goal:running", relation="supports"),),
        ),
        "Collect the bib before the race. Transport is not arranged yet.",
    )
    write_record(
        root,
        "booking/travel/trainline-booking.md",
        metadata(
            "booking:trainline-windsor",
            "Trainline booking",
            "booking",
            "travel",
            summary="Train tickets from Waterloo to Windsor for race day.",
            tags=("travel",),
            related=(
                KnowledgeRelation(record="plan:windsor-2026", relation="supports"),
            ),
        ),
        "Outbound train reaches Windsor at 08:44. The return is flexible.",
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial knowledge")
    git(root, "push", "-u", "origin", "main")
    return root


def test_markdown_round_trip_keeps_internal_metadata_out_of_public_result(
    tmp_path: Path,
) -> None:
    path = tmp_path / "lily.md"
    expected = metadata(
        "person:lily",
        "Lily Example",
        "person",
        "friends",
        summary="A friend from university.",
        tags=("friend",),
        aliases=("Lil",),
    )
    path.write_bytes(render_document(expected, "Met at university."))

    loaded = parse_document(path)
    public = json.dumps(
        {
            "id": loaded.metadata.id,
            "title": loaded.metadata.title,
            "summary": loaded.metadata.summary,
        }
    )

    assert loaded.metadata == expected
    assert loaded.body == "Met at university."
    assert "created_at" not in public
    assert "revision" not in public
    assert "sources" not in public


def test_search_is_or_ranked_prefix_stemmed_and_typo_tolerant(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    broad = store.search("Windsor train")
    typo = store.search("Windosr")
    prefix = store.search("train")
    plural = store.search("trains")

    assert {result.id for result in broad[:2]} == {
        "plan:windsor-2026",
        "booking:trainline-windsor",
    }
    windsor = next(result for result in broad if result.id == "plan:windsor-2026")
    assert windsor.matched_terms == ("windsor",)
    assert windsor.unmatched_terms == ("train",)
    assert typo[0].id == "plan:windsor-2026"
    assert prefix[0].id == "booking:trainline-windsor"
    assert plural[0].id == "booking:trainline-windsor"


def test_search_filters_dates_tags_collections_and_relationships(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    tagged = store.search(tags=("running", "travel"))
    collected = store.search(collections=("running",))
    dated = store.search(date_from="2026-08-30", date_through="2026-08-30")
    related = store.search(kinds=("plan",), related_to="goal:running")

    assert [result.id for result in tagged] == ["plan:windsor-2026"]
    assert [result.id for result in collected] == ["plan:windsor-2026"]
    assert [result.id for result in dated] == ["plan:windsor-2026"]
    assert [result.id for result in related] == ["plan:windsor-2026"]


def test_search_and_read_return_compact_relationship_context(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    result = store.search("Windsor")[0]
    goal, plan = store.read(("goal:running", "plan:windsor-2026"))

    assert result.relationships[0].id == "booking:trainline-windsor"
    assert result.relationships[0].summary.startswith("Train tickets")
    assert {relationship.direction for relationship in result.relationships} == {
        "incoming",
        "outgoing",
    }
    assert goal.relationships[0].id == "plan:windsor-2026"
    assert goal.relationships[0].direction == "incoming"
    assert plan.relationships[0].summary
    assert "body" not in result.relationships[0].model_dump()


def test_browse_and_orientation_expose_a_two_level_human_map(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    tree = store.browse(depth=2, include_summaries=False)
    running = store.browse("plan/running", depth=1)
    orientation = store.orientation()

    top_level = {child["name"] for child in tree["collections"]}
    assert top_level == {"booking", "goal", "plan"}
    assert running["records"][0]["id"] == "plan:windsor-2026"
    assert running["records"][0]["summary"].startswith("Confirmed Windsor")
    assert orientation["kinds"] == {"booking": 1, "goal": 1, "plan": 1}
    assert orientation["tags"] == {"health": 1, "running": 2, "travel": 2}
    assert orientation["relationships"] == {"supports": 2}
    assert orientation["collections"] == [
        "booking/travel",
        "goal/health",
        "plan/running",
    ]

    rendered = render_knowledge_orientation(**orientation)
    assert "booking/\n  travel/" in rendered
    assert "Kinds: booking (1), goal (1), plan (1)" in rendered
    assert "Relationships: supports (2)" in rendered


def test_profile_resolution_applies_live_knowledge_orientation_and_root(
    knowledge_repository: Path,
) -> None:
    enriched = resolve_profile(
        MAIL_PROFILE,
        vault=knowledge_repository,
        human="Divy",
        knowledge_root=knowledge_repository,
    )

    assert enriched.developer_instruction_sources[-1] == (
        "generated/knowledge-orientation"
    )
    assert "booking/\n  travel/" in enriched.developer_instructions
    assert "Kinds: booking (1), goal (1), plan (1)" in (enriched.developer_instructions)
    assert dict(enriched.mcp_environment_values)[ROOT_ENVIRONMENT] == str(
        knowledge_repository
    )
    assert str(knowledge_repository) not in enriched.developer_instructions


def test_whole_repository_validation_checks_records_and_relationships(
    knowledge_repository: Path,
) -> None:
    report = validate_repository(knowledge_repository)

    assert report.records == 3
    assert report.relationships == 2
    assert report.archived == 0


def test_whole_repository_validation_rejects_a_broken_relationship(
    knowledge_repository: Path,
) -> None:
    path = knowledge_repository / "goal/health/running.md"
    record = parse_document(path)
    broken = record.metadata.model_copy(
        update={
            "related": (
                KnowledgeRelation(record="person:missing", relation="involves"),
            )
        }
    )
    path.write_bytes(render_document(broken, record.body))

    with pytest.raises(KnowledgeValidationError, match="missing record"):
        validate_repository(knowledge_repository)


def test_whole_repository_validation_rejects_a_stale_title_filename(
    knowledge_repository: Path,
) -> None:
    original = knowledge_repository / "plan/running/windsor-trail-run.md"
    original.rename(original.with_name("old-title.md"))

    with pytest.raises(KnowledgeValidationError, match="does not match title"):
        validate_repository(knowledge_repository)


def test_validation_command_reports_a_compact_summary(
    knowledge_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["ariadne-knowledge", str(knowledge_repository)])

    validate_knowledge_main()

    assert capsys.readouterr().out == (
        "Knowledge is valid: 3 records, 2 relationships, 0 archived.\n"
    )


def test_create_generates_id_path_and_timestamps_then_pushes(
    knowledge_repository: Path,
) -> None:
    before = datetime.now(UTC)
    store = KnowledgeStore(knowledge_repository)

    created = store.create(
        title="Race Breakfast & Snacks",
        summary="Possible food for race morning and recovery.",
        kind="scratch",
        collection="race-preparation/food",
        tags=("food", "running"),
        body="Try oats, banana, and a bagel.",
        related=(KnowledgeRelation(record="plan:windsor-2026", relation="supports"),),
    )

    path = (
        knowledge_repository / "scratch/race-preparation/food/race-breakfast-snacks.md"
    )
    stored = parse_document(path)
    assert created.metadata.id == "scratch:race-breakfast-snacks"
    assert path.is_file()
    assert stored.metadata.created_at >= before
    assert stored.metadata.updated_at == stored.metadata.created_at
    assert "created_at" not in created.public_payload()
    assert git(knowledge_repository, "status", "--porcelain") == ""
    assert git(knowledge_repository, "rev-list", "@{upstream}..HEAD") == ""


def test_update_uses_latest_record_updates_time_and_moves_generated_path(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)
    original = store.read(("plan:windsor-2026",))[0]

    updated = store.update(
        "plan:windsor-2026",
        title="Windsor Race Day",
        summary="Race and train arrangements are confirmed.",
        kind="event",
        collection="2026/running",
        tags=("running", "travel", "confirmed"),
        body="Arrive at 08:44 and go directly to bib collection.",
    )

    destination = knowledge_repository / "event/2026/running/windsor-race-day.md"
    assert not (knowledge_repository / "plan/running/windsor-trail-run.md").exists()
    assert destination.is_file()
    assert updated.metadata.id == "plan:windsor-2026"
    assert updated.metadata.updated_at > original.metadata.updated_at
    assert git(knowledge_repository, "status", "--porcelain") == ""


def test_title_and_collection_moves_preserve_id_and_relationships(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    renamed = store.update("plan:windsor-2026", title="Windsor Race Day")
    recollected = store.update("plan:windsor-2026", collection="2026/running")
    booking = store.read(("booking:trainline-windsor",))[0]

    assert renamed.metadata.id == "plan:windsor-2026"
    assert recollected.metadata.id == "plan:windsor-2026"
    assert not (knowledge_repository / "plan/running/windsor-trail-run.md").exists()
    assert (knowledge_repository / "plan/2026/running/windsor-race-day.md").is_file()
    assert any(
        relationship.id == "plan:windsor-2026" for relationship in booking.relationships
    )


def test_archive_is_timestamped_and_hidden_without_deleting(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    archived = store.archive("booking:trainline-windsor", "The journey is complete.")

    assert archived.metadata.archived_at is not None
    assert archived.public_payload()["archived"] is True
    assert store.search("Trainline") == ()
    assert (
        store.search("Trainline", include_archived=True)[0].id
        == "booking:trainline-windsor"
    )
    with pytest.raises(KnowledgeConflict, match="already archived"):
        store.archive("booking:trainline-windsor", "Do not archive twice.")


def test_create_avoids_ids_that_are_already_an_alias(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)
    store.update(
        "goal:running",
        aliases=("half marathon", "scratch:race-breakfast"),
    )

    created = store.create(
        title="Race breakfast",
        summary="An incomplete food thought.",
        kind="scratch",
        collection="running/food",
        body="An incomplete food thought.",
    )

    assert created.metadata.id == "scratch:race-breakfast-2"


def test_update_rejects_contradictory_patch_instead_of_guessing(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    with pytest.raises(KnowledgeValidationError, match="updated and cleared"):
        store.update("plan:windsor-2026", tags=("complete",), clear=("tags",))


def test_unmanaged_changes_block_mutation_without_overwriting_them(
    knowledge_repository: Path,
) -> None:
    unmanaged = knowledge_repository / "notes.txt"
    unmanaged.write_text("mine", encoding="utf-8")
    store = KnowledgeStore(knowledge_repository)

    with pytest.raises(KnowledgeSyncError, match="unmanaged local changes"):
        store.create(
            title="Blocked",
            summary="This should not be written.",
            kind="scratch",
            collection="tests",
            body="Do not write this.",
        )

    assert unmanaged.read_text(encoding="utf-8") == "mine"
    assert not (knowledge_repository / "scratch/tests/blocked.md").exists()


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
            summary="A record whose first push is rejected.",
            kind="scratch",
            collection="tests",
            body="The local commit should survive.",
        )

    assert git(knowledge_repository, "status", "--porcelain") == ""
    assert git(knowledge_repository, "rev-list", "--count", "@{upstream}..HEAD") == "1"

    hook.unlink()
    store.create(
        title="Next thought",
        summary="The operation first synchronizes the prior commit.",
        kind="scratch",
        collection="tests",
        body="Then it writes this record.",
    )

    assert store.search("Durable retry")[0].id == "scratch:durable-retry"
    assert git(knowledge_repository, "rev-list", "@{upstream}..HEAD") == ""


def test_a_mutation_automatically_pulls_remote_knowledge_before_writing(
    knowledge_repository: Path,
) -> None:
    origin = Path(git(knowledge_repository, "remote", "get-url", "origin"))
    other = knowledge_repository.parent / "other"
    git(
        knowledge_repository.parent,
        "clone",
        "--branch",
        "main",
        str(origin),
        str(other),
    )
    git(other, "config", "user.name", "Other Knowledge Process")
    git(other, "config", "user.email", "other@example.test")
    write_record(
        other,
        "note/travel/remote-change.md",
        metadata(
            "note:remote-change",
            "Remote change",
            "note",
            "travel",
            summary="Knowledge written by another synchronized process.",
        ),
        "This must be pulled before the next local write.",
    )
    git(other, "add", ".")
    git(other, "commit", "-m", "Add remote knowledge")
    git(other, "push")

    store = KnowledgeStore(knowledge_repository)
    store.create(
        title="Local change",
        summary="Knowledge written after synchronizing the remote change.",
        kind="note",
        collection="tests",
        body="This is created only after the automatic pull.",
    )

    assert store.search("Remote change")[0].id == "note:remote-change"
    assert git(knowledge_repository, "rev-list", "@{upstream}..HEAD") == ""


def test_a_long_lived_reader_rebuilds_after_another_writer_changes_head(
    knowledge_repository: Path,
) -> None:
    reader = KnowledgeStore(knowledge_repository)
    writer = KnowledgeStore(knowledge_repository)
    assert reader.search("new thought") == ()

    writer.create(
        title="New thought",
        summary="A useful experiment.",
        kind="scratch",
        collection="experiments",
        body="A useful experiment.",
    )

    assert reader.search("new thought")[0].title == "New thought"


def test_thousand_record_index_remains_small_and_retrievable(tmp_path: Path) -> None:
    records = []
    for number in range(1_000):
        body = f"Generated context number {number} about training and recovery."
        record_metadata = metadata(
            f"note:generated-{number}",
            f"Generated {number}",
            "note",
            "generated",
            summary=f"Generated training context {number}.",
        )
        records.append(
            StoredKnowledge(record_metadata, body, tmp_path / f"generated-{number}.md")
        )

    started = time.monotonic()
    index = KnowledgeIndex(records)
    results = index.search("Generated 847")
    elapsed = time.monotonic() - started

    assert results[0].id == "note:generated-847"
    assert elapsed < 2


async def test_mcp_tools_hide_storage_ids_timestamps_and_git_details(
    knowledge_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(knowledge_tools.ROOT_ENVIRONMENT, str(knowledge_repository))
    knowledge_tools._STORES.clear()

    async with Client(create_server()) as client:
        created = await client.call_tool(
            "create_knowledge",
            {
                "title": "Windsor weather",
                "summary": "Check the forecast before packing.",
                "kind": "scratch",
                "collection": "running/weather",
                "body": "Check the forecast before packing.",
                "related": [{"record": "plan:windsor-2026", "relation": "supports"}],
            },
        )

    payload = created.data["record"]
    rendered = json.dumps(payload)
    assert payload["id"] == "scratch:windsor-weather"
    assert str(knowledge_repository) not in rendered
    assert "created_at" not in rendered
    assert "updated_at" not in rendered
    assert "revision" not in rendered
    assert "sources" not in rendered
    assert "commit" not in rendered.casefold()


async def test_knowledge_tool_annotations_distinguish_reads_from_private_writes() -> (
    None
):
    tools = {tool.name: tool for tool in await create_server().list_tools()}

    assert "ranked lexical search" in tools["search_knowledge"].description
    assert set(tools["create_knowledge"].parameters["properties"]) == {
        "title",
        "summary",
        "kind",
        "collection",
        "body",
        "tags",
        "aliases",
        "starts_at",
        "ends_at",
        "related",
    }
    assert {
        "state",
        "sources",
        "revision",
        "created_at",
        "updated_at",
    }.isdisjoint(tools["update_knowledge"].parameters["properties"])

    for name in ("search_knowledge", "browse_knowledge", "read_knowledge"):
        assert tools[name].annotations is not None
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.destructiveHint is False
    for name in ("create_knowledge", "update_knowledge", "archive_knowledge"):
        assert tools[name].annotations is not None
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is False
