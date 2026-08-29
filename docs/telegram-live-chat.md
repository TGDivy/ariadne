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
refresh loop while preserving the more informative progress state and Stop
control. Ordinary live responses are top-level messages in the same Telegram
topic. Telegram replies provide quoted input context but do not force a visual
reply from Iris.

```text
                         ┌──────────────┐
user message ───────────▶│   STARTING   │
                         └──────┬───────┘
                                │ temporary “Thinking…” + Stop
                                ▼
                         ┌──────────────┐
          follow-up ────▶│   RUNNING    │◀──── rich edit / activity
                         └───┬──────┬───┘
                             │      │ model asks a decision
                     Stop ───┘      ▼
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
and its Stop button remain present while the separate question card waits.

## Mid-turn messages

The Telegram application processes updates concurrently, while Ariadne
serializes steering into the one Codex turn:

```text
t0  You: initial request
    Ariadne: [Thinking…] [Stop]

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
│           [Stop] │─▶│ growth calculation │─▶│ not reconcile.         │
└──────────────────┘  │ ✦ Analysing…      │  └────────────────────────┘
                      │             [Stop] │       permanent bubble 1
                      └────────────────────┘

new work bubble        summary streams        final starts
┌──────────────────┐  ┌────────────────────┐  ┌────────────────────────┐
│ ✦ Thinking…     │  │ Choosing the next  │  │ Correct it before the  │
│           [Stop] │─▶│ action             │─▶│ meeting.               │
└──────────────────┘  │ ✦ Analysing…      │  └────────────────────────┘
                      │             [Stop] │       permanent bubble 2
                      └────────────────────┘

Stop at any live state ─▶ useful partial speech, or “Stopped” + [Stopped]
```

Codex is asked for concise reasoning summaries. These are generated summaries,
not raw hidden reasoning; they remain visibly provisional and are replaced by
the next native commentary or final message. Each completed commentary item is
permanent, then Ariadne opens a fresh work bubble for the remainder of the turn.

The activity footer is independent from the provisional body. A tool event can
change `Analysing…` to `Searching…`, `Reading mail…`, or `Running a command…`.
`Analysing…` and `Planning…` come from corresponding Codex events. The stable `✦`
is intentionally quiet rather than timer-driven: labels change only when a real
event occurs.

Telegram's native shimmering `<tg-thinking>` block is restricted to
`sendRichMessageDraft`. Because that draft transport can make the composer's
send action unavailable, the persistent path does not use it.

```text
Codex item start ──▶ item id → commentary/final phase
summary delta ─────▶ temporary work body ─┐
message delta ─────▶ structural stabilizer ├─▶ current message ID
tool event ────────▶ activity footer      │
latest state within 1 second ─────────────┘
item completes ───▶ exact rich source, no footer ──▶ permanent bubble
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
Media and maps use labelled placeholders. The Stop control is the deliberate
live exception. On the terminal edit, the exact complete rich source restores
safe links and structured blocks atomically. Callback data is never trusted
from model text; Ariadne constructs its own active controls.

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

## Delivery contract

Rich Messages are required for live responses, proactive messages, and question
cards. A rejected Rich Message operation is an explicit delivery failure; it is
not silently converted to classic HTML, plain text, or an inline keyboard. A
transient preview edit may leave the last valid preview visible, but terminal
completion, stop, and failure states must succeed through the Rich Message API.

## Manual smoke test

Use a private operator config on the machine running Ariadne; never paste the
bot token into an issue, PR, or chat transcript. Start with:

```bash
uv run python -m ariadne config check
uv run python -m ariadne
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
3. Start a long answer and press the red Stop button. It must immediately become
   disabled, Codex must interrupt, and partial useful text must remain with a
   terminal “Stopped” state.
4. Ask for a deployment choice that genuinely requires input. Answer once with
   a button, then repeat with typed text. In both cases the same answer should
   continue after the card settles; there must not be a second model turn.
5. Double-tap a choice and tap an old choice after completion. The first valid
   selection wins; later taps are harmless and report that the card is inactive
   or already answered.
6. Request more than 4,096 characters. It should remain one formatted Rich
   Message. Request more than 32,768 characters; final overflow should use
   additional formatted Rich Messages without broken fenced-code blocks.
7. Restart Ariadne while a question is waiting. On startup the old question
   should show Cancelled and its buttons should no longer act.
8. Make `sendRichMessage` return a Bot API `BadRequest`. The operation must fail
   explicitly and must not send a classic text or keyboard substitute.
9. Send three casual messages such as “ugh, long day”, “that was funny”, and
   “what do you think?” They should feel like continuing one chat, not three
   miniature reports with restatements and headings.
10. Trigger a mail event worth notifying about. It must use
    `send_telegram_message`; the tool must not be available in an ordinary
    Telegram-triggered turn.
11. Schedule a wake-up about an open task, then resolve it in Telegram before
    the wake-up runs. The fresh turn should be able to read the newer message
    and avoid a redundant notification. Restart Ariadne between the message and
    wake-up to verify persistence.

Automated coverage for these state transitions lives in `tests/test_bot.py`,
`tests/test_telegram_rich.py`, `tests/test_telegram_questions.py`,
`tests/test_telegram_history.py`, and `tests/test_mcp_server.py`.

## References

- [OpenAI reasoning summaries](https://developers.openai.com/api/docs/guides/reasoning#reasoning-summaries)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Telegram advanced formatting](https://core.telegram.org/bots/features#advanced-formatting-options)
- [python-telegram-bot forward compatibility](https://github.com/python-telegram-bot/python-telegram-bot/wiki/Bot-API-Forward-Compatibility)
- [Rich Text Demo bot](https://t.me/richtextdemobot)
