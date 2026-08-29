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
prompt builder, so it also makes prompt drift inspectable. Ordinary tests verify
that every scenario remains valid and that the recorded fake capabilities keep
the same names, descriptions, and schemas as their production counterparts.
These checks do not initialize Codex, need credentials or network access, or
incur model usage, and are safe to run in CI.

A real run is always an explicit local action:

```bash
uv run python -m ariadne.scripts.behavior run race-confirmation \
  --output /tmp/race-confirmation.md
```

It uses the mail model, reasoning effort, web-search setting, personality, and
instruction layers from the selected Ariadne config. It therefore needs the
same local Codex authentication as Ariadne and may incur usage. It is not called
by the test suite or CI.

Each run creates a disposable Git-backed Thread containing only synthetic
fixtures. Telegram delivery, file delivery, and mail triage are replaced with
harmless capabilities that preserve the production tool contracts and record
their calls. Real Telegram, mail, calendar, and Thread credentials are not
passed to the scenario MCP process. The Codex workspace is writable only inside
the disposable scenario directory and shell network domains are empty.

Native Codex web search is different: it is never simulated. If the mail
profile has `web_search = "live"`, the run uses the real locally available web
search capability; if it is disabled, the model is told it is disabled. A
missing live capability is reported as a run failure rather than silently
substituted.

The report captures model-visible commentary/final messages, recorded
capability calls, commits, a full text patch of workspace changes, and a short
set of questions for manual review. It deliberately does not expose hidden
reasoning or declare a scenario passed because a sentence happened to match.

The initial stories are the two halves of the Windsor example:

- `race-confirmation`: recognise that a booking is a commitment with open
  preparation loops;
- `train-confirmation`: connect transport to the existing race, preserve the
  flexible return, and notice the tight arrival window.

These first runs should make current capability gaps visible. In particular,
the production mail profile still lacks calendar and knowledge capabilities;
the lab does not pretend otherwise.
