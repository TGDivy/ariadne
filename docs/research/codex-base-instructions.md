# Replacing Codex's base instructions

Codex CLI 0.147.0, `openai-codex` SDK 0.147.0.

Codex ships ~4,400 tokens of base instructions written for a coding agent in a
terminal. Iris is not one, so we replace them. See
`src/ariadne/instructions/base.md` and `telegram.md`.

## Where they come from

Served by OpenAI per model, cached at
`~/.codex/models.json → models[].model_messages.instructions_template`. Not in
the open-source prompt files. To read the one a model actually receives:

```bash
python3 -c "import json,os;d=json.load(open(os.path.expanduser('~/.codex/models.json')));print([m for m in d['models'] if m['slug']=='gpt-5.6-luna'][0]['model_messages']['instructions_template'])"
```

The `base_instructions.text` in a session rollout is byte-identical to that
template, so it is exactly what goes on the wire. The three `gpt-5.6-*` models
share one; `gpt-5.5` has a different, longer one.

## An override replaces the instructions field and nothing else

`codex debug prompt-input` renders the model-visible input list identically with
and without `-c base_instructions=…`. Codex still injects, on its own:

- `<permissions instructions>` (~970 tokens) — sandbox mode, escalation via
  `require_escalated` / `justification` / `prefix_rule`, and when to escalate
- `<environment_context>` — cwd, shell, date, timezone, workspace roots
- skills, apps and plugin blocks when enabled, plus all tool schemas

**So sandbox and escalation mechanics stay out of our replacement.** They are
injected separately and would only be duplicated. That is what makes replacing
the rest safe.

## Kept

Load-bearing, because Ariadne's own code depends on it:

- The `commentary`/`final` channel contract. `stream_turn` preserves each
  declared phase as a semantic event, while Telegram renders commentary and
  final items as separate messages.
- `apply_patch` for edits — the 5.6 models are `apply_patch_tool_type: freeform`.
- CommonMark's blank line before a list, or `telegram_format` will not render it.

Kept because it is good: steering, compaction, the autonomy taxonomy
(answer / diagnose / change, bias to action when read-only, don't infer
authority for a different action, lead with evidence rather than folding),
dirty-worktree and git safety, destructive actions near-verbatim, and shell
hygiene.

## Dropped

- `# Using skills`, ~1,180 tokens and 27% of the prompt. Disabled on the machine
  Ariadne runs on, and injected separately as `<skills_instructions>` when
  enabled, so redundant either way.
- `### Formatting rules`. It asks for local files as `[app.py](/abs/path.py:12)`;
  `telegram_format` only anchors http(s), so the target is dropped and
  `See [app.py](/abs/path/app.py:12)` renders as `See app.py`. Silent data loss.
  Replaced by `telegram.md`.
- `### Visualizations`. Recommends tables; our parser has no tables rule, so they
  arrive as raw pipes.
- The coding-agent framing.

4,432 tokens becomes 1,431. Worth noting the saving is small in practice: the
prefix is paid once per thread and cached at roughly a tenth of the price after
that. The point was coherence, not cost.

## Residual risk

The template is per model. Only `gpt-5.6-*` is in use and they share one, so this
is inert today; it would matter if `/settings` brought `gpt-5.5` back. If OpenAI
changes a tool contract, our frozen prompt will not learn about it — re-diff
against the manifest after notable Codex upgrades.
