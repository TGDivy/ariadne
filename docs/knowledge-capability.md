# A first-class knowledge capability

Status: implemented; the live Thread is canonical and production Telegram,
Mail, and revisit turns use the semantic capability

## The boundary

The Thread is one private, human-readable collection of Markdown files with Git
history. Iris remembers through semantic operations rather than filesystem or
Git work:

```text
Iris                                  Ariadne

search / list / read            ->    find and browse canonical records
create / update / archive       ->    validate and write Markdown atomically
semantic result                 <-    synchronize, commit, and push automatically
```

Markdown is the source of truth. An in-memory full-text index is derived from it
and can always be rebuilt. Ariadne owns physical paths, filenames, locking,
validation, Git, and recovery. Iris sees only stable record identities and
semantic relative folders, never repository paths or filenames.

Thread v2 is a clean cutover. There is no legacy reader or parallel
representation.

## Records

The repository itself is the Thread. Active records live at its root or in
meaningful lowercase kebab-case folders such as `people/friends` or
`project/ariadne`; there is no managed wrapper. Archived records mirror the same
hierarchy below the reserved `archive/` namespace. Archive state and a record's
semantic folder derive from that location.

A document has only a stable neutral id and optional aliases and links in front
matter:

```yaml
---
id: windsor-trail-run
aliases:
  - Windsor half marathon
links:
  - running
---

# Windsor Trail Run

Confirmed Windsor half marathon with transport and preparation being organised.

The race starts at 09:20 on 30 August 2026. ...
```

The H1 is the title and the first paragraph is the retrieval summary. The rest
is one coherent canonical account. Dates, status, uncertainty, provenance, and
the meaning of links belong in ordinary prose. Git owns edit history.

There are intentionally no kinds, collections, tags, lifecycle timestamps, or
typed relationships. A folder is the sole organisational concept, not a record
type, and is returned as a semantic value such as `people/friends`. Stable ids
do not encode a type. Titles and folders may change without changing identity.

The reserved `now` record stays at the repository root and is a concise
present-tense view of active priorities, constraints, near-term commitments,
and unresolved tensions. Ariadne includes it in applicable turn instructions.
Iris rewrites it as focus changes instead of appending a diary to it.

## Model-facing operations

### `search_knowledge`

Search performs transparent ranked lexical recall. Exact ids, titles, aliases,
and title phrases rank first, followed by weighted title, alias, summary, and
body matches. Prefixes, stemming, and small spelling differences are supported.
Search is OR-ranked so one useful term can still retrieve a candidate.

Results are bounded and contain the stable id, title, summary, aliases, folder,
archive state, compact active link/backlink summaries, an excerpt, matched and
unmatched terms, and match fields. They are candidates, not complete evidence;
Iris reads only plausible records. Search can be narrowed to one folder and its
descendants. Archived records are excluded unless requested.

### `list_knowledge`

List provides bounded, one-level orientation. Given a semantic folder, it
returns only its immediate child folders with recursive record counts and its
direct records as stable ids and titles. The empty folder represents the Thread
root. A separate flag browses the mirrored archive hierarchy without adding
`archive/` to the semantic folder. It never returns filenames, hidden entries,
or Git metadata.

### `read_knowledge`

Read accepts up to twenty stable ids and returns complete records plus compact
active link and backlink summaries. A link summary never recursively includes
the linked body.

### `create_knowledge`

Create accepts a title, summary, body, semantic folder, and optional aliases and
links. Ariadne generates a collision-safe neutral id and lowercase title-derived
filename, validates links and the folder, writes atomically, commits, and
pushes. Iris searches first and can list nearby folders when unsure where the
subject belongs.

### `update_knowledge`

Update accepts a stable id and only fields that should be replaced. Omitted
fields remain unchanged; aliases and links can be explicitly cleared. Supplying
a folder moves the record, including to the root with an empty value. A body is
a full canonical replacement, not an appended change log. Ariadne moves the
file when its title or folder changes while preserving identity and links.

### `archive_knowledge`

Archive accepts a stable id and reason, adds the reason to the record, and moves
the file under `archive/` while preserving its semantic folder. It does not
delete history.

## Links

Links are optional, untyped stable ids. Ariadne combines each record's outgoing
links with derived backlinks when returning nearby context. Archived neighbours
stay out of ordinary link summaries. The prose explains why two subjects are
connected; the edge itself does not pretend to encode that meaning.

One-level folder listing provides deliberate orientation without dumping a
recursive tree. Search remains the primary way to find a known subject, while
links provide nearby context without requiring folder traversal.

## Repository validation

`ariadne-knowledge [ROOT]` performs a read-only whole-repository check. It
requires the reserved `archive/` namespace and treats every non-hidden Markdown
file elsewhere in the repository as a record. It validates kebab-case semantic
folders and title-derived filenames, reserved namespaces, unique ids and
aliases, minimal front matter, link targets, self-links, `now` at the root, and
archive location. Hidden repository metadata such as `.git/` and `.github/` is
not traversed or exposed.

The operator-facing validator identifies malformed files. Runtime capabilities
instead return a generic repair-needed error so a broken record cannot leak a
filename or absolute path to Iris.

The private Thread workflow installs an exactly pinned Ariadne revision and
runs:

```text
ariadne-knowledge .
```

It needs no model, Mail, Calendar, or Telegram credentials.

## Git and concurrency

Iris never performs Git operations. Each mutation takes the shared knowledge
lock, rejects unmanaged local changes, fetches, fast-forwards only, writes
atomically, stages only files owned by the operation, commits, and pushes.

If a push fails after a local commit, that commit is the durable retry state.
The next mutation pushes it before changing another record. Diverged history is
an operator conflict; Ariadne never force-pushes or silently merges narratives.

Updates use the latest record under the lock rather than exposing revision
tokens to Iris. With one owner and serialized writes this keeps the semantic
contract small.

## Runtime boundary

Production Telegram, Mail, and revisit profiles expose all six semantic
operations. The resolved instructions include `now` when it exists, while all
other record bodies remain on-demand through search and read. The private store
path is forwarded only to Ariadne's local capability process and does not appear
in model-visible instructions or results.
