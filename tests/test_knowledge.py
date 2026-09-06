import json
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from fastmcp import Client

from ariadne.codex.resolver import resolve_profile
from ariadne.knowledge import (
    KnowledgeConflict,
    KnowledgeMetadata,
    KnowledgeSearchError,
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
from ariadne.scripts.knowledge import main as validate_knowledge_main


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
    *,
    summary: str | None = None,
    aliases: tuple[str, ...] = (),
    links: tuple[str, ...] = (),
) -> KnowledgeMetadata:
    return KnowledgeMetadata(
        id=identifier,
        title=title,
        summary=summary or f"Useful context about {title}.",
        aliases=aliases,
        links=links,
    )


def write_record(
    root: Path,
    relative: str,
    record: KnowledgeMetadata,
    body: str,
) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(render_document(record, body))


@pytest.fixture
def knowledge_repository(tmp_path: Path) -> Path:
    root = tmp_path / "knowledge"
    origin = tmp_path / "origin.git"
    root.mkdir()
    (root / "archive").mkdir()
    git(tmp_path, "init", "--bare", str(origin))
    git(root, "init", "--initial-branch=main")
    git(root, "config", "user.name", "Knowledge Test")
    git(root, "config", "user.email", "knowledge@example.test")
    git(root, "remote", "add", "origin", str(origin))
    write_record(
        root,
        "now.md",
        metadata(
            "now",
            "Now",
            summary="The Windsor race is the immediate priority.",
        ),
        "Transport and breakfast still need attention.",
    )
    write_record(
        root,
        "goal/running.md",
        metadata(
            "running",
            "Running",
            summary="Build consistency and complete a half marathon comfortably.",
            aliases=("half marathon",),
        ),
        "Build consistency and complete a half marathon comfortably.",
    )
    write_record(
        root,
        "event/running/windsor-trail-run.md",
        metadata(
            "windsor-2026",
            "Windsor Trail Run",
            summary="Confirmed Windsor half marathon; transport is not arranged.",
            links=("running",),
        ),
        "Collect the bib before the race. Transport is not arranged yet.",
    )
    write_record(
        root,
        "event/travel/trainline-booking.md",
        metadata(
            "trainline-windsor",
            "Trainline booking",
            summary="Train tickets from Waterloo to Windsor for race day.",
            links=("windsor-2026",),
        ),
        "Outbound train reaches Windsor at 08:44. The return is flexible.",
    )
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial knowledge")
    git(root, "push", "-u", "origin", "main")
    return root


def test_markdown_round_trip_has_minimal_front_matter(tmp_path: Path) -> None:
    path = tmp_path / "lily.md"
    expected = metadata(
        "lily",
        "Lily Example",
        summary="A friend from university.",
        aliases=("Lil",),
        links=("university",),
    )
    path.write_bytes(render_document(expected, "Met at university."))

    loaded = parse_document(path)
    rendered = path.read_text(encoding="utf-8")
    front_matter = rendered.split("---", 2)[1]

    assert loaded.metadata == expected
    assert loaded.body == "Met at university."
    assert set(json.loads(json.dumps(loaded.metadata.model_dump()))) == {
        "id",
        "title",
        "summary",
        "aliases",
        "links",
    }
    assert "title:" not in front_matter
    assert "summary:" not in front_matter
    assert "created_at" not in rendered
    assert "kind:" not in rendered


def test_unknown_front_matter_fields_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "example.md"
    path.write_text(
        "---\n"
        "schema: 1\n"
        "id: example\n"
        "kind: note\n"
        "---\n\n"
        "# Example\n\n"
        "Example summary.\n",
        encoding="utf-8",
    )

    with pytest.raises(KnowledgeValidationError, match="invalid"):
        parse_document(path)


def test_search_is_or_ranked_prefix_stemmed_and_typo_tolerant(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    broad = store.search("Windsor train")
    typo = store.search("Windosr")
    prefix = store.search("train")
    plural = store.search("trains")

    assert {result.id for result in broad[:2]} == {
        "windsor-2026",
        "trainline-windsor",
    }
    windsor = next(result for result in broad if result.id == "windsor-2026")
    assert windsor.matched_terms == ("windsor",)
    assert windsor.unmatched_terms == ("train",)
    assert typo[0].id == "windsor-2026"
    assert prefix[0].id == "trainline-windsor"
    assert plural[0].id == "trainline-windsor"


def test_search_can_narrow_to_a_folder_and_its_descendants(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    all_folders = store.search("Windsor", folder="")
    events = store.search("Windsor", folder="event")
    running = store.search("Windsor", folder="event/running")

    assert {result.id for result in all_folders} == {
        "now",
        "windsor-2026",
        "trainline-windsor",
    }
    assert {result.id for result in events} == {
        "windsor-2026",
        "trainline-windsor",
    }
    assert [result.id for result in running] == ["windsor-2026"]
    assert store.search("travel")[0].id == "trainline-windsor"


def test_list_returns_only_immediate_semantic_folders_and_direct_records(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    root = store.list_folder()
    events = store.list_folder("event")
    running = store.list_folder("event/running")

    assert [(item.folder, item.record_count) for item in root.folders] == [
        ("event", 2),
        ("goal", 1),
    ]
    assert [(record.id, record.title) for record in root.records] == [("now", "Now")]
    assert [item.folder for item in events.folders] == [
        "event/running",
        "event/travel",
    ]
    assert events.records == ()
    assert [(record.id, record.title) for record in running.records] == [
        ("windsor-2026", "Windsor Trail Run")
    ]
    assert ".md" not in json.dumps(root.model_dump(mode="json"))
    with pytest.raises(KnowledgeValidationError, match="does not exist"):
        store.list_folder("missing")


def test_list_reports_independent_bounded_counts(
    knowledge_repository: Path,
) -> None:
    listing = KnowledgeStore(knowledge_repository).list_folder(limit=1)

    assert listing.folder_count == 2
    assert listing.folders_truncated is True
    assert len(listing.folders) == 1
    assert listing.record_count == 1
    assert listing.records_truncated is False


def test_long_query_terms_do_not_reverse_match_tiny_field_words(
    tmp_path: Path,
) -> None:
    records = (
        StoredKnowledge(
            metadata(
                "ithaca-deployment",
                "Ithaca deployment",
                summary="Investigate the failed Ithaca API deployment.",
            ),
            "The deployment needs review.",
            tmp_path / "ithaca-deployment.md",
        ),
        StoredKnowledge(
            metadata(
                "hotel",
                "Hotel",
                summary="It is a confirmed booking.",
            ),
            "I should check in tomorrow.",
            tmp_path / "hotel.md",
        ),
    )

    results = KnowledgeIndex(records).search("Ithaca deployment")

    assert [result.id for result in results] == ["ithaca-deployment"]


def test_natural_watching_query_ranks_the_actual_preference_first(
    tmp_path: Path,
) -> None:
    records = (
        StoredKnowledge(
            metadata(
                "watching-preferences",
                "Watching preferences",
                summary="What Divy enjoys watching and what suits different moods.",
            ),
            "Choose one humane, reflective film when he asks what he should watch.",
            tmp_path / "watching-preferences.md",
        ),
        StoredKnowledge(
            metadata(
                "work-reflection",
                "What public work suggests",
                summary="A reflection on how work should feel.",
            ),
            "I should keep the uncertainty visible.",
            tmp_path / "work-reflection.md",
        ),
        StoredKnowledge(
            metadata(
                "ivona",
                "Ivona",
                summary="I know Ivona from a training class.",
            ),
            "I should ask how the training is going.",
            tmp_path / "ivona.md",
        ),
    )

    results = KnowledgeIndex(records).search("what should I watch")

    assert results[0].id == "watching-preferences"


def test_cached_search_index_serves_concurrent_worker_threads(
    knowledge_repository: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ROOT_ENVIRONMENT, str(knowledge_repository))
    knowledge_tools._STORES.clear()
    assert knowledge_tools.search_knowledge("Windsor")["count"] == 3
    queries = ("Windsor", "train", "running", "Windosr")
    barrier = Barrier(len(queries) + 1)

    def search(query: str) -> dict[str, object]:
        barrier.wait()
        return knowledge_tools.search_knowledge(query)

    try:
        with ThreadPoolExecutor(max_workers=len(queries)) as workers:
            searches = [workers.submit(search, query) for query in queries]
            barrier.wait()
            results = [search.result() for search in searches]
    finally:
        knowledge_tools._STORES.clear()

    assert all(result["count"] for result in results)


def test_sqlite_search_failures_use_the_stable_knowledge_error(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)
    store.search("Windsor")
    assert store._index is not None
    store._index._database.close()

    with pytest.raises(
        KnowledgeSearchError,
        match="Private knowledge search is temporarily unavailable",
    ) as raised:
        store.search("Windsor")

    assert isinstance(raised.value.__cause__, sqlite3.Error)


def test_search_and_read_return_compact_untyped_links(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    result = store.search("Windsor")[0]
    goal, plan = store.read(("running", "windsor-2026"))

    result_links = {link.id: link for link in result.links}
    assert result_links["trainline-windsor"].summary.startswith("Train tickets")
    assert result_links["trainline-windsor"].folder == "event/travel"
    assert {link.id for link in plan.links} == {"running", "trainline-windsor"}
    assert plan.folder == "event/running"
    assert goal.links[0].id == "windsor-2026"
    assert "body" not in result.links[0].model_dump()
    assert "relation" not in result.links[0].model_dump()


def test_profile_resolution_injects_root_overview_and_now_without_taxonomy(
    knowledge_repository: Path,
) -> None:
    enriched = resolve_profile(
        MAIL_PROFILE,
        vault=knowledge_repository,
        human="Divy",
        knowledge_root=knowledge_repository,
    )

    assert enriched.base_instruction_sources[-1] == "ariadne.prompts/knowledge.md"
    assert enriched.developer_instruction_sources[-2:] == (
        "generated/thread-overview",
        "generated/current-context",
    )
    assert "## Thread overview" in enriched.developer_instructions
    assert "Active records: 4." in enriched.developer_instructions
    assert "`event` (2), `goal` (1)" in enriched.developer_instructions
    assert "## Current context" in enriched.developer_instructions
    assert "Transport and breakfast still need attention." in (
        enriched.developer_instructions
    )
    assert "Kinds:" not in enriched.developer_instructions
    assert "Collections:" not in enriched.developer_instructions
    assert dict(enriched.mcp_environment_values)[ROOT_ENVIRONMENT] == str(
        knowledge_repository
    )
    assert str(knowledge_repository) not in enriched.developer_instructions


def test_whole_repository_validation_checks_records_and_links(
    knowledge_repository: Path,
) -> None:
    report = validate_repository(knowledge_repository)

    assert report.records == 4
    assert report.links == 2
    assert report.archived == 0


def test_whole_repository_validation_rejects_a_broken_link(
    knowledge_repository: Path,
) -> None:
    path = knowledge_repository / "goal/running.md"
    record = parse_document(path)
    broken = record.metadata.model_copy(update={"links": ("missing",)})
    path.write_bytes(render_document(broken, record.body))

    with pytest.raises(KnowledgeValidationError, match="missing records"):
        validate_repository(knowledge_repository)


def test_whole_repository_validation_rejects_a_stale_title_filename(
    knowledge_repository: Path,
) -> None:
    original = knowledge_repository / "event/running/windsor-trail-run.md"
    original.rename(original.with_name("old-title.md"))

    with pytest.raises(KnowledgeValidationError, match="does not match title"):
        validate_repository(knowledge_repository)


def test_runtime_repository_errors_do_not_expose_paths_or_filenames(
    knowledge_repository: Path,
) -> None:
    path = knowledge_repository / "goal/running.md"
    path.write_text("not a knowledge record", encoding="utf-8")

    with pytest.raises(
        KnowledgeValidationError,
        match="invalid and needs operator review",
    ) as raised:
        KnowledgeStore(knowledge_repository).search("running")

    message = str(raised.value)
    assert str(knowledge_repository) not in message
    assert "running.md" not in message


def test_hidden_repository_markdown_is_not_a_knowledge_record(
    knowledge_repository: Path,
) -> None:
    workflow = knowledge_repository / ".github/README.md"
    workflow.parent.mkdir()
    workflow.write_text("Not private knowledge.", encoding="utf-8")

    report = validate_repository(knowledge_repository)

    assert report.records == 4


def test_nested_semantic_folders_are_valid(
    knowledge_repository: Path,
) -> None:
    nested = knowledge_repository / "project/ariadne/example.md"
    write_record(
        knowledge_repository,
        "project/ariadne/example.md",
        metadata("example", "Example"),
        "This belongs in a meaningful nested folder.",
    )

    report = validate_repository(knowledge_repository)

    assert nested.is_file()
    assert report.records == 5


def test_reserved_records_wrapper_is_rejected(knowledge_repository: Path) -> None:
    write_record(
        knowledge_repository,
        "records/example.md",
        metadata("example", "Example"),
        "This folder is reserved by the Thread.",
    )

    with pytest.raises(KnowledgeValidationError, match="reserved"):
        validate_repository(knowledge_repository)


def test_runtime_requires_the_archive_namespace(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    git(root, "init", "--initial-branch=main")

    with pytest.raises(KnowledgeValidationError, match="archive/"):
        KnowledgeStore(root)


@pytest.mark.parametrize("folder", ["archive", "archive/old", "records", "People"])
def test_reserved_or_nonsemantic_folders_are_rejected_before_writing(
    knowledge_repository: Path,
    folder: str,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    with pytest.raises(KnowledgeValidationError, match="folder"):
        store.create(
            title="Invalid location",
            summary="This should never be written.",
            body="This should never be written.",
            folder=folder,
        )

    assert not (knowledge_repository / "invalid-location.md").exists()


def test_current_context_cannot_move_out_of_the_root(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    with pytest.raises(KnowledgeValidationError, match="belongs at the root"):
        store.update("now", folder="journal")

    assert (knowledge_repository / "now.md").is_file()


def test_validation_command_reports_a_compact_summary(
    knowledge_repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["ariadne-knowledge", str(knowledge_repository)])

    validate_knowledge_main()

    assert capsys.readouterr().out == (
        "Knowledge is valid: 4 records, 2 links, 0 archived.\n"
    )


def test_create_generates_stable_id_in_a_semantic_folder_then_pushes(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    created = store.create(
        title="Race Breakfast & Snacks",
        summary="Possible food for race morning and recovery.",
        body="Try oats, banana, and a bagel.",
        folder="plan/health",
        links=("windsor-2026",),
    )

    path = knowledge_repository / "plan/health/race-breakfast-snacks.md"
    rendered = path.read_text(encoding="utf-8")
    assert created.metadata.id == "race-breakfast-snacks"
    assert created.folder == "plan/health"
    assert path.is_file()
    assert "created_at" not in rendered
    assert "kind:" not in rendered
    assert git(knowledge_repository, "status", "--porcelain") == ""
    assert git(knowledge_repository, "rev-list", "@{upstream}..HEAD") == ""


def test_update_can_move_folder_and_title_while_preserving_id_and_links(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    updated = store.update(
        "windsor-2026",
        title="Windsor Race Day",
        summary="Race and train arrangements are confirmed.",
        body="Arrive at 08:44 and go directly to bib collection.",
        folder="plan/running",
    )
    booking = store.read(("trainline-windsor",))[0]

    destination = knowledge_repository / "plan/running/windsor-race-day.md"
    assert not (knowledge_repository / "event/running/windsor-trail-run.md").exists()
    assert destination.is_file()
    assert updated.metadata.id == "windsor-2026"
    assert updated.folder == "plan/running"
    assert booking.links[0].id == "windsor-2026"
    assert git(knowledge_repository, "status", "--porcelain") == ""


def test_archive_moves_record_and_hides_it_without_deleting(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    archived = store.archive("trainline-windsor", "The journey is complete.")

    assert archived.archived is True
    assert archived.folder == "event/travel"
    assert not (knowledge_repository / "event/travel/trainline-booking.md").exists()
    assert (
        knowledge_repository / "archive/event/travel/trainline-booking.md"
    ).is_file()
    assert store.search("Trainline") == ()
    assert store.search("Trainline", include_archived=True)[0].id == "trainline-windsor"
    archived_event = store.list_folder("event", archived=True)
    assert [(item.folder, item.record_count) for item in archived_event.folders] == [
        ("event/travel", 1)
    ]
    with pytest.raises(KnowledgeConflict, match="already archived"):
        store.archive("trainline-windsor", "Do not archive twice.")


def test_create_avoids_ids_that_are_already_an_alias(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)
    store.update(
        "running",
        aliases=("half marathon", "race-breakfast"),
    )

    created = store.create(
        title="Race breakfast",
        summary="An incomplete food thought.",
        body="An incomplete food thought.",
    )

    assert created.metadata.id == "race-breakfast-2"


def test_update_rejects_contradictory_patch_instead_of_guessing(
    knowledge_repository: Path,
) -> None:
    store = KnowledgeStore(knowledge_repository)

    with pytest.raises(KnowledgeValidationError, match="updated and cleared"):
        store.update("windsor-2026", links=("running",), clear=("links",))


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
            body="Do not write this.",
        )

    assert unmanaged.read_text(encoding="utf-8") == "mine"
    assert not (knowledge_repository / "blocked.md").exists()


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
            body="The local commit should survive.",
        )

    assert git(knowledge_repository, "status", "--porcelain") == ""
    assert git(knowledge_repository, "rev-list", "--count", "@{upstream}..HEAD") == "1"

    hook.unlink()
    store.create(
        title="Next thought",
        summary="The operation first synchronizes the prior commit.",
        body="Then it writes this record.",
    )

    assert store.search("Durable retry")[0].id == "durable-retry"
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
        "project/remote-change.md",
        metadata(
            "remote-change",
            "Remote change",
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
        body="This is created only after the automatic pull.",
    )

    assert store.search("Remote change")[0].id == "remote-change"
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
        body="A useful experiment.",
    )

    assert reader.search("new thought")[0].title == "New thought"


def test_thousand_record_index_remains_small_and_retrievable(tmp_path: Path) -> None:
    records = []
    for number in range(1_000):
        body = f"Generated context number {number} about training and recovery."
        record_metadata = metadata(
            f"generated-{number}",
            f"Generated {number}",
            summary=f"Generated training context {number}.",
        )
        records.append(
            StoredKnowledge(record_metadata, body, tmp_path / f"generated-{number}.md")
        )

    started = time.monotonic()
    index = KnowledgeIndex(records)
    results = index.search("Generated 847")
    elapsed = time.monotonic() - started

    assert results[0].id == "generated-847"
    assert elapsed < 2


async def test_mcp_tools_hide_storage_and_git_details(
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
                "body": "Check the forecast before packing.",
                "folder": "event/running",
                "links": ["windsor-2026"],
            },
        )

    payload = created.data["record"]
    rendered = json.dumps(payload)
    assert payload["id"] == "windsor-weather"
    assert payload["folder"] == "event/running"
    assert str(knowledge_repository) not in rendered
    assert "created_at" not in rendered
    assert "updated_at" not in rendered
    assert "kind" not in rendered
    assert "relation" not in rendered
    assert "commit" not in rendered.casefold()


async def test_knowledge_tool_contract_is_small_and_annotations_are_accurate() -> None:
    tools = {tool.name: tool for tool in await create_server().list_tools()}

    assert "ranked lexical search" in tools["search_knowledge"].description
    assert set(tools["create_knowledge"].parameters["properties"]) == {
        "title",
        "summary",
        "body",
        "folder",
        "aliases",
        "links",
    }
    assert set(tools["update_knowledge"].parameters["properties"]) == {
        "id",
        "title",
        "summary",
        "body",
        "folder",
        "aliases",
        "links",
        "clear",
    }
    assert set(tools["list_knowledge"].parameters["properties"]) == {
        "folder",
        "archived",
        "limit",
    }
    assert "folder" in tools["search_knowledge"].parameters["properties"]
    assert "browse_knowledge" not in tools

    for name in ("search_knowledge", "list_knowledge", "read_knowledge"):
        assert tools[name].annotations is not None
        assert tools[name].annotations.readOnlyHint is True
        assert tools[name].annotations.destructiveHint is False
    for name in ("create_knowledge", "update_knowledge", "archive_knowledge"):
        assert tools[name].annotations is not None
        assert tools[name].annotations.readOnlyHint is False
        assert tools[name].annotations.destructiveHint is False
