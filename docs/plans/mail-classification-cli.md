# Standalone mail classification

Status: approved for implementation.

## Outcome

Ariadne can answer "what would happen to this message?" without waiting for the message to arrive and without spending a model turn. `ariadne mail classify` runs the same ordered routing and cheap triage that mail ingestion runs, against a message already in a mailbox or a raw message file, and reports the route that would win, the action ingestion would take, the destination folder, and whether Iris would be woken at all. Route authoring stops being guesswork verified only in production.

## Product boundary

- Classification is read-only. It selects mailboxes read-only, fetches with `BODY.PEEK`, and never moves, flags, marks, files, drafts, or enqueues anything. Running it on the same message twice changes nothing.
- It reports what ingestion *would* do. It does not create a mail job, claim a job, consume a checkpoint, or start a Codex conversation, and its answer is never recorded as a decision.
- It reuses the shipped classification path rather than reimplementing it. The route matcher, the cheap-triage heuristic, and the rule that turns a matched route into an action are shared with the ingestion loop, so a divergence between the two is a code change, not a silent drift.
- It answers for one message at a time. Bulk measurement over a whole mailbox already exists as the route-lint operator script and is not duplicated here.
- A raw file is treated as untrusted external evidence: it is parsed for headers only, and its content never becomes an instruction.

## Design

- Extract the fresh-message decision that currently lives inline in the ingestion processor into one pure function over validated routes and parsed metadata, returning the winning route, the resulting classification, the action, the destination folder, whether Iris would be woken, and the cheap-triage verdict when no route matched. The ingestion processor calls it instead of keeping its own copy; the CLI calls the same function.
- Report every matching rule in configured order, not only the winner, so a shadowed rule is visible where it is easiest to act on. This reuses the ordered-match accessor the route linter already relies on.
- Move the pure header heuristics next to the other pure routing data so the shared function has no runtime, IMAP, or model dependency, and keep the existing public names re-exported so no caller has to change.
- Accept either `--id` with an opaque mail ID from `mail search` or `--file` with a raw message path, exclusive and required. The ID path resolves its account from the ID, selects that folder read-only, verifies UIDVALIDITY, and fetches exactly the header set ingestion fetches, so the classification is computed from the same evidence rather than a richer accidental one.
- Load routes from the configured private route file through the existing validated loader, and fail with the existing configuration error contract when Mail is not enabled or the route file is invalid.
- Emit one bounded JSON object: the account when there is one, the message's identifying headers, the ordered matching route IDs, the selected route, the action and destination, whether Iris would be woken, the cheap-triage verdict, and the effective unmatched defaults. Follow the established CLI conventions for compact JSON, `--pretty`, redaction, and exit statuses.

## Configuration

No new configuration. Classification reads the Mail accounts and the route file already configured, and needs no credentials at all in the `--file` form.

## Verification

- Unit tests prove the extracted function returns the same route, action, destination, and wake decision the ingestion processor produced before the refactor, across a `move` route, an `iris` route, an `iris_then_move` route, an unmatched message under `inspect`, and an unmatched message under `cheap_triage` for both routine and non-routine headers.
- Existing ingestion, routing, and processor tests remain green unchanged, demonstrating the extraction preserved behavior.
- CLI tests cover the file form with no configured credentials, the ID form against a fake client, ordered reporting of multiple matching rules, unknown and stale IDs, a missing or unreadable file, an invalid route file, and Mail not being enabled.
- A test asserts the ID form issues only a read-only select and a `BODY.PEEK` header fetch, with no store, move, copy, expunge, or job insertion.
- All repository formatting, lint, strict typing, and tests pass.

## Out of scope

Classifying a whole mailbox, applying the reported action, writing decisions or jobs, editing route files, suggesting new rules, model-assisted classification, batch or streaming input, and any change to the ordered first-match routing semantics themselves.
