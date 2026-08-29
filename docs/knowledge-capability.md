# A first-class knowledge capability

Status: implemented; the live Thread is canonical and production Telegram and
mail turns use the semantic capability

## The boundary

The Thread remains one private, human-readable collection of Markdown files
with Git history. Iris should experience remembering as semantic operations,
not as filesystem editing, committing, or publishing a repository.

```text
Iris                                  Ariadne

search / browse / read          ->    find canonical records
create / update / archive       ->    validate and write Markdown atomically
semantic result                 <-    pull, commit, and push automatically
```

There is one canonical document set. The in-memory search index is derived
entirely from Markdown and can always be rebuilt. Ariadne owns paths, filenames,
timestamps, locking, validation, Git, and operational recovery. None of those
details belong in model-visible results or routine Telegram messages.

The existing Thread will be adapted manually and in place, file by file. The
capability assumes compatible records and has no parallel legacy representation.

## Records

Every record is ordinary Markdown with a compact front matter envelope:

```yaml
---
schema: 1
id: event:windsor-trail-run-2026
title: Windsor Trail Run
summary: Confirmed half-marathon in Windsor on 30 August, with transport and preparation being organised.
kind: event
collection: running
tags:
  - health
  - running
  - travel
aliases:
  - Windsor half marathon
starts_at: 2026-08-30T09:20:00+01:00
ends_at: 2026-08-30T12:00:00+01:00
related:
  - record: goal:running
    relation: supports
created_at: 2026-08-29T10:04:00Z
updated_at: 2026-08-29T14:31:00Z
---
```

Iris supplies the semantic fields: title, summary, one primary kind, collection,
tags, aliases, meaningful dates, relationships, and Markdown body. Ariadne
generates the stable ID, filename, storage location, creation and update times,
and optional archive time.

There is no universal free-form state. Lifecycle facts remain in the summary or
body until a concrete kind needs a small typed status. Archiving is system-owned
metadata set only through `archive_knowledge`.

`kind` answers what a record primarily is. `tags` express its overlapping
subjects. `collection` places it in the familiar hierarchy. Existing kinds,
collections, tags, and relationship names are shown to Iris in generated
orientation so she can reuse conventions without making them a closed taxonomy.

## Human-readable hierarchy

Every record lives beneath at least a kind and collection. All folder and file
segments are lowercase kebab-case:

```text
people/friends/lily.md
people/family/mum.md
event/running/windsor-trail-run-2026.md
goal/health/half-marathon.md
project/ariadne/overview.md
journal/2026/2026-08-29.md
scratch/ariadne/knowledge-search.md
```

Collections may be deeper where that remains natural. Paths aid human and model
orientation but are never record identity. Reads, updates, archives, and
relationships use the stable ID, so reorganization cannot break references.

## Model-facing operations

### `search_knowledge`

Search is transparent ranked lexical retrieval, not embedding search. Query
terms broaden results rather than all being mandatory. Ranking strongly favours
exact IDs, titles, aliases, and phrases, followed by weighted title, alias, tag,
summary, collection, and body matches. It uses prefix matching, stemming, and
small spelling tolerance.

Optional filters cover kinds, collections, tags, date overlap, a directly
related record, and archived records. Filters are exact, and all requested tags
must be present. Results contain title, summary, kind, collection, tags, dates,
an excerpt, matched and unmatched terms, match fields, and compact relationship
summaries.

### `browse_knowledge`

Browse exposes one to five levels of the familiar collection tree. It is useful
when search wording is uncertain, nearby records matter, or Iris needs a broad
map for self-organization. It returns stable IDs and optional summaries; paths
remain orientation rather than identity.

### `read_knowledge`

Read accepts up to twenty stable IDs and returns their complete semantic
metadata and Markdown bodies. Direct incoming and outgoing relationships
include the related ID, title, summary, kind, label, and direction. Related
bodies are not fetched unless Iris explicitly reads those IDs.

### `create_knowledge`

Create accepts title, summary, kind, collection, body, and optional tags,
aliases, dates, and relationships. Ariadne generates everything operational,
creates a lowercase kebab-case filename, commits, and pushes.

### `update_knowledge`

Update accepts a stable ID and only the semantic fields that should change.
Under one lock, Ariadne pulls, reads the latest record, applies supplied fields,
updates the internal timestamp, moves the file if its generated location
changes, commits, and pushes. There is no model-visible revision protocol.

### `archive_knowledge`

Archive accepts a stable ID and short reason. Ariadne marks the record archived,
updates its timestamp and body, commits, and pushes. Nothing is hard-deleted.

## Relationships

A relationship is directed and labelled:

```text
event:windsor-trail-run-2026 ──supports──▶ goal:running
```

Ariadne derives incoming edges in memory; files do not duplicate both
directions. `related_to` searches direct neighbours only. Search and read return
compact semantic information about those neighbours but never recursively load
an entire graph.

Relationship names remain open initially. Generated orientation reports the
names already in use and asks Iris to reuse them where they fit. Navigation by
record ID works independently of the label, so we can observe real usage before
deciding whether labels need a controlled vocabulary.

## Generated orientation

Before a production turn, Ariadne will generate compact trusted context from
the canonical store:

```text
Current knowledge structure:

people/
  family/
  friends/

event/
  running/
  travel/

Kinds: event, goal, journal, person, plan, preference, project, scratch
Tags: career, family, health, running, travel
Relationships: depends-on, involves, supports, supersedes
```

Only a shallow tree and current vocabulary belong in every prompt. Iris can
browse deeper on demand. Static developer instructions explain when and why to
use knowledge; the search tool description explains its actual algorithm.

The shared `with_knowledge_orientation` assembly function already applies this
generated section to a resolved turn profile and is used by the behaviour lab.
A marked cutover TODO identifies where it should become part of every applicable
production turn after the live records are ready.

## Repository validation

`ariadne-knowledge [ROOT]` performs a read-only whole-repository check suitable
for local use and automation. It validates every Markdown front matter envelope,
the `kind/collection/name.md` location, generated lowercase title filename,
timestamps, unique IDs and aliases, relationship targets, self-links, and
duplicate edges. A moved record remains valid when its path agrees with its
semantic fields because all references use its stable ID.

The authoritative GitHub workflow belongs in the private Thread repository, not
this application repository, and should be added during the manual cutover. It
needs no model, mail, calendar, or Telegram credentials: it only checks out the
records, installs the pinned Ariadne validator, and runs:

```text
ariadne-knowledge .
```

A local pre-commit hook can run the same command with `pass_filenames: false`.
Validation intentionally scans the complete collection so cross-record links can
be proven. At the expected hundreds-to-low-thousands scale this is lightweight;
CI should remain authoritative and the hook can stay optional if its measured
startup cost becomes annoying.

## Git and concurrency

Iris never performs Git operations. Each mutation takes the shared knowledge
lock, rejects unmanaged local changes, fetches, fast-forwards only, writes
atomically, stages only files owned by the operation, commits, and pushes.

If a push fails after a local commit succeeds, that commit is the durable retry
state. The next mutation pushes it before applying new work. Diverged history is
an explicit operator conflict; Ariadne never force-pushes or silently merges two
narratives.

Updates intentionally use the latest record under the lock rather than exposing
a revision token to Iris. With one owner and serialized writes this keeps the
ordinary interaction simple. If concurrent whole-body replacement becomes a
real source of lost work, it should be solved from observed cases rather than
pre-emptively leaking storage concurrency into the model API.

## Runtime boundary

Production Telegram and mail profiles expose all six semantic operations. Their
resolved developer instructions include generated orientation from the current
canonical records, while record bodies remain available only through search,
browse, and read. Storage configuration is forwarded privately to Ariadne's MCP
process and is never included in profile inspection or model-visible results.

The backing repository remains Codex's working directory for now, but the prompt
explicitly directs Iris to use semantic memory rather than inspect or edit its
storage. Ariadne alone validates, writes, commits, pulls, and pushes knowledge
mutations.
