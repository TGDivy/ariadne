# Ariadne

[![CI](https://github.com/TGDivy/ariadne/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/TGDivy/ariadne/actions/workflows/ci.yml?query=branch%3Amain)

Ariadne is a persistent personal AI partner.

Current status: **Milestone 2 — The Thread foundation**.

## GitHub learning webhook

Run `uv run --env-file .env ariadne-webhook`. Configure GitHub to POST `push`,
`pull_request`, and `workflow_run` events to
`https://YOUR_PUBLIC_HOST/github/webhook`, using the configured webhook secret.
Each accepted delivery starts an independent Codex turn against The Thread.
A public HTTPS endpoint is required before GitHub can deliver real events.

## GitHub learning webhook foundation

The repository includes pure signature verification and allow-list parsing for
future GitHub learning webhooks. It does not expose an HTTP endpoint, connect a
GitHub App, receive events, create assignments, or send proactive Telegram
messages. Those operational capabilities require an explicit, separately
reviewed deployment and opt-in policy.

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

   `ARIADNE_VAULT` must point to that local Git clone. It is Codex's working
   directory and workspace-write sandbox. The Codex model, reasoning effort,
   and web-research setting are also applied explicitly from this file.

4. Ensure Codex is already authenticated on this machine.
5. Run:

   ```bash
   uv run --env-file .env python -m ariadne
   ```

On a new session, Ariadne reads `Ariadne/Identity.md`, `Ariadne/Mission.md`,
and `Ariadne/OperatingRules.md` from The Thread when they exist. Send `/new`
to start a fresh Codex conversation while retaining the vault. Create those
files in the vault to define Ariadne's identity, mission, and operating rules.

Use `/settings` to choose an available model, supported reasoning effort, and
live web research for the running process. Each change starts a new in-memory
Codex conversation. Use `/stop` to ask Codex to interrupt the active turn; it
cannot undo work that already completed.
