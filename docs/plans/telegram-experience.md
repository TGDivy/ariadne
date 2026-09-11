# Telegram experience

Status: approved for implementation.

Depends on: conversational background handoffs and proactive stewardship for their status/control surfaces.

## Outcome

The private Telegram chat feels like messaging a thoughtful person rather than watching a terminal or operating an admin bot. Iris speaks in several natural, relatively small conversational messages when a response has multiple beats; useful thinking and activity remain visible while work is underway; completed speech produces notifications containing the actual message; and deterministic controls are informative but disappear when conversation resumes.

## Current behavior to preserve deliberately

A Telegram turn currently sends one persistent Rich Message, edits it with generated reasoning summaries, semantic activity, and streaming speech, and finally replaces it with authored commentary or final text. Reasoning summaries are concise generated summaries rather than raw hidden reasoning. They are temporary and excluded from durable Telegram history. Each completed commentary item is already capable of becoming a permanent bubble, while final ends the turn.

Keep rich formatting, mid-turn steering, interactive questions, file-delivery approval, interruption through `/stop`, safe streaming of incomplete rich structures, semantic activity labels, and durable history of genuine human/Iris messages.

## Friend-like message rhythm

Strengthen the Telegram conversation instructions rather than mechanically chopping prose by character count:

- Treat each permanent bubble as one conversational beat.
- When a response naturally contains several beats, send earlier complete beats as commentary and reserve final for only the last beat.
- Prefer a few relatively short messages over one long report in ordinary conversation, planning, reflection, and proactive updates.
- Do not split one simple thought artificially, send fragments merely to create activity, or narrate tool use as conversation.
- Do not recap in final what an earlier bubble already said.
- Retain structured long-form output when the user actually asks for a report, table, code, or detailed analysis.

Add several examples to the instruction/evaluation material rather than one rigid template: casual back-and-forth, a researched plan delivered in three natural beats, a sensitive reflective response, and a proactive update woven into an existing conversation. Do not add deterministic short-message splitting beyond Telegram's existing rich-message safety limit until prompt/evaluation evidence shows it is needed.

## Thinking and activity presentation

- Continue showing generated reasoning summaries and real semantic activities while work is in progress.
- Make their provisional nature visually unmistakable with a stable, quiet treatment such as a labelled quoted block followed by the activity:

  ```text
  Thinking
  Connecting this with tomorrow's schedule…

  ✦ Reading Calendar…
  ```

- Keep the current structural streaming safeguards and edit throttling so partial Markdown, code, tables, links, media, or mathematical notation never render as broken source.
- Temporary thought/activity previews must remain absent from durable conversation history and disappear when replaced by completed speech.
- Do not retain a reasoning transcript or expose raw hidden reasoning.
- Do not add an inline Stop button. Keep the existing `/stop` command available and visible in Telegram's command menu.

## Completion notifications

Telegram normally notifies for the initial sent message but not for later edits, so editing `Thinking…` into the answer leaves a backgrounded user without a notification containing the completed response.

Change the delivery lifecycle:

1. Send each temporary thought/activity bubble with `disable_notification=true` and edit it during work.
2. Stream provisional authored text in that temporary bubble as today.
3. When a commentary or final speech item completes, send its exact complete Rich Message as a new normal, notification-bearing message and record that new message in history.
4. Delete the temporary preview after the permanent send succeeds. After commentary, open a new silent preview for later work; after final, finish with no preview.

The permanent send is authoritative. If preview deletion fails, log and recover without resending the completed speech. Preserve safe block splitting for oversized messages; each authored bubble or required overflow chunk is a genuine new message. Handle Telegram-accepted/history-write failures with the existing conservative no-duplicate contract.

Manually validate notification behavior on the owner's actual Telegram clients with the app foregrounded, backgrounded, and the phone locked. Telegram ultimately controls device notification presentation; the required result is a fresh completed-message notification carrying final authored text rather than relying on an edit notification.

## Replies and reactions

Preserve Telegram's native conversational gestures rather than turning them into commands:

- When Divy replies to a specific permanent Iris or human bubble, include the referenced message's stable identity, author, and visible content in the next turn. The existing basic reply context is the baseline; verify it remains correct across multiple commentary bubbles, restarts, rich messages, and overflow chunks.
- Never make a deleted temporary thought/activity preview the durable target of a reply. If a reply arrives during streaming, resolve it to the visible preview content conservatively and preserve the relationship when accepting it as steering.
- Accept Telegram reaction updates on permanent Iris messages and store their current state as lightweight private feedback linked to that message. Repeated updates must replace the prior reaction state rather than inflate a count.
- A reaction does not immediately start or steer a model turn and does not need an acknowledgement. Recent reactions are available to ordinary conversation and proactive stewardship as evidence of what resonated, felt unhelpful, or deserves revisiting.
- Do not assign rigid universal meanings to individual emoji. Positive and negative reactions are useful signals, not proof of intent; Iris may ask naturally when an important interpretation is ambiguous.

Reaction handling is restricted to the configured owner and messages in the private Ariadne conversation. Aggregate anonymous reaction counts and reactions to temporary control/thinking messages are not conversational memory.

## Incoming voice notes

Accept a bounded Telegram voice note as another natural way for Divy to speak. Download it into the existing private attachment lifecycle, transcribe it through a small configurable transcription boundary, and submit the transcript to the same shared conversation with its Telegram message and reply context. Label it as a transcription so Iris can account for possible recognition errors without prefacing every answer with a transcript recap.

Store a concise transcript as a durable human `voice` message and retain no additional model-facing audio history. If transcription is unavailable or fails, explain that briefly in a normal notification-bearing message and leave the conversation usable. Enforce duration and byte limits before expensive processing. Spoken Iris responses, live audio streaming, voice cloning, and autonomous interpretation of non-speech audio are separate features.

## Ephemeral command panels

Treat deterministic commands as temporary controls, not conversation:

- Delete the owner's command message after accepting it when Telegram permits.
- Maintain at most one bot control panel at a time and edit it while the owner navigates buttons.
- Send panels silently, exclude them from conversational history, and remove them when a normal text/media message is accepted or a direct/proactive conversational turn begins.
- Retain enough panel identity in the existing private Telegram SQLite state to remove an abandoned panel after restart or on the next interaction. Stale and duplicate callbacks must remain harmless.
- If deletion fails, keep the control correct and inert rather than allowing cleanup failure to break conversation.

Apply this lifecycle consistently to `/status`, `/wakeups`, `/settings`, `/new` confirmation, `/stop` notices, and other deterministic cards. Useful partial Iris speech retained after `/stop` is conversation, not a disposable command artifact.

## Status and scheduled-work controls

Add `/status` as the compact entry point and `/wakeups` as a direct shortcut. Keep `/new`, `/stop`, and `/settings` in the menu. Avoid commands for ordinary Mail, Calendar, health, goals, or knowledge work; those remain conversational capabilities.

`/status` should show only truthful, useful state, in the configured local timezone:

- whether Iris is ready or working;
- initiative enabled/paused/running state, last completed cycle, and next expected cycle;
- counts of upcoming and failed one-off wake-ups;
- count of conversational handoffs waiting for a quiet moment;
- which major private sources are enabled, without claiming live connectivity that has not been measured;
- current Telegram model, effort, and web-research mode.

Use trusted buttons to navigate the same panel to Wake-ups, Initiative, and Settings. Initiative controls may Run now, Pause for 24 hours, Pause indefinitely, and Resume. A waiting-handoff control may request delivery now without displaying an internal notification-inbox UI.

The Wake-ups panel should present a bounded chronological page with readable due time, concise note, attention level, and visible failure state. Allow cancellation through a confirmed trusted control. Keep rescheduling and substantial note changes conversational rather than building a form system.

Settings should retain the current trusted-button interaction and clearly disclose that changing the model, effort, or web mode starts a fresh Codex conversation. Control changes edit the panel in place and do not themselves become model turns.

## Telegram-focused bug pass

While implementing this checkpoint, inspect and fix other reproducible defects within the Telegram adapter, adding a regression test for each. Keep this bounded to Telegram behavior rather than opportunistically redesigning unrelated systems. Pay particular attention to:

- races between normal messages, steering, proactive turns, and panel cleanup;
- stale/double callbacks and authorization checks;
- preview send/edit/delete ordering and notification flags;
- failures after Telegram accepts a permanent message;
- duplicate or missing durable history;
- restart cleanup for questions, panels, handoffs, and live placeholders;
- Rich Message block splitting and incomplete-structure streaming;
- albums, documents, voice-note failures, reply context, reactions, message-thread identity, and command deletion;
- terminal failure and `/stop` behavior after the new permanent-send lifecycle.

Summarize concrete additional fixes in the PR description. Do not make speculative changes without a reproduced failure or clear violated contract.

## Verification

- Renderer tests prove temporary messages are silent, permanent commentary/final messages are fresh sends, previews are deleted only after success, multiple speech phases create ordered bubbles, and history contains only permanent authored messages.
- Failure tests cover permanent-send rejection, accepted-send/history failure, preview deletion failure, oversized output, stop, and cancellation without automatic duplicates.
- Prompt and behavior scenarios demonstrate several natural short bubbles without acknowledgement spam, work-log narration, or final repetition.
- Panel tests cover single-panel replacement, immediate command cleanup, automatic cleanup on every conversational entry path, restart recovery, silent sends, harmless stale callbacks, and deletion failures.
- Reply tests cover permanent commentary/final bubbles, rich overflow, mid-turn replies, restart, and references whose Telegram source is no longer available.
- Reaction tests cover add/change/remove updates, duplicate delivery, owner authorization, deleted messages, and proof that reactions neither start nor steer a turn. Stewardship scenarios treat them as uncertain feedback rather than commands.
- Voice-note tests cover transcription, reply context, duration/size bounds, unsupported or failed transcription, durable transcript history, and attachment cleanup.
- Status/wake-up tests cover local-time rendering, bounded pagination, failed items, cancellation confirmation, initiative controls, queued-handoff counts, and truthful enabled-versus-connected wording.
- Manually test on the owner's Telegram client: watch a long turn live, background the app before completion, confirm that each completed conversational message produces an appropriate notification with its real text, return to a clean chat, and exercise every command panel.
- Run the complete formatting, lint, strict typing, and test suite.

## Out of scope

- An inline Stop button, permanent reasoning history, raw chain-of-thought display, deterministic sentence/character chopping, a Telegram Mini App, a second notification chat or agent, spoken Iris responses, commands for ordinary life-data queries, and a general administrative dashboard.
