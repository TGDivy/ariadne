# Getting started

This guide gets a single private Ariadne installation running without putting personal data or credentials into the repository.

## Before you begin

You will need:

- Python **3.13+** and [uv](https://docs.astral.sh/uv/);
- an authenticated local Codex installation;
- a Telegram bot token from [BotFather](https://t.me/BotFather) and the numeric ID of the one Telegram account that may talk to the bot;
- a private Git-backed *Thread* repository. This is the canonical home for personal knowledge; it is separate from the Ariadne source tree.

The source repository includes safe examples only. Your actual configuration, mail routes, state databases, and Thread should remain private.

## Install

```bash
git clone https://github.com/TGDivy/ariadne.git
cd ariadne
uv sync --locked
```

## Configure a private runtime

Create the configuration outside the repository and restrict its file permissions:

```bash
mkdir -p ~/.config/ariadne
cp config.example.toml ~/.config/ariadne/config.toml
chmod 600 ~/.config/ariadne/config.toml
$EDITOR ~/.config/ariadne/config.toml
```

Set, at minimum:

```toml
human_name = "Your Name"
vault = "~/path/to/your/private-thread"

[telegram]
bot_token = "from BotFather"
allowed_user_id = 123456789
```

`vault` must point to the canonical private Thread clone. Ariadne keeps it as the agent's working directory and uses its semantic knowledge model to retrieve and update durable context.

> [!IMPORTANT]
> Do not commit `config.toml`, credentials, or your Thread repository. `config.toml` and `mail-routes.yaml` are ignored by default, but keeping them outside the source checkout is a useful second line of defence.

## Check, then run

```bash
uv run ariadne config check
uv run ariadne serve
```

`config check` validates the resolved configuration without printing secrets. To use a different private configuration, set `ARIADNE_CONFIG` or put `--config PATH` before the command you are running. `ariadne config show` displays the effective configuration with secrets redacted. `python -m ariadne` exposes the same CLI when a console script is inconvenient, but it still needs the explicit `serve` command.

Send `/new` in Telegram to start a fresh Codex conversation while retaining durable knowledge. `/settings` selects a supported model, reasoning effort, and web-research setting for the running Telegram process. `/stop` asks the active turn to interrupt; completed work cannot be undone.

## Add optional integrations deliberately

Mail, Calendar, and telemetry are off in `config.example.toml`. Enable only the pieces you intend to operate:

- **Mail:** add shared iCloud credentials under `[icloud]`, point `[mail].routes` at a private routes file, then enable `[mail]`. Mail turns may triage, flag, or move messages under their configured route policy, but never send email.
- **Calendar:** enable `[calendar]` and set an IANA timezone. Calendar writes and invitation responses can communicate externally, so keep it opt-in and review the [architecture notes](architecture.md#integration-boundaries).
- **Telemetry:** enable `[telemetry]` only after adding an OTLP endpoint and authorization to the private configuration. The included Grafana dashboard is at `docs/grafana/ariadne-observability.json`.

The implementation details and operational contracts for Mail and Calendar live in the source and their nearby documentation. They are intentionally not required for a first private conversation.

## Optional: give the bot a public identity

After filling `[telegram.identity]`, update the Telegram bot profile once:

```bash
uv run python -m ariadne.scripts.bot_profile
```

This updates the bot profile directly; it does not start the Ariadne runtime. Run it again only when the displayed name, descriptions, or profile photo should change.

## Next reading

- [Architecture and boundaries](architecture.md) for the trust model and turn lifecycle.
- [Telegram live chat](telegram-live-chat.md) for rich content and delivery behaviour.
- [Knowledge capability](knowledge-capability.md) for the private knowledge contract.
- [Behaviour scenarios](behaviour-scenarios.md) to inspect or replay important companion behaviours safely.
