# Behaviour scenarios

The scenario lab is a small, repository-owned way to replay important stories
while prompts and capabilities change. It is a smoke test for judgement, not a
hosted evaluation platform and not an exact-text test suite.

Two commands are free and deterministic:

```bash
uv run python -m ariadne.scripts.behavior list
uv run python -m ariadne.scripts.behavior show race-confirmation
```

They list or render checked-in synthetic inputs. `show` uses the production mail
or revisit activation builder, so it also makes prompt drift inspectable. Ordinary tests verify
that every scenario remains valid and that the recorded fake capabilities keep
the same names, descriptions, and schemas as their production counterparts.
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

It uses the scenario's mail or attention-selected revisit profile, web-search
setting, and instruction layers from the repository defaults. It needs local Codex authentication and
may incur usage, but it does not need an Ariadne config or service credentials.
It is not called by the test suite or CI.

Pass `--personality path/to/personality.md` to include a personality without
loading any service configuration. Pass `--config path/to/config.toml` when an
exact reproduction of a deployed profile is wanted; in that case its human
name, personality, model, reasoning effort, and web-search setting are used, but
its service credentials still are not forwarded to the run.

Each run creates a disposable Git-backed Thread containing only synthetic
fixtures. Telegram delivery, file delivery, mail access and triage, Calendar,
semantic knowledge, and future revisits are replaced with harmless capabilities that preserve the production
tool contracts and record their calls. The knowledge and Calendar substitutes
start with the scenario's synthetic records and write only temporary state. The
report renders the resulting calendar after the turn so several creates or
updates can be reviewed as one outcome. Real Telegram, mail, Calendar, and
Thread credentials are not passed to the scenario MCP process. The Codex
workspace is writable only inside the disposable scenario directory and shell
network domains are empty.

The fake capabilities are explicitly annotated as closed-world and harmless.
Their only effect is appending to the temporary call record, so they do not need
Codex's external-action reviewer. This keeps a missing local reviewer model from
being confused with Iris choosing not to use a capability.

Native Codex web search is different: it is never simulated. If the mail
profile has `web_search = "live"`, the run uses the real locally available web
search capability; if it is disabled, the model is told it is disabled. A
missing live capability is reported as a run failure rather than silently
substituted.

The command streams safe activity labels, model-visible speech, and MCP call
boundaries while the model works. The report retains the activity/speech
timeline, elapsed time, Codex's reported token usage, exact recorded capability
calls, commits, a full text patch of workspace changes, and a short set of
questions for manual review. It deliberately does not expose hidden reasoning
or declare a scenario passed because a sentence happened to match.

The initial stories cover the Windsor event from arrival to a later revisit:

- `race-confirmation`: recognise that a booking is a commitment with open
  preparation loops;
- `train-confirmation`: connect transport to the existing race, preserve the
  flexible return, and notice the tight arrival window;
- `race-evening-revisit`: wake once, reassess what has changed, finish useful
  preparation, and message only if something still matters.
- `resolved-before-wakeup`: reconcile an older wake-up note with Divy's newer
  Telegram message and avoid a reminder for work he already completed.

These first runs should make current capability gaps visible. The scenario uses
the same six semantic knowledge operations and generated orientation as the
production mail profile, with harmless temporary implementations behind those
contracts.
