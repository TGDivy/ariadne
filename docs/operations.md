# Operations reference

This is the operator-facing reference for optional services and routine inspection. Keep credentials, personal routes, and exported data outside the source checkout.

## Inspect the active turn profiles

Telegram, Mail, and each revisit attention level have independent turn profiles. Inspect the exact model, prompts, tool set, thread behaviour, permissions, and forwarded environment-variable names for a surface with:

```bash
uv run python -m ariadne.scripts.profile telegram
uv run python -m ariadne.scripts.profile mail
uv run python -m ariadne.scripts.profile revisit-focused
```

Add `--json` for machine-readable output. Inspection never prints environment values. The declarative profile definitions are in `src/ariadne/profile.py`; polling, queues, retries, and state live in the relevant runtime modules.

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

Mail is opt-in. Copy the route example to a private location, configure shared iCloud credentials, and enable the mail section:

```bash
cp mail-routes.example.yaml ~/.config/ariadne/mail-routes.yaml
chmod 600 ~/.config/ariadne/mail-routes.yaml
```

```toml
[icloud]
username = "YOUR_ICLOUD_ADDRESS"
app_password = "YOUR_APP_SPECIFIC_PASSWORD"

[mail]
enabled = true
routes = "~/.config/ariadne/mail-routes.yaml"
state = "~/.local/state/ariadne/mail.sqlite3"
```

Existing configurations with credentials directly under `[mail]` remain supported. When `[icloud]` is present, its credentials are shared by Mail and Calendar and take precedence over those legacy fields.

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

Restoring a folder moves every message in that named folder, because older backfills do not retain a per-message move history.

### Read-only export experiment

Export recent iCloud Mail messages for local analysis:

```bash
uv run python -m ariadne.scripts.mail_export \
  --limit 1000 --output mail-export.jsonl
```

The export uses a read-only mailbox selection and `BODY.PEEK` fetches. It does not send, move, delete, or mark messages read. It can contain sensitive message text and metadata; treat the output as private.

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

Calendar supports bounded event search, availability, creation, updates, deletion, and invitation responses. Search and read results include an ETag that can be supplied to a mutation to reject stale decisions. Calendar writes and attendee changes can cause provider updates or invitations, so enable it only when that scope is intended.

## One-off revisits

The revisit loop is available independently of Mail and Calendar. Its local state defaults to `~/.local/state/ariadne/revisits.sqlite3`; `[revisits]` can change the state path and polling interval.

Each wake-up has a timezone-aware due time, a self-contained reason, and one attention level:

| Attention | Intended work |
| --- | --- |
| `light` | A predetermined reminder or small nudge. |
| `focused` | A bounded check using current Mail, Calendar, or knowledge. |
| `deep` | Cross-source investigation, research, planning, or meaningful ambiguity. |

The runtime does not start a model turn unless an item is due. A due item starts fresh, re-checks present context, and either sends a warranted message or completes silently. There is no recurrence or heuristic escalation.

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

Use the provider-specific base endpoint ending in `/otlp`; Ariadne appends `/v1/metrics` and `/v1/traces`. The authorization value is redacted by `python -m ariadne config show`. The included Grafana dashboard is `docs/grafana/ariadne-observability.json`.

Metrics and traces use bounded operational labels such as source, model, reasoning effort, status, tool name, and timing. They do not include prompts, responses, commands, tool arguments/results, Telegram identifiers, mail identifiers, or credentials.
