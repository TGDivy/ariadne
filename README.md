# Ariadne

[![CI](https://github.com/TGDivy/ariadne/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/TGDivy/ariadne/actions/workflows/ci.yml?query=branch%3Amain)

Ariadne is a persistent personal AI partner.

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

   `ARIADNE_VAULT` must point to that local Git clone. It is Codex's working
   directory and workspace-write sandbox.

4. Ensure Codex is already authenticated on this machine.
5. Run:

   ```bash
   uv run --env-file .env python -m ariadne
   ```

On a new session, Ariadne reads `Ariadne/Identity.md`, `Ariadne/Mission.md`,
and `Ariadne/OperatingRules.md` from The Thread when they exist. Send `/new`
to start a fresh Codex conversation while retaining the vault. Create those
files in the vault to define Ariadne's identity, mission, and operating rules.
