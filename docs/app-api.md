# The Ari app API

`ariadne serve` hosts the API the Ari iPhone app talks to. The exact request,
response, and event shapes live in the generated [`app-api.json`](app-api.json)
(OpenAPI 3.1 plus an `x-stream` section); this page explains how the pieces fit.

## Reaching the server

The API listens only on a loopback address (`[app].host`, default
`127.0.0.1:8737`). A private HTTPS proxy makes it reachable from the phone; the
intended one is Tailscale Funnel:

```bash
tailscale funnel --bg --https=443 http://127.0.0.1:8737
```

Push notifications do not depend on this route: they travel from the server to
Apple to the phone. The route matters only while the app is open.

## Pairing a phone

Every route except `/health` needs `Authorization: Bearer <token>`, on the
WebSocket handshake too. The server stores only each token's SHA-256:

```bash
ariadne-app pair --id iphone --name "iPhone"
```

The command prints the token once (enter it in Ari, which keeps it in the
Keychain) and an `[[app.devices]]` entry to paste into the private
configuration. Restart Ariadne after adding it.

## Commands and the event stream

Commands are plain HTTP. `POST /v1/messages` carries a client-generated
`client_message_id`, so a retry after a dropped connection is harmless: the
second response says `duplicate` and returns the original message. The response
also says what the message did:

| Disposition | Meaning |
| --- | --- |
| `started` | It began a new turn. |
| `steered` | It joined the turn already running. |
| `queued` | It waits for the next turn (the live turn could not take it). |
| `answered` | It answered the question Iris is waiting on. |

The conversation streams over `GET /v1/stream` (WebSocket, server to client
only). The first frame is a `snapshot`: the live turn (activity label,
provisional reasoning summary, provisional speech), any waiting question, and
the sequence number of the last committed event.

- **Committed events** (`message.committed`, `turn.started`, `turn.finished`,
  `question.asked`, `question.settled`, `reactions.changed`,
  `conversation.reset`, `input.accepted`) carry a monotonic `seq`.
- **Live events** (`speech.updated`, `activity.changed`, `work.summary`,
  `turn.state`, `status.changed`, `notice`, `heartbeat`) carry `seq: null`.

To resume, reconnect with `?after=<last applied seq>`. The server replays every
committed event since then, after the snapshot. If those events have been pruned,
or no cursor is given, the snapshot says `reset: true` and the app refetches
`/v1/messages`. A changed `boot_id` means the server restarted. Close code `4401`
means unauthorized; `4409` means the client fell behind and should reconnect with
its cursor.

Provisional speech (`speech.updated`) is replaced by the permanent
`message.committed` that follows it. A stopped turn keeps any useful partial
speech as a committed message with `stopped: true`. Proactive turns, which Iris
starts herself from background work, stream no provisional speech: they may end
silently when the update has become obsolete.

## Views outside the conversation

| Route | Shows |
| --- | --- |
| `GET /v1/status` | Idle or working, live activity, enabled sources, settings. |
| `GET /v1/upcoming` | Scheduled wake-ups and rhythms. |
| `GET /v1/activity` | What Iris did, newest first (filled by activations in a later change). |
| `GET /v1/goals` | Thread records in `goal/`; completed goals are archived records. |
| `GET /v1/library` | One level of the Thread; records render from Markdown. |
| `GET /v1/records/{id}` | One record with its Markdown body and links. |

## Building without a server

`ariadne-dev-server` runs the same API over a scripted model and synthetic
fixtures, with no credentials or Codex login:

```bash
uv run ariadne-dev-server --host 0.0.0.0
```

It prints a URL and a token. `POST /dev/trigger/{proactive,question,fail,long,stop,share}`
forces each app state, and `GET /dev/pushes` lists the push notifications that
would have been sent. Replies come from `src/ariadne/devkit/default_script.yaml`;
pass `--script` to use another.

## Keeping the contract current

The contract is generated from the server's models:

```bash
uv run python -m ariadne.app.contract          # rewrite docs/app-api.json
uv run python -m ariadne.app.contract --check  # fail if it is stale
```

A test fails when the checked-in document and the models drift apart, and
another fails when a served route is missing from it.
