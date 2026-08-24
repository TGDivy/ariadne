# Ariadne

Ariadne is the system that runs Iris on your own machine and connects her to you
over a private Telegram chat. She follows The Thread wherever it leads, not an
assistant you own.

Current status: **Milestone 2 — The Thread foundation**.

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
   instructions. `vault` must point to that local Git clone.
   It is Codex's working directory. Iris can read anywhere, write anywhere
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

Nothing from the vault is injected into the prompt. The Thread is Iris's working
directory and she reads it herself. Send `/new` to start a fresh Codex
conversation while retaining the vault.

Use `/settings` to choose an available model, supported reasoning effort, and
live web research for the running process. Each change starts a new in-memory
Codex conversation. Use `/stop` to ask Codex to interrupt the active turn; it
cannot undo work that already completed.

Telegram and mail have independent turn profiles. To inspect the exact model,
prompts, tools, thread behavior, permissions, and forwarded environment variable
names that either surface will use, run:

```bash
uv run python -m ariadne.scripts.profile telegram
uv run python -m ariadne.scripts.profile mail
```

Add `--json` for machine-readable output. Inspection never prints environment
values. Every declarative source profile lives together in
`src/ariadne/profile.py`; runtime trigger policy such as polling, queues,
retries, and UID state remains in each surface's runtime code.

### Read-only mail export experiment

The one-off operator script can export recent iCloud Mail messages for local
analysis. Configure `username` and `app_password` under `[mail]`, then run:

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
turn that is already running, so Codex folds them into the work in flight.

### iCloud Mail loop

Mail is opt-in. Copy `mail-routes.example.yaml` outside the repository, edit it,
and enable its TOML section explicitly:

```toml
[mail]
enabled = true
username = "YOUR_ICLOUD_ADDRESS"
app_password = ""
routes = "~/.config/ariadne/mail-routes.yaml"
state = "~/.local/state/ariadne/mail.sqlite3"
```

`state` optionally changes the durable SQLite path. The normal
`python -m ariadne` command
records the current `INBOX` UID as its first-run baseline without processing old
mail. From then on it catches up mail received during downtime, drains jobs
sequentially, and waits with IMAP IDLE. Inspection uses `BODY.PEEK`; rules that
say `move` do not invoke Iris; `iris_then_move` rules run Iris successfully
before filing into their configured folder. Mail turns can keep, flag, or move
the current message and may draft, but never send, email. Each mail turn receives
the external routes-file path so Iris can read it and propose a correction when
a route was inappropriate. The configured routes file can contain personal data
and must stay outside Git.

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

Mail turns default independently to `gpt-5.6-luna` at medium reasoning effort
with web search disabled. Override those defaults under `[profiles.mail]`;
Telegram's `/settings` choices do not affect mail.

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

## Instructions

Iris's prompt is assembled from Markdown documents owned by the shared Codex
runtime and each conversation surface:

- `src/ariadne/instructions/base.md` replaces Codex's built-in coding-agent base
  instructions.
- `src/ariadne/telegram/instructions.md` and
  `src/ariadne/mail/instructions.md` hold rules specific to those surfaces and
  are appended to the shared base.
- `src/ariadne/instructions/grounding.md` is the developer message: where Iris is
  running and what she can reach.

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

Ariadne exposes local MCP capabilities to Codex: runtime status, speaking in
Telegram, and preparing files from the configured user's home directory.
Prepared files are not sent immediately: Ariadne sends a short-lived Telegram
approval card that lists the exact files and has Approve and Reject buttons.

## Speaking

Two separate things reach the chat. While a turn runs, Ariadne streams Iris's
developing response into a Telegram draft — ephemeral, animated in place, and
gone within thirty seconds of the last update, so it leaves no trail of
intermediate messages. What persists is what Iris chose to send: `send_message`
and `react` put a message or an emoji in the chat the moment she calls them, and
her final response is delivered when the turn ends.

Each incoming message is tagged with its Telegram message id so Iris can reply
or react to it. If her final response repeats something she already sent
herself, Ariadne does not send it twice.
