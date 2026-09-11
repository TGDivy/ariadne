# Conversational background handoffs

Status: approved for implementation.

## Outcome

Mail, scheduled wake-ups, and future background activations can still investigate and complete private work, but they no longer speak directly into Telegram. They hand their findings to the continuing Telegram Iris, which presents them with the context and tone of the current conversation. Background updates never interleave with an active response, and later ordinary messages remain coherent without requiring a Telegram reply quote.

## Product behavior

- Only the shared Telegram conversation may send conversational text or files to the owner.
- A background worker either finishes silently or submits one free-form handoff containing as much materially useful context as needed: what happened, what it learned, what it completed, what remains, and references for evidence worth reopening.
- The handoff is internal context, not prewritten Telegram prose. The Telegram Iris decides how to connect it to the current discussion, what to omit, and what question—if any—to ask.
- If a direct human message arrives while handoffs are waiting, include a bounded batch with that next turn so Iris can integrate them naturally.
- Otherwise, start a proactive turn on the same shared Telegram thread after two minutes without human or Iris conversational activity.
- Never inject a newly arrived handoff into an already-running turn. Direct human input always has priority; if it arrives after a proactive turn starts, use the existing steering behavior so Iris handles both coherently.
- Give several pending handoffs to Iris together in FIFO order and let the model combine them conversationally. Do not build semantic grouping, topic clustering, digests, or priority classes in this checkpoint.
- A handoff judged worth surfacing should be communicated unless the shared Iris can see that it has become obsolete or duplicative.

## Minimal design

- Add one handoff table to the existing private Telegram SQLite state rather than introducing a message broker or new service. Keep only the operational fields needed for durability: identifier, originating activation key, creation time, lightweight source label, status/error, and a free-form text body.
- Generously bound the body for operational safety, but do not impose a structured findings schema. Prefer opaque mail, Calendar, knowledge, or file references over copying very large source documents.
- Add a background-only MCP capability such as `hand_off_to_telegram_conversation(text)`. Enable it for Mail and revisit profiles and make it available to later background profiles explicitly. The ordinary Telegram profile does not need it.
- Replace `send_telegram_message` in background profiles with this capability. Background profiles must also lose direct Telegram file delivery; a handoff may reference a prepared file and the shared Telegram turn can deliver it through its existing capability.
- Infer the activation key and source from the local profile/job environment when convenient. This is trusted single-owner infrastructure: source metadata is for behavior and diagnosis, not an authentication or anti-spoofing boundary.
- Permit at most one staged handoff per activation. A repeated submission before release may replace that activation's staged body, making retries simple and avoiding bursts.
- Keep a submitted handoff staged until its originating mail or revisit job completes successfully. Release it only after the worker's private actions have committed; failed or cancelled activations must not produce confident messages about work that did not finish.
- Add one small coordinator to the existing Telegram service. It observes ready handoffs, the bot's busy/pending state, and the last conversational activity time; it does not become a general event framework.
- Feed claimed handoffs into the existing shared `CodexConversation`. Use a small proactive renderer that reuses Telegram formatting and durable history but does not show a fake incoming message or a premature thinking placeholder.
- Mark the claimed batch complete after the shared turn and its visible output finish successfully. Recover interrupted claims conservatively using the existing delivery/history guarantees; do not attempt a distributed exactly-once protocol around Telegram.

## Conversational instruction

Present handoffs as part of the relationship and current discussion, not as system notifications. Do not announce queues, workers, triggers, or background machinery. Lead with the human meaning of what changed, mention useful work already completed, and ask only for the decision or input that remains. When a direct message and a handoff share a turn, answer the person naturally and weave in the update at an appropriate point.

Target quality:

> Also, while we were sorting tomorrow out—the train company moved your departure to 08:40. I updated the calendar, but does that still work for you?

## Verification

- State tests cover staging, replacement within one activation, successful release, failure/cancellation suppression, ordered bounded claims, restart recovery, and completion.
- Coordinator tests with an injected clock prove the two-minute quiet window, direct-message priority, next-turn inclusion, no mid-turn injection, bounded batching, and steering when a human message arrives during a proactive turn.
- Profile tests prove background turns can hand off but cannot directly send Telegram text or files, while the Telegram profile retains its normal delivery capabilities.
- Mail and revisit tests prove a handoff is released only after the originating operation succeeds and that silent completion remains valid.
- Telegram history tests prove proactive shared-thread output remains available to later reconciliation, including ordinary non-reply messages.
- Behavior scenarios cover an update during a live conversation, multiple queued wake-ups, an obsolete/duplicate update, and a natural follow-up without replying to the proactive bubble.
- Existing format, lint, strict typing, and test suites remain green.

## Out of scope

- Multiple Telegram agents or chats, forum topics, configurable quiet periods, urgency tiers, notification digests, semantic handoff clustering, a general event bus, and persistence of the Codex conversation itself across a full Ariadne process restart.
