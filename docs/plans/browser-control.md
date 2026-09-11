# Browser control

Status: approved for implementation.

## Outcome

Ariadne can complete browser-based work through a real persistent Chromium session on Divy's private Linux home server. The capability is general enough for research, forms, reservations, and shopping; preserves existing logins; allows private human takeover when a site needs it; and exposes clear, bounded agent tools rather than relying on ad-hoc shell automation.

This repository and CI may develop and test the capability with isolated browser profiles. Live account login and final deployment validation happen on the Linux home server, not this development machine. Ithaca/iOS work remains with the Mac agent.

## Shape of the capability

Run a small long-lived browser service backed by Playwright and Chromium. Ariadne consumes it through a dedicated MCP server so the tool is enabled only for turns and initiative profiles that need browser authority. Also provide a thin administrative CLI for health checks, profile creation, inspection, and deliberate shutdown; the CLI is not the model-facing interaction contract.

Expose a compact set of composable operations:

- create or attach to a named persistent profile and task session;
- navigate, inspect tabs, and return a bounded accessibility-oriented page snapshot plus URL/title;
- click, type, select, scroll, upload, download, and wait using stable semantic targets;
- capture a screenshot when visual evidence is needed and use coordinate interaction only as a fallback;
- report downloads, navigation, dialogs, and material page changes;
- close or release the task without destroying its persistent login profile;
- request human takeover and resume after the human releases control.

Prefer inspect-then-act calls with fresh element references over arbitrary JavaScript execution or long model-generated Playwright programs. Keep the primitive surface capable rather than creating a special MCP tool for every website.

## Server and profile lifecycle

- Use a headful Chromium instance inside an appropriate Linux display environment so sites behave normally. Provide private, authenticated visual access over the owner's existing secure network or tunnel for login, CAPTCHA, MFA, recovery, and takeover; do not expose the browser publicly.
- Store browser user data, downloads, screenshots, and task state beneath explicitly configured private paths with restrictive permissions. Never commit them or include session secrets in logs.
- Maintain separately named profiles where isolation is useful, with an initial personal profile suitable for Waitrose and similar owner-operated sites.
- Permit only one mutating task to own a browser profile at a time. A waiting or conflicting turn receives truthful busy state rather than racing tabs or baskets.
- Recover abandoned task ownership after a bounded lease and service restart without killing a healthy browser profile. Do not retry a consequential final action merely because its response was lost.
- Detect expired login and pause for human takeover rather than repeatedly submitting credentials or MFA.

## Observation and reliability

Every action result should contain enough bounded state for the agent to verify its effect: current URL, title, relevant semantic changes, dialogs/errors, and optionally a screenshot. Do not return an unbounded DOM or entire accessibility tree. Redact password and payment fields from observations and logs while still allowing the browser itself to use site-stored credentials.

Record a concise private action journal with task ID, timestamps, site/origin, operation class, outcome, and paths to any deliberately retained evidence. This is for recovery and explanation, not permanent screenshots of all browsing or a raw keystroke log. Apply retention bounds to screenshots and downloads.

Downloads remain inert private artifacts until an existing Ariadne flow deliberately reads or shares them. Uploads must name the intended local artifact explicitly. New origins, file downloads, external application launches, browser permission prompts, and pop-ups are surfaced rather than silently accepted.

## Authority

The browser is a way to exercise existing authority, not a grant of new authority. Read-only browsing, public research, reversible form filling, and preparation of a cart may proceed when relevant to the active task. Existing Ariadne policy still requires confirmation before payment, sending or publishing as Divy, applications, meaningful account changes, legal/government/medical/financial actions, or commitments with material cancellation consequences.

Human takeover does not imply approval for the agent to continue a consequential action. Confirmation must identify the material state being approved, and any material change invalidates it. Sites may block automation; Ariadne should explain the boundary and offer takeover rather than attempt to bypass CAPTCHA, access controls, or anti-abuse systems.

## Integration with stewardship

Enable the browser MCP surface for Telegram and the separately approved stewardship profile. Stewardship may use it to research, prepare, and complete work within its authority, then verify the result, update Calendar or knowledge when appropriate, and communicate through the conversational handoff path. It must not browse merely to create activity or maintain an open session without a selected useful task.

## Verification

- Unit tests cover profile and task leases, allowed state transitions, stale ownership recovery, bounded observations, redaction, journal retention, and uncertainty after lost action responses.
- Browser integration tests use disposable profiles and local fixture sites for navigation, semantic inspection, forms, downloads/uploads, dialogs, pop-ups, screenshots, restart recovery, and human-takeover transitions.
- Concurrency tests prove two turns cannot mutate the same profile and that read/act references become invalid when the page changes.
- Policy tests prove preparation is distinct from consequential submission and that changed approved state requires new confirmation.
- Deployment documentation covers Chromium dependencies, configured private paths, display/takeover access, service health, backups that exclude ephemeral artifacts, and profile recovery.
- Smoke-test login persistence and human takeover on the Linux home server. Validate real sites conservatively without placing an order or making another consequential commitment.
- Run the complete formatting, lint, strict typing, and test suite.

## Out of scope

CAPTCHA or access-control bypass, public remote-browser hosting, raw payment-card storage in Ariadne, stealth/fingerprinting evasion, unrestricted arbitrary JavaScript as the normal tool interface, multi-user browser tenancy, iOS browser control, and autonomous payment authority.
