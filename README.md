# Ariadne

Ariadne is a persistent personal AI partner.

## Run

1. Create a Telegram bot with BotFather and note its token.
2. Copy the example configuration and fill in your values:

   ```bash
   cp .env.example .env
   $EDITOR .env
   ```

   `ARIADNE_WORKSPACE` is where Codex works: its current directory and
   workspace-write sandbox. The example uses `.`, meaning this Ariadne checkout
   when you run the command from the repository root. Use an absolute path there
   to work in a different project.

3. Ensure Codex is already authenticated on this machine.
4. Run:

   ```bash
   uv run --env-file .env python -m ariadne
   ```
