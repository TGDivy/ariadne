# Conversation continuity

Status: approved for implementation.

## Outcome

An ordinary Ariadne deployment or process restart does not make Iris silently lose the active Telegram conversation. The shared Codex thread resumes from its durable identifier, while `/new` and settings changes that promise a fresh conversation remain genuine, deliberate boundaries.

## Durable shared thread

- Persist the active Telegram `AsyncThread.id` in Ariadne's existing private Telegram state, scoped to the configured owner/chat and Telegram conversation profile.
- Once a new shared thread is started, save its identifier before treating the turn as durably established. On ordinary startup, use `AsyncCodex.thread_resume(thread_id, ...)` with the current profile's working directory, permissions, instructions, MCP configuration, model, effort, and web setting.
- Keep the identifier synchronized when Ariadne deliberately replaces the shared thread. Do not apply this policy to mail, revisit, stewardship, evaluation, or other `fresh-per-event` conversations.
- Durable Telegram message history remains useful shared evidence but is not replayed as a fabricated replacement conversation when a thread should be fresh.

## Explicit fresh boundaries

`/new` must interrupt or finish any active lifecycle safely, clear the persisted identifier, and begin a genuinely fresh thread on the next turn. Settings changes that currently start a new conversation must do the same after their confirmation succeeds. Clearing conversation continuity does not erase private knowledge, Telegram history, reactions, attachments, scheduled work, or handoffs.

Do not accidentally resume the old thread after a crash between clearing it and starting the next one. Conversely, do not clear a healthy identifier merely because the process is shutting down.

## Recovery

If the saved identifier is absent, malformed, unavailable to the SDK, or cannot be resumed, log the bounded reason, invalidate it, and start a fresh shared thread without taking down Telegram. Tell Divy concisely only when the fallback materially means conversational context was lost; do not repeatedly warn on every startup.

Use a small versioned state record so future profile or SDK compatibility rules can invalidate continuity intentionally. Never search for or resume an arbitrary recent Codex thread. Treat the identifier as private state and avoid placing it in Telegram messages or routine logs.

## Verification

- Unit tests prove a newly created thread ID is persisted and resumed after reconstructing the service, while an ordinary clean shutdown preserves it.
- `/new` and each conversation-resetting settings change clear the durable ID and cannot resurrect it after a crash/restart boundary.
- Missing, malformed, unknown, and resume-failing identifiers fall back once to a fresh usable thread with no restart loop.
- Fresh-per-event profiles remain fresh, and thread state is isolated by owner/chat/profile.
- An integration test uses a fake SDK across two service instances to demonstrate that the second Telegram turn continues the first thread without replaying Telegram history.
- Run the complete formatting, lint, strict typing, and test suite.

## Out of scope

Cross-user conversation sharing, reconstructing a lost Codex thread by replaying chat, resuming arbitrary historical threads, a thread browser, permanent reasoning storage, and changing the existing semantics of private knowledge or `/new`.
