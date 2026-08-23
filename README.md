# Ariadne

[![CI](https://github.com/TGDivy/ariadne/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/TGDivy/ariadne/actions/workflows/ci.yml?query=branch%3Amain)

Ariadne is the system that runs Iris on your own machine and connects her to you
over a private Telegram chat. She follows The Thread wherever it leads, not an
assistant you own.

Current status: **Milestone 2 — The Thread foundation**.

## Run

1. Create a Telegram bot with BotFather and note its token.
2. Clone your private The Thread vault locally:

   ```bash
   git clone https://github.com/TGDivy/ariadne-thread.git ~/ariadne-thread
   ```

3. Copy the example configuration and fill in your values:

   ```bash
   cp .env.example .env
   $EDITOR .env
   ```

   `ARIADNE_HUMAN_NAME` is the name Iris calls you by; it is substituted into
   her instructions. `ARIADNE_VAULT` must point to that local Git clone.
   It is Codex's working directory. Iris can read anywhere, write anywhere
   under your home directory, and reach only the domains in `NETWORK_DOMAINS`
   in `src/ariadne/codex/profile.py`. The Telegram Codex model, reasoning
   effort, and web-research setting are also applied explicitly from this
   file.

4. Ensure Codex is already authenticated on this machine.
5. Optionally, give the bot its name, descriptions, and profile photo — set
   any of `ARIADNE_BOT_NAME`, `ARIADNE_BOT_DESCRIPTION`,
   `ARIADNE_BOT_SHORT_DESCRIPTION`, and `ARIADNE_BOT_PROFILE_PHOTO` in `.env`
   and run once:

   ```bash
   uv run --env-file .env python -m ariadne.scripts.bot_profile
   ```

   This talks to Telegram directly and changes the bot itself, not the running
   process; it only needs to be run again when one of these should change.
6. Run:

   ```bash
   uv run --env-file .env python -m ariadne
   ```

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
uv run --env-file .env python -m ariadne.scripts.profile telegram
uv run --env-file .env python -m ariadne.scripts.profile mail
```

Add `--json` for machine-readable output. Inspection never prints environment
values. The declarative source profiles are exported as `TELEGRAM_PROFILE` from
`ariadne.telegram` and `MAIL_PROFILE` from `ariadne.mail`; runtime trigger
policy such as polling, queues, retries, and UID state remains in each surface's
runtime code.

### Read-only mail export experiment

The one-off operator script can export recent iCloud Mail messages for local
analysis. Add `ICLOUD_USERNAME` and `ICLOUD_APP_PASSWORD` to the ignored `.env`
file, then run:

```bash
uv run --env-file .env python -m ariadne.scripts.mail_export \
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
and set all three values below in the ignored `.env` file:

```dotenv
ICLOUD_USERNAME=you@icloud.com
ICLOUD_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
ARIADNE_MAIL_ROUTES=~/.config/ariadne/mail-routes.yaml
```

`ARIADNE_MAIL_STATE` optionally changes the durable SQLite path (the default is
`~/.local/state/ariadne/mail.sqlite3`). The normal `python -m ariadne` command
records the current `INBOX` UID as its first-run baseline without processing old
mail. From then on it catches up mail received during downtime, drains jobs
sequentially, and waits with IMAP IDLE. Inspection uses `BODY.PEEK`; rules that
say `move` do not invoke Iris. Mail turns can keep, flag, or move the current
message and may draft, but never send, email. The configured routes file can
contain personal data and must stay outside Git.

Mail turns default independently to `gpt-5.6-luna` at medium reasoning effort
with web search disabled. Override those defaults with `ARIADNE_MAIL_MODEL`,
`ARIADNE_MAIL_REASONING_EFFORT`, and `ARIADNE_MAIL_WEB_SEARCH`; Telegram's
`/settings` choices do not affect mail.

To apply only deterministic `move` rules to mail that was already in `INBOX`,
stop Ariadne and preview the separate backfill:

```bash
uv run --env-file .env python -m ariadne.scripts.mail_backfill
uv run --env-file .env python -m ariadne.scripts.mail_backfill --apply
```

The backfill never starts a Codex turn: it skips every `iris` rule and unmatched
message. Its default mode is read-only and reports what `--apply` would move.

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
`{{ human }}` exists today, from `ARIADNE_HUMAN_NAME`. Keep the set small: these
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
