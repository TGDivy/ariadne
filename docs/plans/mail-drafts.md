# Provider-side mail drafts

Status: approved for implementation.

## Outcome

Iris can leave a real, editable draft in Divy's own mailbox instead of handing him reply text to copy out of Telegram. The draft appears in the Drafts folder of the correct account in Outlook.com or iCloud Mail, already addressed, already threaded under the original conversation, and already quoting the message it answers. Divy reads it in the client he actually uses, edits it there, and sends it himself. Ariadne never sends mail.

## Product boundary

- Creating a draft is the only mailbox write this capability performs. There is no SMTP client, no submission path, and no scheduled send. The single IMAP operation is an APPEND carrying the `\Draft` flag.
- A draft is inert. Nothing in Ariadne later selects, submits, or acts on a message it has drafted, and no route, decision, or background loop consumes the Drafts folder.
- Two shapes cover the real requests. A reply continues an existing message; a composed draft starts a new one to named recipients. Both produce the same kind of ordinary draft.
- Replies are addressed reply-all: the original sender in `To`, the remaining original recipients in `Cc`, with Divy's own account addresses removed. He can narrow the audience in his client before sending, which is exactly the point of leaving it as a draft.
- The existing `record_current_mail_decision` contract is unchanged. Its `draft_reply` field remains the text Iris carries into the Telegram handoff; drafting into the mailbox is a separate, explicitly invoked action rather than a side effect of classifying a mail event.
- Never place a draft anywhere except the account's own Drafts folder, and never create that folder. An account without a discoverable Drafts folder is a clear failure, not an invitation to reorganize the mailbox.

## Design

- Add `ariadne mail draft` to the unified CLI. Drafting is a deliberate, bounded, reviewable operation of the same kind as the existing Calendar writes, and it is useful in any turn — an ordinary Telegram conversation can ask for a draft to someone with no mail event in sight. It is deliberately not an MCP tool: the model process holds no mailbox credentials, and the MCP surface stays limited to the mutation that only means something inside a claimed ingestion job.
- Reply mode takes `--reply-to` with an opaque mail ID from `mail search`. The ID already carries the account, folder, UIDVALIDITY, and UID, so the account is resolved from the ID rather than guessed, and a stale ID fails the same way `read` and `thread` already fail.
- Compose mode takes `--account`, `--to`, and `--subject`, with repeatable `--to` and `--cc`. When exactly one account is enabled, `--account` may be omitted; when several are, it is required rather than defaulted, because writing into the wrong mailbox is not recoverable by the reader.
- Both modes take the body as `--body` text or `--body-file`, exclusive and required. Recipient flags belong to compose mode only, keeping the reply contract a single predictable behavior.
- Build the message with the standard library `EmailMessage` and a UTF-8 plain-text body. A reply sets `In-Reply-To` to the original `Message-ID` and appends that ID to the original `References` chain, bounded to the most recent entries so a long thread cannot grow the header without limit. `Subject` gains one `Re:` prefix and never accumulates more. Every draft gets its own generated `Message-ID` and a `Date`.
- Quote the original beneath an attribution line, prefixed with `> `, truncated at a fixed byte bound with an explicit marker. Report whether quoting was truncated rather than silently shortening the record of the conversation.
- Reuse the existing per-account connection and authentication boundary exactly as search and read use it: the account registry for multi-account deployments and the same single-account path otherwise. iCloud password authentication and Outlook XOAUTH2 both work without a provider branch in the drafting code.
- Discover the Drafts folder from the IMAP `\Drafts` special-use flag, falling back to an ordinary name match. The reader already understands special-use flags for its folder exclusions, so this reuses knowledge the mailbox boundary already has.
- Return one bounded JSON object naming the account, folder, generated `Message-ID`, subject, resolved recipients, whether the original was quoted or truncated, and an explicit statement that nothing was sent.

## Configuration

No new configuration, credentials, scopes, or state. Drafting uses the accounts already enabled for Mail and the authentication material already present for them. The Outlook IMAP scope already granted covers APPEND; no mail-send permission is requested, then or ever.

## Verification

- Unit tests cover reply threading headers against a message with and without an existing `References` chain, single `Re:` prefixing, reply-all recipient derivation with the account's own addresses removed and duplicates collapsed, attribution and `> ` quoting, quote truncation reporting, and non-ASCII subject and body encoding.
- Compose mode is tested for required recipients, account selection when one and several accounts are enabled, and rejection of recipient flags in reply mode.
- Drafts-folder discovery is tested through the special-use flag, through the name fallback, and for a clear failure when neither is available.
- A fake IMAP client proves the operation issues exactly one APPEND, carries the `\Draft` flag, targets the discovered folder, and performs no copy, store, expunge, or move. Both an iCloud-shaped and an Outlook-shaped account reach the same APPEND through their own authentication path.
- Stale, malformed, and cross-account mail IDs fail with the existing bounded CLI error contract and exit statuses.
- Existing Mail search, read, thread, ingestion, and decision tests remain green. All repository formatting, lint, strict typing, and tests pass.
- Manual validation against the real accounts proves a drafted reply appears in Outlook.com and in iCloud Mail, opens as an ordinary editable draft, sits inside the original thread, and can be sent by Divy from his own client. Do not claim the feature complete until that live validation passes without placing addresses or message contents in Git.

## Out of scope

Sending mail through any protocol, scheduled or delayed send, editing or deleting an existing draft, listing drafts, HTML or rich-text bodies, attachments, inline images, signatures, per-account identity aliases, address-book lookup or recipient autocompletion, drafting from an MCP tool, and any automatic drafting that is not explicitly requested.
