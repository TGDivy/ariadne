# Brief: build Ari, the iOS app for Ariadne

Paste this to the coding agent on the MacBook once PR-1 (conversation core, app API, dev server) is up on the `ari` branch. It is tool-neutral: it works for Codex or Claude Code.

## What you are building

**Ariadne** is Divy's private, self-hosted companion runtime. **Iris** is the agent he talks to. Until now the conversation ran over Telegram. **Ari** replaces Telegram: a native iPhone app for one continuous conversation with Iris, plus views of what she is doing and looking after.

- Read first:
  - `docs/plans/ari.md` (the plan; Ari is PR-4);
  - `docs/app-api.md` and the generated contract `docs/app-api.json` (OpenAPI 3.1 plus the stream events);
  - `docs/architecture.md`.
- Sibling reference: `TGDivy/ithaca`, Divy's SwiftUI HealthKit app. Clone it and read `AGENTS.md`. Copy its Swift design rules, layout conventions, XcodeGen setup, Keychain token storage and `--design-preview` fixture gallery approach.

## Constraints

- Repo `TGDivy/ariadne`. Work on a feature branch off `ari` and open a draft PR into `ari`. All iOS code lives under `apple/`.
- **Platform:** iOS 26, SwiftUI, Swift 6 language mode with strict concurrency, an XcodeGen `apple/project.yml`.
  - Targets: the `Ari` app, an `AriLiveActivity` widget extension, and `AriTests`.
  - The home-screen display name is **Ari**, set from build config.
- **Keep out of git:** Team ID, bundle identifiers, server URLs and tokens. Put them in an untracked `apple/Config/Local.xcconfig`, as Ithaca does.
- **Dependencies:** Apple frameworks only. You may add `apple/swift-markdown` (or Apple's `swift-openapi-generator`) if you can justify it in the PR; otherwise hand-write `Codable` models that match the contract.
- **Don't change the server contract yourself.** If the app needs something different, describe it in the PR, and the server agent updates the contract.
- **Use your judgement.** You decide structure, components and details. Ask Divy only for taste decisions, and show screenshots, not prose.

## Visual direction

- **Dark-first;** light mode works through semantic colours.
- **Native iOS 26:** Liquid Glass bars and controls (Ithaca uses `.buttonStyle(.glass)` / `.glassProminent`), SF Symbols, system typography with Dynamic Type, system lists and sheets. Nothing that looks like a web page.
- **One signature, the iridescent iris.** A small living prism that represents Iris in the conversation header (and in the Live Activity). It is the **only colour in the app**; everything else is monochrome glass. Its states:
  - idle: slow breathing;
  - working: a swirl whose pace hints at activity;
  - waiting for Divy: a gentle pulse;
  - a background failure: dims with a red edge;
  - offline: desaturated.

  Build it in SwiftUI (`MeshGradient` / `AngularGradient` + `TimelineView` or `Canvas`). With Reduce Motion, show a static iris tinted by state. Respect Reduce Transparency.
- **Iris's colour is also used, sparingly,** for running indicators and for highlighting what changed in a rendered record.

## Screens and behaviour

**Pairing.** Server URL plus token (pasted). The token goes in Keychain (`AfterFirstUnlockThisDeviceOnly`), the URL in `UserDefaults`.

**Conversation.**
- **Header:** the iris, the agent's name from `GET /v1/identity`, and a one-line status from status events (the current activity, "2 things in progress", a failure marker). Tapping the header opens the Iris sheet.
- **Messages:** Iris on the left, Divy on the right.
  - Render Markdown: paragraphs, emphasis, lists, task lists, code blocks, tables, links.
  - Each committed commentary or final message is its own bubble.
  - Speech streams provisionally and is replaced by the committed message.
  - Reasoning summaries show as a quiet provisional "thinking" line and are never kept.
- **Inline cards:**
  - **Background job:** live label, state, Stop; tapping opens it in Activity.
  - **Question:** buttons plus a typed answer; a stale card does nothing.
  - **Shared file.**
- **Composer:** text; `+` for photos (PhotosPicker) and files (fileImporter); a mic that transcribes on the device and sends the text flagged as voice; swipe to reply; long-press to react; a Stop control while a turn is running.
- Scroll up to page through history. Proactive messages look like any other message.

**Iris sheet:** a segmented control with Upcoming | Activity.
- **Upcoming:** scheduled wake-ups and delegated jobs grouped by day, then a Rhythms section (daily initiative, weekly digest) showing time, attention and linked Thread records.
- **Activity:** rows grouped by day.
  - Each row has an outcome chip (running / spoke / handed off / quiet / failed / stopped), Iris's one-line account, and expandable "observed effects".
  - Running rows have Stop. Failed rows stand out.
  - The weekly digest appears as a card.

**Goals tab.** Each goal shows its title, summary, "next" and "last / quiet N days". Tapping one opens the rendered record with its linked upcoming items and activity, plus an **Ask Iris** button that opens the composer with the record as reply context.

**Library tab.** Thread folders, then records rendered from Markdown. Read-only.

**Settings.** Pairing, model / effort / web research, new conversation (with confirmation), notification status.

## Networking

- **Commands** use URLSession `async/await` with a bearer header. Each send carries a `client_message_id` (UUID), so retries are idempotent; show an optimistic local echo and reconcile it when the server commits the message.
- **Event stream:** `URLSessionWebSocketTask` with the Authorization header (never put the token in the query string).
  - Keep `lastSeq`; reconnect with backoff and `after=lastSeq`.
  - If `boot_id` changes, refetch the snapshot and history.
  - Send heartbeats.
  - Close the socket in the background and reconnect in the foreground. While backgrounded, push is the only channel.
  - Show an offline state.

## Notifications and Live Activity

- **Push:**
  - register for remote notifications and post the device token plus environment (sandbox for Xcode builds, production for TestFlight) to `/v1/devices`;
  - request alert, sound, badge and time-sensitive authorisation, and add the Time Sensitive capability;
  - the server sets the interruption level; tapping a notification opens the conversation at that message.
- **Live Activity** (delegated jobs only):
  - When a delegated job starts while the app is open, start a Live Activity (Lock Screen + Dynamic Island: the iris glyph, title, latest label, elapsed time) and post its push token to `/v1/live-activities`.
  - The server pushes updates and the end event. `ContentState` must match the server's payload field for field.
  - End it locally if the stream reports completion first.

## Development loop

- Run the backend on the Mac without credentials:

  ```bash
  uv sync --locked --all-groups
  uv run ariadne-dev-server --host 0.0.0.0
  ```

  It prints a URL and a dev token. Use `/dev/trigger/{proactive,question,job-progress,failure}` to force UI states, and `/dev/pushes` to read the recorded push payloads (replay them in the Simulator with `xcrun simctl push`).
- Later, point the app at the test instance's Funnel URL to try it end to end with real Iris.
- **Design-preview gallery:** like Ithaca, a `--design-preview <scenario>` launch argument with fixtures for every screen and state:
  - conversation streaming, question, job card, shared file;
  - Upcoming; Activity including quiet and failed rows;
  - goals, library record, settings, pairing, offline.

  Attach dark *and* light Simulator screenshots of every scenario to the PR. Divy reviews visuals from these.

## Testing

- **Logic tests:** the stream reducer (events → UI state), resume by `seq` and `boot_id` changes, reconciling idempotent sends, Markdown block parsing, Activity grouping. Skip snapshot tests unless they're cheap.
- **Physical iPhone checks:** interruption levels, the Live Activity, on-device transcription, Keychain persistence across launches.

## Done when

- [ ] It pairs with the dev server and with the test instance.
- [ ] **Messages:** streaming with commentary and final bubbles; the provisional thinking line; steering while busy keeps order with no duplicates on retry; Stop keeps partial speech.
- [ ] **Questions** answered by tap or typed text; stale cards are inert.
- [ ] **Input types:** photos, documents, on-device voice, reply-to, reactions.
- [ ] **Delivery:** proactive messages arrive in the app, and by push with the right interruption level when it's closed; nothing committed is lost across background / airplane-mode reconnects.
- [ ] **Views:** the Iris sheet (Upcoming; Activity with quiet and failed rows; Stop on running rows); Goals and Library render tables and task lists.
- [ ] **Live Activity** for delegated jobs updates and ends.
- [ ] Dark and light screenshots for every preview scenario are attached.
