# Telegram live chat

This is the operator contract for Ariadne's private Telegram conversation. It
focuses on live turns; chat-history search and prompt consolidation are separate
work.

## Runtime shape

Telegram's HTTP Bot API is the official bot interface. Ariadne keeps
`python-telegram-bot` 22.8 for polling, update routing, files, and lifecycle, and
uses its documented `do_api_request` compatibility method for Rich Message API
types that the library does not model yet. `python-telegram-bot` is a
community-maintained library, not Telegram's official SDK.

The official [Rich Text Demo bot](https://t.me/richtextdemobot) is the behavior
reference. Ariadne deliberately does not use `sendRichMessageDraft`: native
drafts are ephemeral and can make the client's composer unavailable. It sends a
normal Rich Message for the current work or speech phase and edits that message
instead.

The live message is the visible activity signal, so Ariadne does not also send
Telegram typing actions. This avoids a redundant indicator and an unbounded
refresh loop while preserving the more informative progress state. Interruption
remains available through `/stop`. Ordinary live responses are top-level
messages in the same Telegram topic. Telegram replies provide quoted input
context but do not force a visual reply from Iris.

```text
                         ┌──────────────┐
user message ───────────▶│   STARTING   │
                         └──────┬───────┘
                                │ temporary “Thinking…”
                                ▼
                         ┌──────────────┐
          follow-up ────▶│   RUNNING    │◀──── rich edit / activity
                         └───┬──────┬───┘
                             │      │ model asks a decision
                    /stop ───┘      ▼
                         │   ┌──────────────┐
                         │   │ WAITING_INPUT│
                         │   └──────┬───────┘
                         │          │ button or typed text
                         │          └──────────────▶ RUNNING
                         ▼
                   ┌──────────┐       normal end       ┌──────────┐
                   │ STOPPING │────────────────────────▶│ STOPPED  │
                   └──────────┘                         └──────────┘
                                RUNNING ───────────────▶ COMPLETE
                                      error ───────────▶ FAILED
```

`WAITING_INPUT` is still part of the active Codex turn. The current live bubble
remains present while the separate question card waits, and `/stop` remains
available throughout.

## Mid-turn messages

The Telegram application processes updates concurrently, while Ariadne
serializes steering into the one Codex turn:

```text
t0  You: initial request
    Ariadne: [Thinking…]

t1  You: also compare the migration risk ─┐
t2  You: and keep the table compact       ├─ arrival-order steering queue
t3  You: reply to message 123             ┘
                                            │
                                            ▼
    Codex active handle:  t1 → t2 → t3, exactly once each
                                            │
                                            ▼
    Ariadne: [current phase continues with the steered context]
```

If Codex has not exposed an active turn handle yet, or a steering request races
with turn completion, the input stays queued. Once a safe boundary exists it is
steered or becomes the next turn. Ariadne sends no “noted” acknowledgement
bubbles, and it does not discard input on a steering exception.

## Native phase lifecycle

```text
reasoning starts       summary streams        commentary starts
┌──────────────────┐  ┌────────────────────┐  ┌────────────────────────┐
│ ✦ Analysing…    │  │ Confirming the     │  │ The growth figure does │
└──────────────────┘─▶│ growth calculation │─▶│ not reconcile.         │
                      │ ✦ Analysing…      │  └────────────────────────┘
                      └────────────────────┘       permanent bubble 1

new work bubble        summary streams        final starts
┌──────────────────┐  ┌────────────────────┐  ┌────────────────────────┐
│ ✦ Thinking…     │  │ Choosing the next  │  │ Correct it before the  │
└──────────────────┘─▶│ action             │─▶│ meeting.               │
                      │ ✦ Analysing…      │  └────────────────────────┘
                      └────────────────────┘       permanent bubble 2

`/stop` at any live state ─▶ useful partial speech, or “Stopped”
```

Codex is asked for concise reasoning summaries. These are generated summaries,
not raw hidden reasoning; they remain visibly provisional and are replaced by
the next native commentary or final message. Each completed commentary item is
permanent, then Ariadne opens a fresh work bubble for the remainder of the turn.

A provisional body is presented as a labelled quoted block above the activity,
so its temporary nature is unmistakable and stable:

```text
**Thinking**

> Connecting this with tomorrow's schedule…

✦ Reading Calendar…
```

### Notification lifecycle

Telegram notifies for a sent message but not for a later edit, so editing
`Thinking…` into the answer leaves a backgrounded reader with no notification
carrying the real text. Completed speech is therefore a fresh send, not an edit:

```text
temporary preview  ─ disable_notification=true, edited while work continues
        │
        │ commentary or final item completes
        ▼
permanent send     ─ exact complete Rich Message, normal notification,
        │            recorded in durable history
        ▼
preview deleted    ─ only after the permanent send succeeded
        │
        ├─ after commentary: a new silent preview opens for later work
        └─ after final: the turn ends with no preview
```

The permanent send is authoritative. If Telegram accepts it but the private
history write fails, Ariadne logs the failure, still removes the preview, and
never resends the message; the same no-duplicate contract covers a rejected
send, a failed preview deletion, `/stop`, and handler cancellation. Oversized
output is still split on block boundaries, and each overflow chunk is a genuine
new message.

The activity footer is independent from the provisional body. A tool event can
change `Analysing…` to `Searching memory…`, `Reading mail…`, `Running tests…`,
or another semantic activity. Known `ariadne` Mail, Calendar, and health commands,
common development commands, and Codex's structured file-read, list, and search
actions receive specific labels. The literal command, arguments, paths, queries,
and output are never copied into Telegram; an unrecognised or compound command
uses `Running a command…`.

Activity identity is retained until completion. The current item then moves to
a short result-reading state such as `Reviewing mail…` or `Reviewing test
results…`; if activities overlap, finishing the newest restores the activity
still running underneath it. `Analysing…` and `Planning…` come from corresponding
Codex events. The stable `✦` is intentionally quiet rather than timer-driven:
labels change only when a real event occurs.

Telegram's native shimmering `<tg-thinking>` block is restricted to
`sendRichMessageDraft`. Because that draft transport can make the composer's
send action unavailable, the persistent path does not use it.

```text
Codex item start ─────▶ item id → commentary/final phase
summary delta ────────▶ temporary work body ─┐
message delta ────────▶ structural stabilizer ├─▶ current message ID
activity starts/ends ─▶ lifecycle-aware footer│
latest state within 1 second ─────────────────┘
speech item completes ─▶ exact rich source, no footer ──▶ permanent bubble
commentary completes ──▶ open next temporary work bubble
```

The stabilizer commits complete paragraphs and table rows, but holds an
unfinished fence, inline format, link, table row, formula, details block, map,
or media block. Its footer names an incomplete advanced block (`Writing code…`,
`Building a table…`, and so on) instead of exposing source tags. Edits are
rate-limited to one per second with a trailing edit, so the newest real state is
not lost merely because it arrived inside the throttle window.

Automatic entity detection, links, mentions, dates, spoilers, expandable
details, media, maps, and model-authored buttons are inert while streaming.
Media and maps use labelled placeholders. On the terminal edit, the exact
complete rich source restores safe links and structured blocks atomically.
Callback data is never trusted from model text; Ariadne constructs its own
active controls.

One Rich Message accepts 32,768 characters. A live preview uses the first safe
rich chunk; a completed commentary or final message over the limit is split on
block boundaries and sent as additional Rich Messages.

## Conversation rhythm

The transport can display a report, but the conversation does not default to
one. Telegram-specific instructions treat the private chat as an ongoing
relationship and scale the response to the moment:

```text
“That was a weird day”          ─▶ one natural conversational response
“When was that meeting again?” ─▶ direct answer, usually no heading
“Audit the migration options”   ─▶ structured analysis when structure helps
```

Iris does not routinely restate or formally close the latest message, add a
heading to casual back-and-forth, or inflate an acknowledgement into a report.

## Supported presentation

Telegram's Rich Markdown parser—not a browser—owns rendering. Ariadne can send
headings, bold and italic text, underline/subscript/superscript HTML dialect
spans, strikethrough, spoilers, highlighting, inline and fenced code with
language hints, links, dividers, block and pull quotes, expandable details,
bullet/ordered/task lists, tables, footnotes, inline and block math, custom
emoji, supported media references, and native button rows. Incoming Rich
Messages are converted back into labelled Markdown structure for the model.

Arbitrary HTML, JavaScript, CSS, fonts, and text colors are not supported.
Telegram accepts only its documented allow-listed rich tags and decides the
theme. Buttons can use `primary`, `success`, `danger`, and `link` accents;
Ariadne uses these for trusted controls. More elaborate custom interfaces belong
in a Telegram Mini App.

## Interactive decision

```text
model calls ask_telegram_question
              │
              ▼
┌─────────────────────────────────┐
│ Which environment should I use? │
│ [Local] [Staging]               │
│ [Production]                    │
└──────────────┬──────────────────┘
               │ tap Production OR type “Production with a canary”
               ▼
SQLite rendezvous atomically records one answer
               │
               ├─ duplicate/stale taps: acknowledged, no second answer
               ├─ timeout/stop: card disabled
               └─ valid answer: same MCP call returns; same Codex turn resumes
```

Callback nonces, message identity, choice bounds, expiry, and the configured
private chat are validated by Ariadne. The model supplies labels, never trusted
callback identifiers. The state database defaults to
`~/.local/state/ariadne/telegram.sqlite3`, is shared with the MCP subprocess by
an absolute path, and is created mode `0600`. On restart, orphaned cards are
cancelled and disabled.

The same private SQLite database stores permanent messages observed in the
configured chat. Authenticated human messages, settled native commentary/final
bubbles, successful mail or wake-up notifications, and genuine question cards
are retained across restarts. Temporary activity/reasoning previews and other
runtime UI are excluded. `read_recent_telegram_messages` requires a
timezone-aware lower bound, optionally accepts an exclusive upper bound,
speaker/source filters and a literal case-insensitive substring, and returns
the newest bounded matches in chronological order. The store is local history
from deployment onward, not a Telegram archive backfill.

## Replies, reactions, and voice notes

A reply to a permanent bubble reaches the model as labelled quoted context
carrying the referenced message's Telegram id and author. A reply aimed at the
live preview is resolved conservatively against the visible preview content and
kept as steering; a deleted preview never becomes the durable target of a reply.

Reaction updates on permanent Iris messages in the configured private chat are
stored as the message's current reaction state, replacing any previous state
rather than accumulating counts. They are lightweight private feedback: they do
not start or steer a turn, need no acknowledgement, and reach ordinary
conversation and stewardship as uncertain evidence rather than instructions. No
individual emoji carries an assigned meaning. Aggregate anonymous counts and
reactions to temporary control or thinking messages are not retained.

A voice note is downloaded into the existing private attachment lifecycle,
bounded to 10 minutes and Telegram's 20 MB download limit before any expensive
work, and transcribed by `telegram.voice_transcription_command`. That command is
argv-only, runs without a shell with exactly one `{input}` placeholder, and must
write only the UTF-8 transcript to stdout within
`telegram.voice_transcription_timeout_seconds`. The transcript is submitted to
the shared conversation labelled as an automatic transcription, stored as a
durable `voice` human message, and the audio file is deleted. Missing,
timed-out, oversized, or failed transcription produces a brief ordinary reply
and leaves the conversation usable.

## Ephemeral command panels

Deterministic commands are temporary controls, not conversation. At most one
control panel exists per chat. Opening a panel deletes the previous one, deletes
the owner's command message, and sends the new panel silently. Panels are
excluded from durable history and are removed when any normal text, media, or
voice message is accepted, and on startup after a restart. Panel identity lives
in the same private SQLite state, so an abandoned panel is cleaned up on the
next interaction. Callbacks for a panel that is no longer the active one are
acknowledged and ignored, and a failed deletion leaves the control inert rather
than breaking conversation.

`/status` is the compact entry point and `/wakeups` a direct shortcut to the
same panel; `/new`, `/stop`, and `/settings` remain in the menu. Ordinary Mail,
Calendar, health, goal, and knowledge work stays conversational.

There is no single local-timezone setting, so the panel uses the first of
`stewardship.timezone`, `calendar.timezone`, and `health.timezone` that is not
the `UTC` default; times read in real local time even when daily initiative is
off. The panel names the zone it used, and the initiative waking window is still
labelled with its own `stewardship.timezone`.

`/status` shows, in that zone: whether Iris is ready or working;
initiative enabled/paused/running state with its last completed and next
expected cycle; counts of upcoming and failed wake-ups; the number of
conversational handoffs waiting for a quiet moment; which major private sources
are switched on; and the current Telegram model, effort, and web-research mode.
It reports what is *enabled*, and says so — connectivity is not measured.

Trusted buttons move the same panel between Wake-ups, Initiative, and Settings.
Initiative offers Run now, Pause 24 hours, Pause indefinitely, and Resume; Run
now starts one background cycle and returns immediately, and a second press
while that cycle is running is ignored. A waiting-handoff control asks for
delivery now without exposing an inbox. Wake-ups is a bounded chronological page
of five, showing local due time, a shortened note, attention level, and failure
state, with cancellation behind a second confirmation. Rescheduling and rewording
stay conversational. Settings keeps its existing interaction and still discloses
that changing model, effort, or web mode starts a fresh Codex conversation.
None of these controls is itself a model turn.

## Delivery contract

Rich Messages are required for live responses, proactive messages, and question
cards. A rejected Rich Message operation is an explicit delivery failure; it is
not silently converted to classic HTML, plain text, or an inline keyboard. A
transient preview edit may leave the last valid preview visible, but terminal
completion, stop, and failure states must succeed through the Rich Message API.

Background profiles never use the proactive delivery transport directly. They
stage internal handoffs. Once released, the shared Telegram conversation may
send settled commentary/final Rich Messages; it does not fabricate an incoming
message or send a thinking placeholder before deciding whether the handoff has
become obsolete. Those completed messages enter the same durable history as an
ordinary response, so a later non-reply message remains coherent.

## Manual smoke test

Use a private operator config on the machine running Ariadne; never paste the
bot token into an issue, PR, or chat transcript. Start with:

```bash
uv run ariadne config check
uv run ariadne serve
```

Run these cases in order:

1. Send “Count slowly to twenty and explain each step.” While it streams, send
   two follow-ups quickly. Both must remain sent in Telegram, arrive in order,
   and affect the active turn without acknowledgement clutter. Any commentary
   must settle as a separate message before the final response.
2. Request a response containing a heading, emphasis, a task list, a table,
   fenced code, inline and block math, a footnote, and expandable details.
   Complete structure must remain rendered during edits. Incomplete table rows,
   code, maths, and details must show a calm labelled state instead of raw tags.
   Links, details, media, and maps must become active only on completion.
3. Ask Iris to search Mail, inspect files, and run tests. Each operation must
   receive a semantic activity label, then a result-reviewing label; no command
   arguments, paths, queries, or output may appear in the footer.
4. Start a long answer and send `/stop`. Codex must interrupt, and partial useful
   text must remain with a terminal “Stopped” state.
5. Ask for a deployment choice that genuinely requires input. Answer once with
   a button, then repeat with typed text. In both cases the same answer should
   continue after the card settles; there must not be a second model turn.
6. Double-tap a choice and tap an old choice after completion. The first valid
   selection wins; later taps are harmless and report that the card is inactive
   or already answered.
7. Request more than 4,096 characters. It should remain one formatted Rich
   Message. Request more than 32,768 characters; final overflow should use
   additional formatted Rich Messages without broken fenced-code blocks.
8. Restart Ariadne while a question is waiting. On startup the old question
   should show Cancelled and its buttons should no longer act.
9. Make `sendRichMessage` return a Bot API `BadRequest`. The operation must fail
   explicitly and must not send a classic text or keyboard substitute.
10. Send three casual messages such as “ugh, long day”, “that was funny”, and
   “what do you think?” They should feel like continuing one chat, not three
   miniature reports with restatements and headings.
11. Trigger a mail event worth discussing. Its profile must be unable to send
    Telegram text or files and must stage a handoff. Confirm the handoff appears
    through the continuing Telegram Iris with a human turn before the quiet
    window, then repeat and allow the two-minute quiet window to elapse. Neither
    path may interleave with an active response or show a fake incoming bubble.
12. Schedule a wake-up about an open task, then resolve it in Telegram before
    the wake-up runs. The fresh turn should be able to read the newer message
    and avoid a redundant notification. Restart Ariadne between the message and
    wake-up to verify persistence.
13. Ask something whose answer has several beats. Background the app before it
    completes, then lock the phone. Each completed conversational message must
    produce its own notification containing the real text, not a bare
    `Thinking…` notification or a silent edit. Repeat with the app foregrounded
    and confirm the previews disappear and the chat is left clean. Telegram
    ultimately controls presentation; the required result is a fresh
    completed-message notification carrying the authored text.
14. Reply to an earlier permanent bubble, to a commentary bubble, and to an
    overflow chunk. Each should reach the turn as quoted context with the right
    author. Reply while a turn is streaming and confirm it is accepted as
    steering. Restart Ariadne and reply to a message from before the restart.
15. Add, change, and remove a reaction on an Iris message. None should start a
    turn or produce an acknowledgement, and only the current state should be
    retained. Ask Iris afterwards about recent reactions and confirm she treats
    them as uncertain feedback.
16. Send a short voice note, then one over 10 minutes, then one while
    `telegram.voice_transcription_command` is unset or pointed at a failing
    command. The first should continue the conversation from its transcript;
    the rest should explain briefly and leave no audio behind under
    `~/.ariadne/attachments`.
17. Exercise every panel: `/status`, its Wake-ups, Initiative, and Settings
    buttons, `/wakeups`, `/new`, `/settings`, and `/stop` with and without an
    active turn. Confirm each command message disappears, only one panel is ever
    visible, panels arrive silently, a stale button reports that the panel is
    inactive, and sending an ordinary message clears the panel. Restart Ariadne
    with a panel open and confirm it is removed on startup. Check the times,
    counts, and enabled sources on `/status` against reality.

Automated coverage for these state transitions lives in `tests/test_bot.py`,
`tests/test_telegram_rich.py`, `tests/test_telegram_questions.py`,
`tests/test_telegram_history.py`, `tests/test_telegram_panels.py`,
`tests/test_telegram_status.py`, `tests/test_telegram_voice.py`,
`tests/test_handoffs.py`, and `tests/test_mcp_server.py`.

## References

- [OpenAI reasoning summaries](https://developers.openai.com/api/docs/guides/reasoning#reasoning-summaries)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Telegram advanced formatting](https://core.telegram.org/bots/features#advanced-formatting-options)
- [python-telegram-bot forward compatibility](https://github.com/python-telegram-bot/python-telegram-bot/wiki/Bot-API-Forward-Compatibility)
- [Rich Text Demo bot](https://t.me/richtextdemobot)
