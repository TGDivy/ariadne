# Operations reference

This is the operator-facing reference for optional services and routine inspection. Keep credentials, personal routes, and exported data outside the source checkout.

## Unified CLI contract

`ariadne` is both the service entry point and the bounded data-access surface available to Iris. Its command tree is intentionally discoverable on demand instead of placing every Mail, Calendar, and health schema in every model turn:

```bash
uv run ariadne --help
uv run ariadne mail --help
uv run ariadne calendar update --help
uv run ariadne health --help
```

The usual production service command is `ariadne serve`; `uv run ariadne serve` is convenient from a source checkout. Global options precede the command, for example `ariadne --config /private/config.toml --pretty calendar list`. Help and argument validation do not load configuration or contact a provider.

Successful non-service commands write exactly one bounded JSON document to stdout. Compact one-line JSON is the default; `--pretty` only changes whitespace. A failed non-service command leaves stdout empty and writes one object of this shape to stderr. `serve` is long-running and retains human-readable operational logging instead:

```json
{"error":{"code":"invalid_arguments","message":"...","retryable":false}}
```

Exit status `0` means success. Status `1` is an unexpected internal or response-contract failure, `2` is invalid arguments, configuration, or a domain request, `3` is failed authentication, `4` is not found, `5` is a stale Calendar conflict, and `6` is a retryable provider or network failure. There are no automatic retries for writes. Results are capped at 2 MiB and individual search commands impose smaller item limits; narrow the query when `output_too_large` is returned.

Provider credentials are read from the private TOML file only after the corresponding command is selected. They are never command arguments, output fields, or MCP environment variables. Provider and validation errors pass through shared secret redaction. Codex telemetry records command execution as the bounded `shell` tool category without exporting the command, arguments, or result.

## Inspect the active turn profiles

Telegram, Mail, proactive stewardship, and each revisit attention level have independent turn profiles. Inspect the exact model, prompts, tool set, thread behaviour, permissions, and forwarded environment-variable names for a surface with:

```bash
uv run python -m ariadne.scripts.profile telegram
uv run python -m ariadne.scripts.profile mail
uv run python -m ariadne.scripts.profile stewardship
uv run python -m ariadne.scripts.profile revisit-focused
```

Add `--json` for machine-readable output. Inspection never prints environment values. The declarative profile definitions are in `src/ariadne/profile.py`; polling, queues, retries, and state live in the relevant runtime modules.

### Telegram conversation continuity

The Telegram profile resumes its active Codex thread across normal service restarts. Ariadne stores the versioned opaque thread identifier in the configured `[telegram].state` SQLite file; it is private runtime state and should follow the same protected backup policy as that database. Clean shutdown does not clear it. `/new` and model, effort, or web-research changes intentionally clear it before starting a fresh conversation, without deleting Telegram history or the Thread knowledge repository.

If the remote thread no longer exists or its saved state is incompatible, Ariadne invalidates only that reference, starts fresh, and tells the owner once. Logs contain the bounded failure class, never the saved identifier. Do not edit or transplant the row manually to resume an arbitrary Codex thread.

### Conversational background handoffs

Mail, revisit, and stewardship profiles do not have `send_telegram_message` or Telegram file-delivery authority. They may instead stage one free-form `hand_off_to_telegram_conversation` result under their runtime-supplied activation key. The originating loop releases that row only after its own operation succeeds; failure or cancellation discards it. Repeating the capability before release replaces the same activation's staged body.

The handoff table is created additively in `[telegram].state`; there is no configuration migration or broker to operate. On startup, interrupted claims return to ready state. When a human message arrives, a bounded FIFO batch joins that next shared turn. With no message, the coordinator waits for two minutes without human or Iris conversational activity, then runs the same Telegram conversation without creating a fake incoming message or a premature thinking placeholder. A failed shared turn retains the batch for retry, and successful visible output is written to normal Telegram history.

Operational logs expose only lifecycle identifiers, counts, statuses, and timings. To validate a deployment, trigger a harmless background handoff, confirm it stays invisible until its source job completes, then check both paths: send a human message before two minutes and later allow another handoff to cross the quiet window. Restart once with a ready handoff and once during a claimed test turn to verify conservative recovery. Do not inspect the private SQLite body in shared logs or a PR.

## Browser control

Browser control is a separate long-lived, opt-in service. Its MCP process receives
only the private Unix-socket path; Chromium profiles, artifacts, and credentials do
not enter model configuration. The administrative CLI is deliberately distinct from
the model-facing tools:

```bash
ariadne-browser health
ariadne-browser profile list
ariadne-browser profile inspect personal
ariadne-browser journal --limit 50
ariadne-browser recovery list
ariadne-browser shutdown
```

Run `ariadne-browser serve` under the owner's service manager and create the first
profile with `ariadne-browser profile create personal`. Full setup, takeover, backup,
recovery, and conservative live validation are in
[Browser control](browser-control.md).

## Grocery ordering

Grocery ordering rides on the browser service and adds one private SQLite database
of order drafts, approvals, and checkout outcomes. There is no operator CLI: drafts
are read and repaired conversationally, because every consequential step needs the
owner in the loop anyway.

Two switches matter. `[grocery].enabled` turns on basket building, which is
reversible. `[grocery].checkout_enabled` turns on real spending and should stay
`false` until the documented dry runs pass. A checkout interrupted by a restart is
recovered as **uncertain** on the next start and logged for owner verification; it is
never retried automatically. Setup, the order lifecycle, failure handling, and the
exact home-server smoke tests are in [Grocery ordering](grocery-shopping.md).

## Behaviour scenarios

The behaviour lab replays synthetic stories without contacting a real Telegram chat, mailbox, Calendar, or private Thread. Listing and inspection are deterministic and CI-safe; a real run is an explicit local command and may incur model usage.

```bash
uv run python -m ariadne.scripts.behavior list
uv run python -m ariadne.scripts.behavior show race-confirmation
uv run python -m ariadne.scripts.behavior run race-confirmation \
  --output /tmp/race-confirmation.md
```

See [Behaviour scenarios](behaviour-scenarios.md) for its isolation boundary and report contents.

## Mail

Mail is opt-in. Copy the route example to a private location, configure iCloud, personal Outlook.com, or both, and enable the shared mail section:

```bash
cp mail-routes.example.yaml ~/.config/ariadne/mail-routes.yaml
chmod 600 ~/.config/ariadne/mail-routes.yaml
```

```toml
[icloud]
username = "YOUR_ICLOUD_ADDRESS"
app_password = "YOUR_APP_SPECIFIC_PASSWORD"
mail_label = "iCloud"

[mail]
enabled = true
routes = "~/.config/ariadne/mail-routes.yaml"
state = "~/.local/state/ariadne/mail.sqlite3"
```

Existing configurations with credentials directly under `[mail]` remain supported. When `[icloud]` is present, its credentials are shared by Mail and Calendar and take precedence over those legacy fields.

### Personal Outlook.com authorization

Register Ariadne as a public client in Microsoft Entra before enabling Outlook:

1. Create an app registration whose supported account type is **Personal Microsoft accounts only**.
2. Under **Authentication**, enable **Allow public client flows**. Device authorization does not need a client secret or a production redirect URL. In Outlook.com's Mail settings, also allow IMAP access for the mailbox if it has been disabled.
3. Add the delegated Office 365 Exchange Online permission `IMAP.AccessAsUser.All`. Do not add `Mail.Send`; Ariadne requests only IMAP read/write access and offline refresh.
4. Copy the application (client) ID into the private configuration. A client ID is not a credential, but the address and cache path still belong in the private file.

```toml
[outlook]
enabled = true
address = "YOUR_PERSONAL_OUTLOOK_ADDRESS"
client_id = "YOUR_MICROSOFT_PUBLIC_CLIENT_ID"
token_cache = "~/.local/state/ariadne/outlook-token.json"
label = "Personal Outlook"

[mail]
enabled = true
routes = "~/.config/ariadne/mail-routes.yaml"
state = "~/.local/state/ariadne/mail.sqlite3"
```

Stop the service for the initial authorization and run:

```bash
ariadne mail authorize-outlook
```

The command prints Microsoft's verification URL and short code, then polls on the Ariadne host. Open the URL on any phone or computer, sign into the configured personal Outlook.com account, and enter the code. The resulting refreshable cache is written atomically on the Ariadne host with owner-only permissions; it is never printed, placed in TOML, or passed to the model. Start the service again afterward. Access tokens refresh automatically across restarts.

If consent expires or is revoked, rerun the same command. To force a clean reauthorization, stop Ariadne, delete only the configured `token_cache`, and rerun it. To revoke access, remove Ariadne from the Microsoft account's consent/applications page and delete the local cache. To remove the account, also set `[outlook].enabled = false`; retained account-qualified job history is harmless and prevents old work from being reassigned to another mailbox.

### Mail state upgrade

The first start of this version migrates the configured Mail SQLite database in one transaction. Existing jobs, decisions, retry counts, and checkpoints are assigned to the stable `icloud` account; new uniqueness is account plus mailbox, UIDVALIDITY, and UID. Stop the old Ariadne process before deploying so it cannot write through the migration. A private backup of the SQLite database (and any `-wal`/`-shm` files) is prudent before the first start. Rolling the binary back afterward requires restoring that backup because the old runtime does not understand account-qualified state.

### Read-only agent commands

Mail search, message reads, and thread reads use the unified CLI:

```bash
ariadne mail search "train confirmation" --since 2026-08-01 --limit 10
ariadne mail search "train confirmation" --account outlook --limit 10
ariadne mail read 'mail:OPAQUE_ID'
ariadne mail thread 'mail:OPAQUE_ID'
```

Search queries every enabled account by default, merges and ranks a global bounded result set, and returns each friendly account label and stable key. `--account` limits the search when needed. The response lists searched and failed accounts and sets `partial=true` if one provider was unavailable; it never implies a partial result is complete. Account-qualified opaque IDs route `read` and `thread` back to only their source mailbox, and legacy IDs continue to resolve to iCloud. These operations use read-only mailbox selection and `BODY.PEEK`; they do not mark, move, delete, or send mail. The MCP surface retains only `record_current_mail_decision`, because that mutation is meaningful solely inside the currently claimed ingestion job. Ordinary reads live in the CLI so their less frequently used schemas are loaded through `--help` only when needed.

Routes are ordered and first-match-wins. A `move` rule does not invoke the agent. For `iris_then_move`, an explicit agent decision to flag or move elsewhere wins; `keep_in_inbox` falls back to the route’s configured folder. By default, unmatched mail is inspected and kept in `INBOX`; set `defaults.unmatched_action` to `cheap_triage` to retain clearly routine unmatched mail without starting an agent turn.

### Safe maintenance commands

Lint the configured rules against the mailbox without mutations:

```bash
uv run python -m ariadne.scripts.mail_route_lint
```

Preview deterministic moves for mail already in `INBOX`, then apply only after reviewing the result:

```bash
uv run python -m ariadne.scripts.mail_backfill
uv run python -m ariadne.scripts.mail_backfill --apply
```

If a prior backfill routed an entire folder incorrectly, preview its restoration before applying it:

```bash
uv run python -m ariadne.scripts.mail_backfill --restore-folder Receipts
uv run python -m ariadne.scripts.mail_backfill --restore-folder Receipts --apply
```

When both providers are enabled these single-mailbox operator tools require `--account icloud` or `--account outlook`; this is mandatory for applying bulk mutations and also keeps previews unambiguous. Both accounts use the same route file. Restoring a folder moves every message in that named folder, because older backfills do not retain a per-message move history.

### Read-only export experiment

Export recent messages from one account for local analysis:

```bash
uv run python -m ariadne.scripts.mail_export \
  --account outlook --limit 1000 --output mail-export.jsonl
```

Every exported record contains its account key and label. The export uses a read-only mailbox selection and `BODY.PEEK` fetches. It does not send, move, delete, or mark messages read. It can contain sensitive message text and metadata; treat the output as private.

### Outlook.com live validation checklist

The automated suite uses fake OAuth and IMAP responses and therefore cannot establish provider compatibility. Before treating Outlook support as operational, validate on the private Linux home server without recording addresses, tokens, codes, or message contents:

1. Run `ariadne mail authorize-outlook`, completing the browser step on a separate phone or computer; confirm the cache is mode `0600`.
2. Restart Ariadne after the first access token expires (or use a deliberately expired fixture cache in a private staging run) and confirm automatic refresh plus successful IMAP authentication.
3. With both accounts enabled, run bounded all-account and `--account outlook` searches, then `read` and `thread` one returned Outlook ID. Confirm an Outlook ID cannot be read through iCloud.
4. Deliver a harmless test message, confirm the Outlook IDLE loop creates and completes an account-qualified job, and exercise each allowed current-event action: keep, flag, and move to a test route folder. Confirm no send or provider-draft capability appears.
5. Disconnect Outlook long enough to deliver another test message, reconnect it, and confirm checkpoint catch-up handles the message exactly once.
6. While Outlook is disconnected or unauthorized, confirm iCloud ingestion and direct reads continue. An all-account search must identify Outlook as failed and mark the response partial.
7. Preview route lint, backfill, restore, and export for each explicit account. Apply only to disposable test mail and confirm exported records include the correct account key.

If Outlook IMAP proves an operation unreliable, capture only sanitized protocol facts and reassess that operation before introducing Microsoft Graph; Graph is not part of the initial implementation.

## Calendar

Calendar uses the same iCloud credentials as Mail but must be enabled separately. Set the IANA timezone used to interpret date-only and offset-free values:

```toml
[icloud]
username = "YOUR_ICLOUD_ADDRESS"
app_password = "YOUR_APP_SPECIFIC_PASSWORD"

[calendar]
enabled = true
timezone = "Europe/London"
# default_calendar = "Calendar"
```

`default_calendar` is optional. When there is only one event calendar, Ariadne selects it automatically; otherwise it lists calendars and uses the returned opaque calendar ID for a create operation.

Calendar supports bounded event search, availability, creation, updates, deletion, and invitation responses through the CLI:

```bash
ariadne calendar list
ariadne calendar search --start 2026-09-01 --end 2026-09-08 \
  --query "planning" --limit 25
ariadne calendar read 'calendar-event:OPAQUE_ID'
ariadne calendar availability \
  --start 2026-09-03T09:00:00+01:00 \
  --end 2026-09-03T17:00:00+01:00
```

Calendar writes are immediate and never prompt interactively, which keeps the contract deterministic for a model or script. Inspect each command’s help before constructing it:

```bash
ariadne calendar create --help
ariadne calendar update --help
ariadne calendar delete --help
ariadne calendar respond --help
```

Repeated `--attendee`, `--alarm-minutes-before`, and `--calendar-id` flags follow ordinary CLI conventions. Updates distinguish omitted fields from explicit `--clear-*` flags. Search and read results include an ETag accepted by `--expected-etag` so a mutation can reject stale state. Calendar writes and attendee changes can cause provider updates or invitations, so enable the integration only when that scope is intended.

The model-facing CLI is distinct from the operator maintenance scripts below and in the Mail section. Those scripts may perform bulk work or produce private files and are not advertised to Iris as personal-data commands.

## Health workouts and sleep

Health access is opt-in and read-only. Configure Ithaca's HTTPS API root and separate read token in the private TOML file; use the timezone in which date-only and offset-free CLI bounds should be interpreted:

```toml
[health]
enabled = true
api_url = "https://your-ithaca-host.example"
read_token = "YOUR_DISTINCT_ITHACA_READ_TOKEN"
timezone = "Europe/London"
timeout_seconds = 30
```

The token stays in the private file and is sent only as an authorization header. It is redacted by `ariadne config show`. The configured hostname is added to Codex's network allowlist so the installed CLI can reach a private deployment; the full URL and token are not model instructions or MCP environment values.

List returns compact newest-first workouts and a stable `next_cursor`. Summarize uses the same half-open period and filters:

```bash
ariadne health workouts list \
  --start 2026-08-01 --end 2026-09-01 --activity running --limit 20
ariadne health workouts summarize \
  --start 2026-08-01 --end 2026-09-01 --activity running
```

Date-only and offset-free bounds use `[health].timezone`; explicit ISO offsets are respected. Requests are normalized to UTC for Ithaca. Repeat `--activity` to include more than one activity type. Run each command's `--help` for the complete reviewed activity values.

The CLI validates Ithaca's complete response shape, then removes transport details, snapshot identifiers, null metrics, and the non-actionable detailed-series catalogue before emitting JSON. It retains unit-bearing field names, workout IDs, readable source names, cursors, freshness and quality flags, and `period_data_coverage`; those coverage counts describe the requested period before optional activity filters.

Use a workout ID returned by list to retrieve compact detail. `show` includes selection and projection quality, metrics, splits, heart-rate zones, components, and route availability:

```bash
ariadne health workouts show '00000000-0000-0000-0000-000000000101'
```

Ithaca returns facts and deterministic arithmetic, not coaching or diagnoses. Missing metrics remain absent from CLI output, while explicit availability, projection, and quality warnings remain visible for Iris's interpretation. Ithaca's lower-level detailed-series endpoint is deliberately not exposed to Iris; a purpose-shaped trend command can be added later if real questions justify the extra data and complexity.

Sleep uses derived local dates rather than workout timestamps. A sleep date is the local date on which the episode ended, using the timezone recorded with that episode; `[health].timezone` does not rewrite it. List and summarize use an inclusive start and exclusive end date:

```bash
ariadne health sleep list \
  --start 2026-09-01 --end 2026-09-08 --limit 20
ariadne health sleep summarize \
  --start 2026-09-01 --end 2026-09-08
ariadne health sleep latest
ariadne health sleep show 2026-09-06
```

List and latest return compact daily duration, stage, timing, source, and timezone facts. Latest omits the interval timeline; pass its `sleep_date` to show when ordered stage intervals are useful. Summaries average only days with observed sleep data and report that count explicitly. Missing stage values remain JSON `null`. Ariadne omits Ithaca's sleep projection and issue metadata from model-facing results, and it never exposes raw HealthKit samples or invents a sleep-quality score.

## One-off revisits

The revisit loop is available independently of Mail and Calendar. Its local state defaults to `~/.local/state/ariadne/revisits.sqlite3`; `[revisits]` can change the state path and polling interval.

Each wake-up has a timezone-aware due time, a self-contained reason, and one attention level:

| Attention | Intended work |
| --- | --- |
| `light` | A predetermined reminder or small nudge. |
| `focused` | A bounded check using current Mail, Calendar, or knowledge. |
| `deep` | Cross-source investigation, research, planning, or meaningful ambiguity. |

The runtime does not start a model turn unless an item is due. A due item starts fresh, re-checks present context, and either stages useful context for the shared Telegram conversation or completes silently. There is no recurrence or heuristic escalation.

## Proactive stewardship

Stewardship is a separate opt-in daily opportunity for Iris to reflect across current goals, conversation, Calendar, mail, health, workouts, and private knowledge; imagine several grounded ways to help; complete one safe coherent loop; and learn from the result. It is not a compulsory briefing. Most cycles may remain silent, and anything worth discussing is staged for the continuing Telegram Iris through the same two-minute handoff coordinator.

Start with recurrence disabled and configure the owner's IANA timezone and local waking window:

```toml
[stewardship]
enabled = false
timezone = "Europe/London"
waking_start = "09:00"
waking_end = "21:00"
state = "~/.local/state/ariadne/stewardship.sqlite3"
poll_interval_seconds = 60
```

Run several explicit trials against the private sources before enabling it:

```bash
uv run ariadne-stewardship --config ~/.config/ariadne/config.toml
```

The command forces one owner-requested cycle even while recurrence is disabled and emits one compact JSON result with `completed`, `failed`, or `not-run` status. Review the resulting private changes and any conversational handoff for usefulness, surprise, repetition, action correctness, and interruption quality. It is valid—and important—that a well-grounded cycle sometimes records “nothing worthwhile” and sends nothing.

Only after those reviews, set `enabled = true` and restart the service. The runtime attempts at most one successful cycle for each local date inside the half-open waking window. Missed days are not replayed. A failed attempt waits 30 minutes before retrying; a cancelled or process-interrupted attempt is immediately eligible again. Outcome history and broad-attention hints are bounded operational orientation, not a second knowledge base.

The SQLite state is created additively at the configured path with owner-only permissions. Indefinite and time-bounded pauses persist across service restarts; an expired pause clears atomically. Resuming does not manufacture a missed cycle. Explicit “run now” bypasses the waking window, disabled recurrence, and a pause without changing the stored pause. Status consumers receive the configured enablement, pause/running state, last completion, and next eligible instant in the configured local timezone.

The stewardship profile can use the ordinary discoverable Mail, Calendar, and health CLI plus semantic knowledge and one-off-revisit capabilities. It cannot send Telegram messages/files or email, and it has no mail-ingestion decision authority. Payment, applications, outbound interpersonal communication, consequential accounts, high-stakes appointments, and ambiguous commitments still require owner confirmation. Browser control and direct sending are separate capabilities, not implied by enabling stewardship.

## Telemetry

Telemetry is opt-in under `[telemetry]`; the ordinary runtime does not read or fall back to `OTEL_*` environment variables. For a Grafana Cloud or other OTLP/HTTP endpoint, put the endpoint and authorization in the private configuration:

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

Use the provider-specific base endpoint ending in `/otlp`; Ariadne appends `/v1/metrics` and `/v1/traces`. The authorization value is redacted by `ariadne config show`. The included Grafana dashboard is `docs/grafana/ariadne-observability.json`.

Metrics and traces use bounded operational labels such as source, model, reasoning effort, status, tool name, and timing. They do not include prompts, responses, commands, tool arguments/results, Telegram identifiers, mail identifiers, or credentials.
