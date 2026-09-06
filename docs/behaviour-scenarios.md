# Behaviour scenarios

The scenario lab is a small, repository-owned way to replay important stories
while prompts and capabilities change. It is a smoke test for judgement, not a
hosted evaluation platform and not an exact-text test suite.

Two commands are free and deterministic:

```bash
uv run python -m ariadne.scripts.behavior list
uv run python -m ariadne.scripts.behavior show race-confirmation
```

They list or render checked-in synthetic inputs. `show` uses an unchanged direct
Telegram message or the production mail/revisit activation builder, so it also
makes prompt drift inspectable. Ordinary tests verify that every scenario
remains valid, that the fake MCP server retains the first-class production tool
contracts, and that fake Mail and Calendar use the production CLI parser.
These checks do not initialize Codex, need credentials or network access, or
incur model usage, and are safe to run in CI.

A real run is always an explicit local action:

```bash
uv run python -m ariadne.scripts.behavior run race-confirmation \
  --output /tmp/race-confirmation.md
```

Add `--effort low`, `--effort medium`, or `--effort high` to compare reasoning
levels for a manual run without changing production configuration. Use
`--model` when deliberately comparing another locally available model.

It uses the scenario's Telegram, mail, or attention-selected revisit profile,
web-search setting, and instruction layers from the repository defaults. It
needs local Codex authentication and may incur usage, but it does not need an
Ariadne config or service credentials. It is not called by the test suite or
CI.

Pass `--personality path/to/personality.md` to include a personality without
loading any service configuration. Pass `--config path/to/config.toml` when an
exact reproduction of a deployed profile is wanted; in that case its human
name, personality, model, reasoning effort, and web-search setting are used, but
its service credentials still are not forwarded to the run.

Each run creates a disposable Git-backed Thread containing only synthetic
fixtures. Telegram delivery, file delivery, mail triage, semantic knowledge,
and future revisits use a harmless fake MCP server. Mail reads and Calendar use
a temporary `ariadne` executable on `PATH`; it invokes the same nested parser as
production with harmless in-memory/file-backed clients. Both paths record their
calls. The knowledge and Calendar substitutes start with the scenario's
synthetic records and write only temporary state. The report renders the
resulting calendar after the turn so several creates or updates can be reviewed
as one outcome. Real Telegram, Mail, Calendar, and Thread credentials are not
passed to either fake surface. The Codex workspace is writable only inside the
disposable scenario directory and shell network domains are empty.

The fake MCP capabilities are explicitly annotated as closed-world and
harmless. Fake CLI mutations affect only the disposable Calendar fixture. This
keeps a missing local reviewer or real provider from being confused with Iris
choosing not to use a capability.

Native Codex web search is different: it is never simulated. If the mail
profile has `web_search = "live"`, the run uses the real locally available web
search capability; if it is disabled, the model is told it is disabled. A
missing live capability is reported as a run failure rather than silently
substituted.

The command streams safe activity labels, model-visible speech, and MCP call
boundaries while the model works. The report retains the activity/speech
timeline, elapsed time, Codex's reported token usage, exact recorded MCP and CLI
client calls, commits, a full text patch of workspace changes, and a short set
of questions for manual review. It deliberately does not expose hidden
reasoning or declare a scenario passed because a sentence happened to match.

The initial stories cover the Windsor event from arrival to a later revisit and
two ordinary conversational moments:

- `race-confirmation`: recognise that a booking is a commitment with open
  preparation loops;
- `train-confirmation`: connect transport to the existing race, preserve the
  flexible return, and notice the tight arrival window;
- `race-evening-revisit`: wake once, reassess what has changed, finish useful
  preparation, and message only if something still matters.
- `resolved-before-wakeup`: reconcile an older wake-up note with Divy's newer
  Telegram message and avoid a reminder for work he already completed.
- `conflicting-needs`: use Divy's own context without flattening ambition and
  the need for rest into one permanent rule;
- `known-person-news`: retrieve Lily's context, share the human moment, and
  update durable knowledge without narrating it.
- `tentative-ambition`: retain a possible ambition under Divy's wishes without
  silently promoting it into an active goal;
- `new-person-day`: create a useful new person while keeping facts about her in
  the person record and Divy's lived experience in the journal.

These first runs should make current capability gaps visible. The scenario uses
the same five semantic knowledge operations and concise generated current
context as the production mail profile, with harmless temporary implementations
behind those contracts.
