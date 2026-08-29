# Ariadne

[![CI](https://github.com/TGDivy/ariadne/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/TGDivy/ariadne/actions/workflows/ci.yml)
[![Grafana](https://img.shields.io/badge/Grafana-observability-orange?logo=grafana)](https://niftytortoise1067.grafana.net/d/tggz4bj/ariadne-codex-ai-agent-overview?from=now-7d&to=now&timezone=browser&refresh=auto&kiosk=true)

Ariadne is the system that runs Iris on your own machine and connects her to you
over a private Telegram chat. She follows The Thread wherever it leads, not an
assistant you own.

Current status: **Milestone 2 — semantic knowledge foundation**.

## Run

1. Create a Telegram bot with BotFather and note its token.
2. Clone your private The Thread vault locally:

   ```bash
   git clone https://example.com/your/thread-repository.git ~/ariadne-thread
   ```

3. Create the private TOML configuration and fill in your values:

   ```bash
   mkdir -p ~/.config/ariadne
   cp config.example.toml ~/.config/ariadne/config.toml
   chmod 600 ~/.config/ariadne/config.toml
   $EDITOR ~/.config/ariadne/config.toml
   ```

   `human_name` is the name Iris calls you by; it is substituted into her
   instructions. `vault` must point to the canonical private-knowledge clone.
   It remains Codex's working directory. Iris can read anywhere, write anywhere
   under your home directory, and reach only the domains in `NETWORK_DOMAINS`
   in `src/ariadne/profile.py`. The Telegram Codex model, reasoning
   effort, and web-research setting are also applied explicitly from this
   file.

4. Ensure Codex is already authenticated on this machine.
5. Optionally, give the bot its name, descriptions, and profile photo — set
   fields under `[telegram.identity]` and run once:

   ```bash
   uv run python -m ariadne.scripts.bot_profile
   ```

   This talks to Telegram directly and changes the bot itself, not the running
   process; it only needs to be run again when one of these should change.
6. Validate the effective configuration, then run:

   ```bash
   uv run python -m ariadne config check
   uv run python -m ariadne
   ```

   `python -m ariadne config show` prints the effective configuration with
   secrets redacted. Set `ARIADNE_CONFIG` for all commands, or pass `--config`
   to the command you are running, to use a non-default path.

A shallow map and the current knowledge vocabulary are generated into the
prompt; record bodies are retrieved on demand through semantic search, browse,
and read operations. Iris creates and updates knowledge through those operations
while Ariadne owns validation and Git synchronization. Send `/new` to start a
fresh Codex conversation while retaining private knowledge.

Use `/settings` to choose an available model, supported reasoning effort, and
live web research for the running process. Each change starts a new in-memory
Codex conversation. Use `/stop` to ask Codex to interrupt the active turn; it
cannot undo work that already completed.

Telegram, mail, and each revisit attention level have independent turn
profiles. To inspect the exact model, prompts, tools, thread behavior,
permissions, and forwarded environment variable names that a surface will use,
run:

```bash
uv run python -m ariadne.scripts.profile telegram
uv run python -m ariadne.scripts.profile mail
uv run python -m ariadne.scripts.profile revisit-focused
```

Add `--json` for machine-readable output. Inspection never prints environment
values. Every declarative source profile lives together in
`src/ariadne/profile.py`; runtime trigger policy such as polling, queues,
retries, and UID state remains in each surface's runtime code.

Important companion behaviours can also be replayed with synthetic inputs.
Listing and inspection are free, deterministic, and CI-safe; a real Codex run
is an explicit local command and may incur usage:

```bash
uv run python -m ariadne.scripts.behavior list
uv run python -m ariadne.scripts.behavior show race-confirmation
uv run python -m ariadne.scripts.behavior run race-confirmation
```

See [`docs/behaviour-scenarios.md`](docs/behaviour-scenarios.md) for the isolation
boundary, recorded fake capabilities, and report contents.

### Read-only mail export experiment

The one-off operator script can export recent iCloud Mail messages for local
analysis. Configure `username` and `app_password` under `[icloud]`, then run:

```bash
uv run python -m ariadne.scripts.mail_export \
  --limit 1000 --output mail-export.jsonl
```

The export is JSONL so it can retain recipients, thread headers, body text, and
attachment metadata without storing attachment binaries. It selects the
mailbox read-only and uses batched `BODY.PEEK[]` fetches; it does not send,
move, delete, or mark messages read. The default folder is `INBOX`; use
`--folder` to inspect a different mailbox. Use `--batch-size` to tune the
number of messages per request.

Messages sent while Ariadne is working are not rejected: they steer the Codex
turn that is already running, so Codex folds them into the work in flight. If
Codex is between turn states, Ariadne retains the messages and drains them in
arrival order instead of rejecting or silently dropping them.
Using Telegram's Reply action includes the immediate replied-to message's full
text or caption as labelled context. Replies to old media include its caption,
but do not re-download the old file or image.

Permanent messages visible in the private chat are retained in the existing
Telegram state database. Fresh mail and wake-up turns can deliberately use
`read_recent_telegram_messages` to reconcile newer conversation before acting.
It supports bounded time, speaker, source, and literal text filters. Temporary
thinking/activity edits, hidden reasoning, settings UI, and tool activity are
never part of this history; mail, Calendar, knowledge, and wake-up state remain
authoritative in their own stores.

The live-chat state diagrams, supported rich content, delivery contract, and
manual test cases are in [`docs/telegram-live-chat.md`](docs/telegram-live-chat.md).

At the default INFO log level, stdout shows privacy-safe operational progress:
Telegram message and turn lifecycle events, plus mail connection, discovery,
queue, routing, Codex-processing, mailbox-action, and MCP call lifecycle events.
These logs use IDs, counts, models, routes, tool names, actions, statuses, and
durations; they do not include Telegram message text, mail subjects/bodies/
addresses, MCP arguments/results, or credentials. One deliberate exception is
a failed `send_telegram_message` call: its attempted message arguments and tool
error are logged so a missed notification can be diagnosed. Credentials and
authorization headers are never part of that MCP request.

### OpenTelemetry and Grafana Cloud

Ariadne can send Codex metrics and traces directly to any OTLP/HTTP endpoint.
Telemetry is opt-in under `[telemetry]`, so the ordinary runtime has no
additional service to operate. Ariadne does not read `OTEL_*` environment
variables or fall back to them.

For Grafana Cloud, follow its
[OTLP endpoint guide](https://grafana.com/docs/grafana-cloud/send-data/otlp/send-data-otlp/):
open the stack's **Configure → OpenTelemetry** tile, generate an access token,
and put the base endpoint and authorization value in Ariadne's existing private
TOML file:

```toml
[telemetry]
enabled = true
endpoint = "https://otlp-gateway-REGION.grafana.net/otlp"
authorization = "Basic YOUR_TOKEN"
service_name = "ariadne"
metrics = true
traces = true
export_interval_seconds = 60
```

Use the exact stack-specific base endpoint ending in `/otlp`; Ariadne appends
`/v1/metrics` and `/v1/traces`. Both `Basic YOUR_TOKEN` and Grafana's encoded
`Basic%20YOUR_TOKEN` form are accepted. The authorization value is redacted by
`python -m ariadne config show`. Keep the TOML at the documented `chmod 600`
permissions and never check it into the repository.

Import `docs/grafana/ariadne-observability.json` in Grafana and select the
stack's Prometheus data source. The dashboard covers tokens, caching, usage by
source and model, flexible-usage equivalents, failures, latency, cumulative
thread usage, tool calls, background-job outcomes, and turns where Ariadne's
MCP server was never reached.
Use **Explore → Tempo** for individual traces. Turn spans use OpenTelemetry
GenAI attributes; child tool spans include only the safe tool name and timing.

The detailed Ariadne metrics are:

- `ariadne.codex.turns`, `active_turns`, `threads`, and `usage_reports`
- `input_tokens`, `cached_input_tokens`, `uncached_input_tokens`,
  `cache_write_input_tokens`, `output_tokens`, and `reasoning_tokens`
- `flex_credits_equivalent`, `flex_cost_equivalent_usd`,
  `turn.flex_cost_equivalent_usd`, and `unpriced_usage_reports`
- `turn.duration`, `turn.time_to_first_response`,
  `thread.total_tokens`, and `compactions`
- `tool.calls`, `tool.duration`, `turn.mcp_calls`, and
  `turns_without_mcp_calls`
- `ariadne.background.jobs`

They use the bounded labels `source`, `model`, `reasoning_effort`, and `status`;
tool metrics add `tool`. `source` comes from the turn profile, currently
`telegram`, `mail`, or one of the three `revisit-*` attention profiles. Ariadne
also emits the conventional `gen_ai.client.operation.duration` and
`gen_ai.client.token.usage` histograms.
It never adds prompts, responses, commands, tool arguments/results, thread IDs,
turn IDs, Telegram IDs, or mail IDs to metrics or traces.

Ariadne derives a **gross Codex flexible-usage equivalent** from the exact
input, cached-input, and output token breakdown reported by Codex. The pricing
snapshot dated 2026-08-24 is kept in `src/ariadne/pricing.py`:

| Model | Input credits / 1M | Cached credits / 1M | Output credits / 1M |
| --- | ---: | ---: | ---: |
| GPT-5.6 Sol | 125 | 12.5 | 750 |
| GPT-5.6 Terra | 50 | 5 | 300 |
| GPT-5.6 Luna | 5 | 0.5 | 30 |

The SDK's cached-input count is a subset of its input count, so Ariadne applies
the full rate only to `input - cached input`. Output already includes reasoning
tokens; reasoning is not charged a second time. At 25 credits per USD, the same
calculation is emitted as `flex_cost_equivalent_usd` and as a per-turn
histogram. Models absent from the dated table do not receive a guessed price;
they increment `unpriced_usage_reports` instead.

These values are **not amounts charged to the account**. They represent what
the observed tokens would consume as paid Codex flexible usage at the snapshot
rate. Included Plus/Pro usage is consumed before purchased credits, and Ariadne
cannot determine that allowance or the resulting overage from token events.
Re-check and update the dated rate table when OpenAI changes Codex pricing.

### iCloud Mail loop

Mail is opt-in. Copy `mail-routes.example.yaml` outside the repository, edit it,
configure shared iCloud credentials, and enable its TOML section explicitly:

```toml
[icloud]
username = "YOUR_ICLOUD_ADDRESS"
app_password = "YOUR_APP_SPECIFIC_PASSWORD"

[mail]
enabled = true
routes = "~/.config/ariadne/mail-routes.yaml"
state = "~/.local/state/ariadne/mail.sqlite3"
```

Existing configurations with credentials directly under `[mail]` remain
supported. When `[icloud]` is present, its credentials are shared by Mail and
Calendar and take precedence over those legacy fields.

`state` optionally changes the durable SQLite path. The normal
`python -m ariadne` command
records the current `INBOX` UID as its first-run baseline without processing old
mail. From then on it catches up mail received during downtime, drains jobs
sequentially, and waits with IMAP IDLE. Inspection uses `BODY.PEEK`; rules that
say `move` do not invoke Iris. For `iris_then_move`, an explicit Iris `flag` or
`move_to_*` decision wins; `keep_in_inbox` falls back to the route's configured
folder. Mail turns can keep, flag, or move the current message and may draft, but
never send, email. Each mail turn receives the external routes-file path so Iris
can read it and propose a correction when a route was inappropriate. The
configured routes file can contain personal data and must stay outside Git.

By default, every unmatched message gets an Iris inspection that defaults to
keeping it in `INBOX`. Set `defaults.unmatched_action` to `cheap_triage` to keep
clearly routine unmatched mail in `INBOX` without starting an Iris turn.

Lint the ordered rules against the mailbox without changing any mail:

```bash
uv run python -m ariadne.scripts.mail_route_lint
```

The report includes total and first-match counts per rule, shadowed matches,
pairwise overlaps, and up to five sample subjects per rule.

Mailbox moves use IMAP `MOVE` when available. On iCloud, Ariadne uses its
`UIDPLUS` support to copy, mark deleted, and expunge only the exact source UIDs.

Mail turns default independently to `gpt-5.6-terra` at medium reasoning effort
with live web search available when useful. When Calendar is enabled, mail turns
can also inspect and maintain it as part of handling a life event. Override the
model defaults under `[profiles.mail]`; Telegram's `/settings` choices do not
affect mail.

### One-off future revisits

Iris can schedule one future wake-up from Telegram, mail, or another revisit.
The operation stores a timezone-aware time, a self-contained note to her future
self, and one explicit attention level:

| Attention | Runtime | Intended work |
| --- | --- | --- |
| `light` | Luna low | A predetermined reminder or simple nudge |
| `focused` | Luna high | A bounded check using current mail, Calendar, or knowledge |
| `deep` | Terra medium | Cross-source investigation, research, planning, or ambiguity |

The lightweight runtime check defaults to every 15 seconds and does not start a
model turn unless an item is due. A due item starts a fresh conversation with
the same private capabilities at every attention level. Iris reinspects current
context, does useful reversible work, and either uses `send_telegram_message` or
finishes silently; native output from the background turn is discarded. There
is no recurrence or runtime heuristic that chooses a model.

Revisits are always available. Their operational state defaults to
`~/.local/state/ariadne/revisits.sqlite3`; `[revisits]` can change that path and
the polling interval. Interrupted work is returned to the pending queue on
startup. A model failure is retained visibly as failed rather than retried or
routed to a different model automatically. The `revisit-light`,
`revisit-focused`, and `revisit-deep` profile mappings can be overridden in the
same TOML `[profiles.*]` form as other turn profiles.

To apply only deterministic `move` rules to mail that was already in `INBOX`,
stop Ariadne and preview the separate backfill:

```bash
uv run python -m ariadne.scripts.mail_backfill
uv run python -m ariadne.scripts.mail_backfill --apply
```

The backfill never starts a Codex turn: it skips every `iris` or
`iris_then_move` rule and every unmatched message. Its default mode is read-only
and reports what `--apply` would move.
Backfill and mail export show progress when run in an interactive terminal while
keeping redirected output clean. Applied backfills group each fetched batch by
destination and move matching messages in bounded bulk operations.

If a bad rule filed messages into the wrong folder, preview moving that entire
folder back to `INBOX`, then apply it after checking the count:

```bash
uv run python -m ariadne.scripts.mail_backfill --restore-folder Receipts
uv run python -m ariadne.scripts.mail_backfill --restore-folder Receipts --apply
```

Repeat `--restore-folder` to restore multiple folders. This restores every
message in each named folder because earlier backfill runs did not record which
messages they moved. Restores also use bounded bulk operations.

### iCloud Calendar

Calendar access is opt-in and uses the same iCloud app-specific password as
Mail. Enable it independently and set the IANA timezone used for date-only and
otherwise offset-free values:

```toml
[icloud]
username = "YOUR_ICLOUD_ADDRESS"
app_password = "YOUR_APP_SPECIFIC_PASSWORD"

[calendar]
enabled = true
timezone = "Europe/London"
default_calendar = "Calendar"
```

`default_calendar` is optional. With one event calendar, Ariadne selects it
automatically. With several calendars and no configured default, Iris lists
them and supplies the returned opaque calendar id when creating an event.

Telegram turns can list calendars; search and read a bounded date range; merge
busy intervals; create, update, and delete events; and respond to invitations.
Events support all-day or timed intervals, descriptions, locations, attendees,
relative display alarms, status and free/busy transparency, and RFC 5545
`RRULE` recurrence. A per-event IANA timezone can override the configured
default. Search expands recurring events into occurrences. An
occurrence id updates or deletes only that occurrence by default; pass the
series scope to affect the complete recurring event. Search and read results
include an ETag that can be supplied to a mutation to reject a stale decision.

Creating or changing attendees can cause iCloud to send invitations or event
updates, and invitation responses communicate with the organizer. Calendar
writes and deletes are immediate. Calendar data is treated as untrusted content
and cannot itself authorize another action.

This first Calendar integration is deliberately on-demand. It keeps no local
calendar copy, does not poll for changes, and does not generate reminders or
background Telegram notifications. iCloud Reminders are also outside its
scope. Restart Ariadne after changing the Calendar configuration.

## Prompts

Every model-facing prompt source is collected under `src/ariadne/prompts`:

- `base.md` replaces Codex's built-in coding-agent base instructions.
- `telegram.md`, `mail.md`, and `revisit.md` add only surface-specific delivery
  and trigger behavior.
- `grounding.md` distinguishes direct messages, Ariadne activations, and
  external evidence.
- `companion.md` is the shared developer layer for initiative, memory,
  follow-through, communication, and future wake-ups.
- `activations.py` builds typed user-level inputs for Telegram replies, mail,
  and scheduled wake-ups. `assembly.py` composes instruction layers and
  generated knowledge orientation. `inspection.py` renders the exact result.
- The configured `personality.md` adds the actual Iris/Divy-specific voice,
  relationship, and standing preferences. Current knowledge vocabulary is
  generated from the private repository and appended separately.

Documents may use `{{ placeholder }}` fields, filled by `render()`. Only
`{{ human }}` exists today, from `human_name`. Keep the set small: these
documents are built when a profile is resolved, so anything that must stay
current belongs in the turn rather than the prompt.

Write them as flowing paragraphs, one line each — hard-wrapped prompt text
teaches the model to hard-wrap its replies, and those newlines survive all the
way to Telegram.

`docs/research/codex-base-instructions.md` records what Codex's own base prompt
contains, what was kept, and what was dropped.

## Local capabilities

Ariadne exposes clearly named local MCP capabilities to Codex: semantic private
knowledge, `read_recent_telegram_messages`,
`ask_telegram_question`, and `request_telegram_file_delivery`, plus mail,
Calendar, and future wake-up operations. Background profiles such as mail can
also send proactive Telegram notifications; ordinary Telegram turns speak
through native Codex phases.
Requested files are not sent immediately: Ariadne sends a short-lived Telegram
approval card that lists the exact files and has Approve and Reject buttons.

## Speaking

While a Telegram turn runs, Ariadne streams concise reasoning summaries into a
temporary Rich Message with a native Stop button. A native Codex commentary
phase replaces that temporary text and settles as its own permanent bubble; a
new temporary bubble then carries the next work phase. The final phase settles
the last bubble. Casual turns normally use only that final bubble.

`ask_telegram_question` adds a separate native choice card and waits inside the
same model turn; a button tap or ordinary typed answer resumes it. Mail and
future background profiles retain `send_telegram_message` as an intentional
notification action, but it is not exposed to Telegram-triggered turns. Rich
Messages are required: delivery failures are reported rather than silently
changing the response to classic text or keyboards.

Every ordinary bubble is top-level in the current Telegram topic. Telegram's
Reply action still supplies quoted context to the model, but Iris does not
automatically attach her response to the replied-to message.
