"""The one continuous conversation, driven through the real app API."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from app_harness import (
    RunningServer,
    committed,
    events_until,
    finished,
    next_event,
    of_type,
    start_server,
)
from openai_codex import LocalImageInput
from websockets.exceptions import InvalidStatus

from ariadne.conversation.inputs import PendingInputs
from ariadne.devkit.scripted import (
    Ask,
    Fail,
    Say,
    Script,
    Share,
    Sleep,
    Think,
    WaitForStop,
    script_of,
)
from ariadne.prompts.activations import SILENT_HANDOFF_RESPONSE

Start = Callable[..., Awaitable[RunningServer]]

ECHO = script_of((".*", [Think("Checking"), Say("You said: {input}", chunk=4)]))


@pytest.fixture
async def start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Start]:
    monkeypatch.setenv("HOME", str(tmp_path))
    servers: list[RunningServer] = []

    async def launch(
        script: Script = ECHO,
        *,
        directory: Path | None = None,
        steer_error: Exception | None = None,
    ) -> RunningServer:
        server = await start_server(
            directory or tmp_path / "instance", script, steer_error=steer_error
        )
        servers.append(server)
        return server

    yield launch
    for server in servers:
        if not server.http.is_closed:
            await server.close()


async def test_only_a_paired_token_reaches_commands_and_the_stream(
    start: Start,
) -> None:
    server = await start()

    assert (
        await server.http.get("/health", headers={"Authorization": ""})
    ).status_code == 200
    anonymous = await server.http.get("/v1/status", headers={"Authorization": ""})
    wrong = await server.http.get(
        "/v1/status", headers={"Authorization": "Bearer nope"}
    )
    assert anonymous.status_code == 401
    assert wrong.status_code == 401
    assert anonymous.json()["error"]["code"] == "unauthorized"
    with pytest.raises(InvalidStatus):
        async with server.stream(token="nope"):
            pass


async def test_a_message_streams_provisional_speech_then_commits_one_reply(
    start: Start,
) -> None:
    server = await start()

    async with server.stream() as stream:
        snapshot = await next_event(stream)
        sent = await server.send("hello")
        events = await events_until(stream, finished)

    assert snapshot["type"] == "snapshot"
    assert snapshot["reset"] is True
    assert sent["disposition"] == "started"
    updates = [event["text"] for event in of_type(events, "speech.updated")]
    assert updates[-1] == "You said: hello"
    assert len(updates) > 1
    (reply,) = committed(events, "iris")
    assert reply["text"] == "You said: hello"
    assert reply["phase"] == "final"
    assert events[-1]["outcome"] == "completed"
    page = (await server.http.get("/v1/messages")).json()
    assert [message["text"] for message in page["messages"]] == [
        "hello",
        "You said: hello",
    ]


async def test_a_retried_send_commits_the_message_once(start: Start) -> None:
    server = await start()

    first = await server.send("hello", client_message_id="same")
    second = await server.send("hello", client_message_id="same")
    await server.wait_idle()

    assert second["disposition"] == "duplicate"
    assert second["message"]["id"] == first["message"]["id"]
    texts = [
        m["text"] for m in (await server.http.get("/v1/messages")).json()["messages"]
    ]
    assert texts.count("hello") == 1


async def test_a_follow_up_steers_the_active_turn(start: Start) -> None:
    script = script_of(
        (
            "research",
            [
                Say("Looking now.", phase="commentary"),
                Sleep(0.4),
                Say("Done. {steered}"),
            ],
        )
    )
    server = await start(script)

    async with server.stream() as stream:
        await next_event(stream)
        await server.send("research dinner")
        await events_until(stream, lambda event: event["type"] == "speech.started")
        steered = await server.send("make it vegetarian")
        events = await events_until(stream, finished)

    assert steered["disposition"] == "steered"
    assert committed(events, "iris")[-1]["text"].endswith("make it vegetarian")
    assert len(server.scripted.turns) == 1


async def test_input_that_cannot_steer_is_kept_for_the_next_turn(start: Start) -> None:
    script = script_of(
        (
            "first",
            [Say("Working.", phase="commentary"), Sleep(0.3), Say("First done.")],
        ),
        ("second", [Say("Second handled.")]),
    )
    server = await start(script, steer_error=RuntimeError("steer rejected"))

    async with server.stream() as stream:
        await next_event(stream)
        await server.send("first")
        await events_until(stream, lambda event: event["type"] == "speech.started")
        queued = await server.send("second")
        events = await events_until(stream, finished)
        events += await events_until(stream, finished)

    assert queued["disposition"] == "queued"
    assert [m["text"] for m in committed(events, "iris")] == [
        "Working.",
        "First done.",
        "Second handled.",
    ]
    assert len(server.scripted.turns) == 2
    assert "second" in server.scripted.turns[1].input_text


async def test_stop_keeps_partial_speech_and_frees_the_conversation(
    start: Start,
) -> None:
    script = script_of(
        ("talk", [Say("one two three four five six seven", chunk=1), WaitForStop()]),
        (".*", [Say("Fresh turn.")]),
    )
    script = Script(rules=script.rules, delay=0.03)
    server = await start(script)

    async with server.stream() as stream:
        await next_event(stream)
        await server.send("talk")
        await events_until(stream, lambda event: event["type"] == "speech.updated")
        assert (await server.http.post("/v1/turn/stop")).json() == {"stopping": True}
        events = await events_until(stream, finished)
        await server.send("again")
        after = await events_until(stream, finished)

    assert events[-1]["outcome"] == "stopped"
    (partial,) = committed(events, "iris")
    assert partial["stopped"] is True
    assert "one two three four five six seven".startswith(partial["text"])
    assert after[-1]["outcome"] == "completed"
    assert (await server.http.post("/v1/turn/stop")).json() == {"stopping": False}


async def test_a_question_is_answered_by_button_and_the_turn_continues(
    start: Start,
) -> None:
    server = await start(
        script_of(
            (".*", [Ask("Which one?", ("Local", "Staging")), Say("Using {answer}.")])
        )
    )

    async with server.stream() as stream:
        await next_event(stream)
        await server.send("deploy it")
        asked = (await events_until(stream, lambda e: e["type"] == "question.asked"))[
            -1
        ]
        question = asked["question"]
        answer = await server.http.post(
            f"/v1/questions/{question['id']}/answer", json={"choice_index": 1}
        )
        events = await events_until(stream, finished)

    assert answer.json()["outcome"] == "accepted"
    assert of_type(events, "question.settled")[0]["question"]["answer"] == "Staging"
    assert [m["kind"] for m in committed(events, "human")] == ["answer"]
    assert committed(events, "iris")[-1]["text"] == "Using Staging."
    again = await server.http.post(
        f"/v1/questions/{question['id']}/answer", json={"choice_index": 0}
    )
    assert again.json()["outcome"] in {"already_answered", "inactive"}


async def test_typed_text_answers_the_waiting_question(start: Start) -> None:
    server = await start(
        script_of(
            (".*", [Ask("Which one?", ("Local", "Staging")), Say("Using {answer}.")])
        )
    )

    async with server.stream() as stream:
        await next_event(stream)
        await server.send("deploy it")
        await events_until(stream, lambda event: event["type"] == "question.asked")
        typed = await server.send("staging with a canary")
        events = await events_until(stream, finished)

    assert typed["disposition"] == "answered"
    assert committed(events, "iris")[-1]["text"] == "Using staging with a canary."


async def test_reconnecting_replays_only_missed_committed_events(start: Start) -> None:
    server = await start()
    async with server.stream() as stream:
        await next_event(stream)
        await server.send("hello")
        events = await events_until(stream, finished)
    last = max(event["seq"] for event in events if event["seq"] is not None)

    async with server.stream(after=last - 2) as stream:
        snapshot = await next_event(stream)
        replayed = [await next_event(stream), await next_event(stream)]

    assert snapshot["reset"] is False
    assert snapshot["seq"] == last
    assert [event["seq"] for event in replayed] == [last - 1, last]
    assert replayed[-1]["type"] == "turn.finished"


async def test_a_proactive_handoff_speaks_once_and_completes_its_batch(
    start: Start,
) -> None:
    server = await start(script_of(("background handoffs", [Say("Your train moved.")])))
    handoffs = server.runtime.handoffs
    handoffs.state.stage(activation_key="mail-1", source="mail", body="Train moved")
    handoffs.state.release("mail-1")

    async with server.stream() as stream:
        await next_event(stream)
        assert await handoffs.deliver_now(server.runtime.core) is True
        events = await events_until(stream, finished)

    assert events[-1]["kind"] == "proactive"
    (spoken,) = committed(events, "iris")
    assert spoken["origin"] == "proactive"
    assert of_type(events, "speech.updated") == []
    delivered = handoffs.state.for_activation("mail-1")
    assert delivered is not None and delivered.status == "completed"
    assert await handoffs.deliver_now(server.runtime.core) is False


async def test_an_obsolete_handoff_can_end_silently(start: Start) -> None:
    server = await start(
        script_of(("background handoffs", [Say(SILENT_HANDOFF_RESPONSE)]))
    )
    handoffs = server.runtime.handoffs
    handoffs.state.stage(activation_key="mail-1", source="mail", body="Old news")
    handoffs.state.release("mail-1")

    async with server.stream() as stream:
        await next_event(stream)
        await handoffs.deliver_now(server.runtime.core)
        events = await events_until(stream, finished)

    assert events[-1]["outcome"] == "silent"
    assert committed(events) == []


async def test_waiting_handoffs_join_the_next_direct_turn(start: Start) -> None:
    server = await start()
    handoffs = server.runtime.handoffs
    handoffs.state.stage(activation_key="mail-1", source="mail", body="Train moved")
    handoffs.state.release("mail-1")

    await server.send("morning")
    await server.wait_idle()

    turn_input = server.scripted.turns[0].input_text
    assert "<background_handoffs>" in turn_input and "Train moved" in turn_input
    assert turn_input.rstrip().endswith("</current_human_message>")
    joined = handoffs.state.for_activation("mail-1")
    assert joined is not None and joined.status == "completed"


async def test_a_failed_turn_is_reported_and_the_next_message_starts_fresh(
    start: Start,
) -> None:
    server = await start(
        script_of(("boom", [Fail("broken")]), (".*", [Say("Fine now.")]))
    )

    async with server.stream() as stream:
        await next_event(stream)
        await server.send("boom")
        failed = await events_until(stream, finished)
        await server.send("again")
        recovered = await events_until(stream, finished)

    assert failed[-1]["outcome"] == "failed"
    assert failed[-1]["error"]
    assert recovered[-1]["outcome"] == "completed"


async def test_settings_and_resets_wait_for_the_active_turn(start: Start) -> None:
    server = await start(script_of(("slow", [WaitForStop()]), (".*", [Say("ok")])))

    async with server.stream() as stream:
        await next_event(stream)
        await server.send("slow")
        await events_until(stream, lambda event: event["type"] == "turn.started")
        busy = await server.http.put("/v1/settings", json={"effort": "low"})
        busy_reset = await server.http.post("/v1/conversation/new")
        await server.http.post("/v1/turn/stop")
        await events_until(stream, finished)
        changed = await server.http.put("/v1/settings", json={"effort": "low"})
        reset = await events_until(
            stream, lambda event: event["type"] == "conversation.reset"
        )

    assert busy.status_code == 409
    assert busy_reset.status_code == 409
    assert changed.json()["effort"] == "low"
    assert reset[-1]["reason"] == "settings"
    current = (await server.http.get("/v1/settings")).json()["current"]
    assert current["effort"] == "low"


async def test_images_and_documents_reach_the_turn_as_attachments(start: Start) -> None:
    server = await start()

    image = await server.http.put(
        "/v1/attachments",
        content=b"\x89PNG image bytes",
        headers={"Content-Type": "image/png", "X-Filename": "../race.png"},
    )
    document = await server.http.put(
        "/v1/attachments",
        content=b"Notes for the trip",
        headers={"Content-Type": "text/plain", "X-Filename": "notes.txt"},
    )
    assert image.status_code == document.status_code == 201
    sent = await server.send(
        "Have a look",
        attachment_ids=[image.json()["id"], document.json()["id"]],
    )
    await server.wait_idle()

    assert image.json()["filename"] == "race.png"
    assert [item["kind"] for item in sent["message"]["attachments"]] == [
        "image",
        "document",
    ]
    raw = server.scripted.turns[0].raw_input
    assert isinstance(raw, list)
    assert any(isinstance(item, LocalImageInput) for item in raw)
    assert "notes.txt" in server.scripted.turns[0].input_text


async def test_files_iris_shares_appear_as_one_message(
    start: Start, tmp_path: Path
) -> None:
    report = tmp_path / "report.md"
    report.write_text("# Shortlist", encoding="utf-8")
    server = await start(
        script_of((".*", [Share((str(report),), "The shortlist"), Say("Shared it.")]))
    )

    async with server.stream() as stream:
        await next_event(stream)
        await server.send("share it")
        events = await events_until(stream, finished)

    shared = next(m for m in committed(events, "iris") if m["attachments"])
    assert shared["text"] == "The shortlist"
    download = await server.http.get(f"/v1/files/{shared['attachments'][0]['id']}")
    assert download.status_code == 200
    assert download.text == "# Shortlist"


async def test_push_alerts_only_when_no_client_is_watching(start: Start) -> None:
    server = await start()

    await server.send("while away")
    await server.wait_idle()
    await asyncio.sleep(0.05)
    async with server.stream() as stream:
        await next_event(stream)
        await server.send("while watching")
        await events_until(stream, finished)

    assert [(item.body, item.level) for item in server.push.notifications] == [
        ("You said: while away", "active")
    ]


async def test_unknown_references_and_oversized_bodies_are_refused(
    start: Start,
) -> None:
    server = await start()

    missing_reply = await server.http.post(
        "/v1/messages",
        json={"client_message_id": "a", "text": "hi", "reply_to_id": 999},
    )
    missing_attachment = await server.http.post(
        "/v1/messages",
        json={"client_message_id": "b", "text": "hi", "attachment_ids": ["att_x"]},
    )
    empty = await server.http.post(
        "/v1/messages", json={"client_message_id": "c", "text": "  "}
    )
    oversized = await server.http.post(
        "/v1/messages",
        content=b"x" * 70_000,
        headers={"Content-Type": "application/json"},
    )

    assert missing_reply.status_code == 404
    assert missing_attachment.status_code == 404
    assert empty.status_code == 422
    assert oversized.status_code == 413


async def test_reactions_apply_only_to_iris_messages(start: Start) -> None:
    server = await start()
    sent = await server.send("hello")
    await server.wait_idle()
    messages = (await server.http.get("/v1/messages")).json()["messages"]
    reply = next(message for message in messages if message["speaker"] == "iris")

    async with server.stream() as stream:
        await next_event(stream)
        reacted = await server.http.put(
            f"/v1/messages/{reply['id']}/reactions", json={"reactions": ["❤️"]}
        )
        event = await next_event(stream)
    refused = await server.http.put(
        f"/v1/messages/{sent['message']['id']}/reactions", json={"reactions": ["❤️"]}
    )

    assert reacted.json() == {"message_id": reply["id"], "reactions": ["❤️"]}
    assert event["type"] == "reactions.changed"
    assert refused.status_code == 404


async def test_the_conversation_continues_after_a_restart(
    start: Start, tmp_path: Path
) -> None:
    directory = tmp_path / "instance"
    first = await start(directory=directory)
    await first.send("before restart")
    await first.wait_idle()
    (thread_id,) = first.scripted.started
    await first.close()

    second = await start(directory=directory)
    await second.send("after restart")
    await second.wait_idle()

    assert second.scripted.resumed == [thread_id]
    assert second.scripted.started == []


async def test_input_accepted_before_a_crash_is_delivered_after_restart(
    start: Start, tmp_path: Path
) -> None:
    directory = tmp_path / "instance"
    directory.mkdir()
    PendingInputs(directory / "conversation.sqlite3").add(
        message_id=1, prompt="accepted before the crash", image_paths=()
    )

    server = await start(directory=directory)
    await server.wait_idle()

    assert server.scripted.turns[0].input_text == "accepted before the crash"
    assert server.runtime.core.inputs.count() == 0
