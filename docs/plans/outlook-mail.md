# Outlook.com mail support

Status: approved for implementation.

## Outcome

Ariadne can operate one personal Outlook.com mailbox and the existing iCloud mailbox simultaneously through one provider-neutral Mail capability. Iris searches, reads, follows threads, and handles a current mail event without choosing a provider or learning mailbox sizes. Results retain ordinary addressing and a friendly account label; opaque IDs route later operations to the correct account.

## Product boundary

- Keep the existing safety model: read, flag, move, and draft a suggested reply, but never send mail.
- Search all enabled accounts by default, merge and rank bounded results globally, and accept an optional account filter.
- Make every returned mail ID account-bound. `read` and `thread` decode the ID and access only its source account.
- Keep thread discovery within the source account. Do not silently deduplicate copies found in different accounts because each copy has independent mailbox state.
- Keep moves and flags scoped to the existing current-mail-event decision. Do not add an ambient model-facing command for reorganizing arbitrary search results.
- Apply the existing shared ordered route file to both accounts. Recipient matching can express account-specific rules without introducing separate rule systems.
- Report partial search failures honestly when one account is unavailable; do not present partial results as complete.

## Design

- Introduce a small account registry and mailbox connection boundary. Reuse one IMAP reader, routing pipeline, message parser, action implementation, and response schema rather than creating parallel iCloud and Outlook features.
- Retain iCloud password authentication and add Outlook OAuth 2.0/XOAUTH2 authentication over TLS IMAP. Start with Outlook IMAP so folders, IDLE, flags, moves, and message parsing retain the current semantics; use Microsoft Graph only if physical Outlook.com validation finds a required IMAP operation unsupported or unreliable.
- Run an independently supervised ingestion/IDLE loop per enabled account, with the existing durable catch-up behavior after disconnects or restarts. Failure of one account must not stop the other.
- Include a stable configured account key in logs, jobs, checkpoints, prompts, and public Mail results. Never expose credentials, access tokens, refresh tokens, or raw authentication failures containing secrets.
- Version the opaque ID payload and include the account key plus the existing folder, UIDVALIDITY, and UID identity. Continue accepting legacy IDs by resolving them to the existing iCloud account.

## Configuration and authorization

- Preserve existing single-iCloud configuration without required edits. Add the smallest backward-compatible configuration for enabling a personal Outlook.com account alongside it, including a stable account label, Outlook address, Microsoft public-client ID, and private token-cache path.
- Target Microsoft personal accounts through the consumer authorization endpoint. Request only the scopes needed for IMAP read/write access and offline refresh; never request mail-send permission or require a client secret.
- Provide an explicit owner-run device-authorization command. It prints Microsoft's verification URL and short code, allowing the browser step on any phone or computer while the command continues polling on the Ariadne host.
- Store the resulting refreshable token cache only on the Ariadne host. Create it with restrictive permissions and update it atomically. Configuration inspection and validation errors must redact all token material.
- Document the Microsoft application-registration settings and the initial authorization, reauthorization, revocation, and account-removal procedures.

## Durable state migration

- Add the account key to mail jobs and checkpoints. Identity and uniqueness become account plus mailbox, UIDVALIDITY, and UID.
- Migrate existing SQLite state atomically and assign all existing rows/checkpoints to the legacy iCloud account. Preserve completed jobs, pending retries, decisions, and watermarks.
- Scope the current-event environment/context to the account as well as the job so a recorded decision can affect only the originating mailbox.

## Surfaces to update

- The long-running mail service and connection factory.
- Unified `ariadne mail search`, `read`, and `thread` commands and their bounded JSON schemas.
- Current-event decision handling, prompts, telemetry labels, and logs.
- Route lint, backfill, restore, and export operator tools. Mutating bulk tools must require an explicit account when ambiguity would be unsafe; exports must identify each record's account.
- Example configuration, architecture/operations documentation, and dependency lock if an OAuth library is required.

## Verification

- Unit tests cover backward-compatible configuration, account registry dispatch, OAuth refresh/cache handling with no live credentials, secret redaction, versioned and legacy IDs, cross-account search ranking, optional filtering, partial failure reporting, provider-isolated loops, account-qualified state migration, restart catch-up, and correctly scoped flag/move actions.
- Existing iCloud behavior and configuration tests remain green.
- All repository formatting, lint, strict typing, and tests pass.
- Manual validation against a personal Outlook.com account proves: device authorization from a separate browser device; token refresh after restart; bounded search, read, and thread; new-mail ingestion; flag and move; disconnect catch-up; and simultaneous continued iCloud operation.
- Do not claim the feature complete until that live Outlook.com validation passes without placing addresses, tokens, or message contents in Git.

## Out of scope

- Sending mail, creating drafts in the provider mailbox, calendar/contact support, Microsoft 365 organizational accounts, shared mailboxes, multiple Outlook accounts, webhook infrastructure, cross-account threads, and Graph-only enrichment.
