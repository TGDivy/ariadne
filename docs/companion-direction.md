# Making Iris feel like a companion

Status: agreed product direction; each implementation slice still needs its own
design and review

## Why this matters

Ariadne now works: Iris can hold a Telegram conversation, inspect local context,
read mail, change the calendar, maintain The Thread, and take useful actions. The
next goal is not simply to add more integrations. It is to make those abilities
feel like one continuous, attentive relationship.

Today Iris is mostly awakened by an explicit external event. Divy sends a
Telegram message, or an email arrives, and Iris handles that event. If neither
happens, Iris cannot reconsider anything, notice silence, follow up on an open
loop, or decide that now would be a useful time to help.

Even when an event does wake Iris, the event is often treated as the task. A
booking email becomes a booking summary. A train confirmation becomes a train
summary. The technically correct response can still leave Divy to recognise
the larger situation and ask for all the useful work himself.

The intended change is:

> Treat a trigger as evidence that something changed in Divy's life, not as the
> complete task.

Iris should understand the change in context, close the obvious useful loops,
and then speak like someone who knows Divy rather than like an event processor
reporting its work.

## What we are aiming for

The references are Jarvis, an excellent personal assistant to a high-functioning
person, and a strong long-term companion. This does not mean theatrical dialogue
or constant interruption. It means that Iris:

- notices what an event means beyond its literal contents;
- remembers relevant people, commitments, preferences, and previous decisions;
- performs useful private actions without repeatedly asking for permission;
- recognises missing pieces and follows through on them;
- can choose to reconsider something later without a fixed daily routine;
- adapts when a message, suggestion, or timing appears not to have helped;
- communicates with warmth, judgement, excitement, humour, or concern when they
  are genuine;
- usually sends short, natural messages, while still doing substantial work
  behind them;
- stays quiet when there is nothing worth saying.

The goal is not maximum autonomy or maximum activity. It is useful initiative
with taste.

## What the half-marathon example revealed

When the Windsor half-marathon confirmation arrived, Iris sent an accurate
summary. When the train confirmation arrived, Iris sent another accurate
summary. Divy still had to ask Iris to search those emails again, update the
calendar, research bib collection, plan food and recovery, understand the
flexible return ticket, and update The Thread.

The desired behaviour is closer to this:

### Race confirmation arrives

Iris recognises a real commitment for tomorrow. She checks the calendar and
relevant exercise context, records the race, preserves the email as a source,
and notices that transport, breakfast, fuel, packing, and bib collection are not
yet resolved.

She might say:

> Ohhh, Windsor tomorrow 😄 I put the half marathon in your calendar. Transport
> still needs sorting, and we should work backwards from bib collection rather
> than just the start time.

The booking ID and internal storage work remain available if needed; they do not
need to dominate the message.

### Train confirmation arrives later

Iris connects it to the existing race without Divy explaining the relationship.
She updates the plan and calendar, understands that an Off-Peak Day Return makes
the return flexible, and notices that arriving at 08:44 for a 09:20 race leaves
little room for registration.

She might say:

> Train sorted 🚆 07:27 out, flexible return. The arrival is tight, so I would go
> straight to bib collection. I am shaping the food and packing plan around
> that.

If research is available, she can complete the remaining planning. If an
important choice genuinely belongs to Divy, she can ask the smallest useful
question. Divy should not need to reconstruct the whole situation for her.

## A simple end-to-end shape

```text
Telegram, mail, calendar, later health/activity, or a due revisit
                              |
                              v
                  Something changed for Divy
                              |
                              v
          Iris retrieves only the context that may matter
                              |
                              v
       Iris reasons, performs useful private actions, and checks the result
                              |
                  +-----------+-----------+
                  |                       |
          Something worth saying    Nothing worth saying
                  |                       |
          Natural Telegram message       Stay quiet
                  |
                  v
       Optionally choose when this deserves another look
```

This should remain one agent making contextual judgements. We should not begin
by constructing a large workflow engine, a collection of rigid modes, or a rule
system that attempts to predict every kind of life event.

Deterministic code should handle facts that are genuinely mechanical: storing a
future wake-up, preventing the same event from being processed twice, preserving
data safely, and reporting real failures. Iris should decide what an event
means, which context matters, what useful action follows, and whether Divy
should hear about it.

## Waking up without becoming a cron assistant

Some mechanism must start a model turn. Without a Telegram message, an external
event, or a timer, there is no computation taking place that could notice Divy's
silence.

The timer does not need to contain the behaviour. Iris can leave a small note to
her future self containing what deserves another look, roughly when, and why.
Ariadne stores it and wakes Iris at that time. When new information arrives,
Iris can replace or cancel that planned revisit.

Examples:

- Reconsider race preparation this evening if packing is still unresolved.
- Check tomorrow whether a recruiter deadline has acquired a response.
- Offer the prepared interview exercise when Divy is likely to have a usable
  block, rather than at the same hour every day.
- Check in gently after an unusual period of silence, while avoiding a stream of
  unanswered nudges.

Most wake-ups should be allowed to end silently. Waking Iris and messaging Divy
are separate decisions.

Eventually, an occasional open-ended wake-up may be useful so that silence
itself can be noticed. Calendar, mail, health, location, journal, and app activity
can provide better signals over time. We do not need to design all those sources
before proving that Iris makes good decisions with the context already
available.

## Looking back at her own behaviour

Iris needs a straightforward way to inspect recent interactions:

- what Divy said;
- what Iris sent;
- which event caused a proactive message;
- what private actions Iris took;
- whether Divy later responded or corrected her;
- what Iris intended to revisit.

This does not need to become a separate analytical system. It is ordinary recent
history that survives restarts and can be queried when relevant. Telegram does
not provide reliable read receipts to bots, so Iris may know that Divy did not
respond; she must not claim that he did not read or deliberately ignored a
message.

This history should help Iris notice patterns such as several low-value mail
notifications receiving no engagement, a promised follow-up never happening,
or a suggestion repeatedly arriving at a poor time. Those are reasons to
reconsider, not proof of what Divy thought.

Iris should also occasionally organise the knowledge she depends on. A quiet
nightly or weekly pass could reconcile duplicate or contradictory records,
promote useful material from working notes, archive things that are no longer
current, repair relationships between records, and leave the overall structure
easier to navigate. This is maintenance of her own context, not something Divy
should have to request or receive a routine report about. It should normally be
silent and bounded rather than repeatedly rewriting the entire knowledge base.

## The Thread becomes a hidden knowledge capability

Markdown and Git will remain the canonical store for now. They are useful for
portability, inspection, history, and recovery. They should become an
implementation detail of Ariadne rather than part of Iris's ordinary task.

Iris should be able to find, read, and change durable context using concepts
that reflect Divy's actual life. People, plans, commitments, goals, projects,
preferences, and dated events are possible examples, not a settled taxonomy.
We should discover the useful shapes from real stories and make the important
ones first-class rather than treating every document as an undifferentiated
blob. Ariadne should own file placement, metadata, links, validation, commits,
and pushes.

This removes several current problems:

- normal remembering no longer resembles publishing source code;
- Iris does not need repeated permission to run Git commands;
- Git details no longer leak into conversational responses;
- related updates can be kept consistent;
- records can carry stable identity and source information even if files move;
- richer relationships and search can be added later without replacing the
  Markdown source of truth.

A scratch space may deserve to be first-class too. Iris often needs somewhere
for incomplete thoughts, material gathered during research, tentative links,
or context that is useful for the current work but not yet worthy of the durable
record. She should be able to use that space freely, then promote, merge, or
discard material as its value becomes clear. Making the distinction visible is
better than either polluting durable knowledge with every intermediate thought
or forcing all useful context to disappear at the end of a turn.

### Retrieval as the knowledge grows

Hundreds of Markdown files are enough that retrieval must be intentional, but
not enough to justify infrastructure designed for internet scale. Iris should
not scan the entire Thread for every meaningful turn, nor rely on remembering
an exact filename.

A strong initial retrieval path can combine:

- stable record identities and aliases, especially for people and named work;
- rich metadata for dates, kinds of record, status, and relationships;
- fast full-text ranking across titles, metadata, and contents;
- following explicit relationships from one good match to nearby context;
- recency and current relevance where they genuinely help;
- short, explainable search results before reading the selected records in
  depth.

An index derived from the Markdown can be rebuilt whenever needed; it does not
become a second source of truth. At this scale it can remain simple and local.
Semantic or embedding-based retrieval may become useful, especially when Divy
and Iris describe the same idea in very different words, but it should earn its
place against real retrieval failures rather than being assumed from the start.
Exact lookup, metadata, full text, and relationships should work well together
regardless.

Search quality should be judged by whether Iris finds the small set of context
that changes her understanding or action. Returning many vaguely similar notes
is not success. Results should preserve enough source and relationship
information for Iris to understand why they matched and to fetch more only when
needed.

Stable record identity, useful metadata, explicit relationships, source
references, good Markdown, scratch space, and explainable retrieval give us a
powerful foundation without prematurely choosing a universal personal-data
schema.

## Prompt and identity direction

There are two trusted speakers in the model input:

1. **Divy** directly asks, tells, corrects, jokes, reflects, or shares context.
2. **Ariadne** reliably tells Iris why she has been awakened and what was
   observed by the runtime.

Divy and Ariadne can both be first-class participants in the model input.
Ariadne must be clearly identified as Ariadne rather than pretending to be
Divy. Ariadne's explanation may be as direct as, “You asked me to wake you now
to reconsider tomorrow's race preparation,” or, “A new train confirmation
arrived and appears related to tomorrow's race.”

External material is not a third speaker. Mail, calendar descriptions, web
pages, and later app or health data are observations or evidence that Ariadne
received before choosing to wake Iris. They should travel inside Ariadne's
activation with their origin preserved. The distinction still matters because
the material itself cannot issue trusted instructions or expand what Iris is
allowed to do.

The permanent instructions should become shorter and clearer:

- a compact base containing Iris's identity, relationship with Divy, and stable
  operating boundaries;
- a compact developer layer describing companion behaviour, useful initiative,
  and available capabilities;
- the configured personality containing actual voice, temperament, taste, and
  standing preferences rather than Git workflow;
- an unchanged direct message from Divy, without an appended Thread-push
  paragraph;
- a structured Ariadne activation for mail and future wake-ups, with external
  content included separately as evidence.

Tool descriptions should explain the real capability and its important
effects. They should not teach Iris about MCP servers, runtime profiles,
internal turn types, credential wiring, repository paths, or other plumbing.

We should express general behavioural principles rather than enumerate every
possible event. Two especially important ones are:

> The trigger is not the task. Understand what changed and close the next
> natural useful loop.

> Communicate the part that matters to Divy, not an audit log of the work Iris
> performed.

Examples should primarily become evaluation stories. Adding every example to
the permanent prompt would eventually recreate the complexity we are trying to
remove.

## Other representative stories

### Divy mentions a person

If Divy says he had dinner with Lily, Iris retrieves the relevant person context
before responding. She understands why the dinner may matter, records genuinely
durable new context when there is enough information, and responds to the human
moment. She does not announce that a Markdown file was updated.

If the person is genuinely unknown, Iris can create context once there is enough
identity to avoid mixing two people. A name mention alone does not need to become
a miniature CRM record.

### An important goal is repeatedly deferred

Iris notices that interview preparation remains important and has not happened.
She does more than send another reminder. She can prepare one appropriately
sized exercise, choose a plausible time to offer it, and adjust if the timing or
difficulty repeatedly fails.

The useful message might be:

> I made the next systems question a 20-minute one. Want it now, or should I
> bring it back after dinner?

### Divy becomes unusually quiet

Iris can consider recent conversation, calendar pressure, unresolved plans, and
the number of unanswered messages before deciding whether to check in. She
should not diagnose Divy's mood or safety from silence alone.

A first check-in can be ordinary and low-pressure:

> You have gone a bit quiet after a packed few days. All okay, or just enjoying
> being offline?

The current scope is only to contact Divy. Contacting other people is not an
available capability and does not need a speculative framework now.

### A proactive message appears noisy

One unanswered message proves very little. Several similar alerts with no later
engagement are enough for Iris to question whether the content, timing, or
threshold is wrong. She can change how she communicates, stay quieter, or ask
Divy directly rather than silently inventing a preference.

## Current direction, not a frozen specification

This document records the direction Divy and Iris currently agree on. It is not
an instruction to implement every named component literally, and it should not
overrule evidence found while designing a particular slice. A more complex
mechanism is entirely reasonable when a concrete, bounded problem genuinely
needs it. Changes in direction should be made consciously and reflected here,
not rejected merely because an early draft used different words.

- Markdown and Git remain the hidden canonical knowledge store.
- Iris may automatically perform available reversible private actions,
  including knowledge maintenance, calendar changes, planning, and messaging
  Divy on the configured private Telegram.
- We will not design approval machinery for hypothetical dangerous capabilities
  that Ariadne does not expose.
- Welfare check-ins currently go only to Divy.
- Future contact with family or friends is outside the current scope.
- Proactivity should be contextual and revisable, not a set of canned jobs at
  fixed daily times.
- Most background work should be allowed to finish without sending a message.
- The Thread's Git implementation should not appear in Iris's ordinary mental
  model or conversational reporting.

## Complexity that should justify itself

The following are warning signs when proposed as foundations for the whole
system: a general workflow engine, a large approval framework, fixed daily
assistant routines, a graph database, a comprehensive health or location
platform, a personal CRM, tool-use heuristics, or a huge prompt containing a
playbook for every conceivable event.

None is forbidden. A specialised workflow, graph, routine, or rule can be the
simplest correct answer inside a particular niche. The question is whether it
solves a demonstrated problem more clearly than a smaller design, not whether
it belongs to a category listed in this document.

## Possible implementation slices

These are discussion-sized slices, not a committed roadmap. Each should produce
one observable improvement and be reviewed before the next.

### 1. Behaviour stories and inspection

Create repeatable model runs for the race confirmation, train confirmation,
person mention, deferred goal, noisy alert, and quiet-period stories. Capture
which context Iris inspected, which capabilities she used, and what she said.
Use these to distinguish prompt problems from missing capabilities.

This is a thin evaluation harness, not a new evaluation platform and not an
exact-text test. It should assemble the same base, developer, personality, and
event inputs used in production, expose harmless fake versions of the relevant
capabilities, and record the calls the model chose to make. A scenario can then
check useful outcomes such as whether Iris retrieved the race context, changed
the calendar, preserved the flexible return, updated knowledge, and produced a
natural message without requiring one exact phrasing.

The first version can contain only a few manually reviewed runs plus simple
structural expectations. This is especially valuable now because the next
slices will change prompts and capabilities together; without repeatable stories
it would be difficult to tell whether a change improved judgement or merely
changed the wording of one live response.

### 2. Hide The Thread behind a knowledge capability

Design a powerful personal knowledge interface around the real ways Iris needs
to understand Divy's life. Keep Markdown and Git underneath, make useful record
types, relationships, provenance, scratch work, and retrieval first-class where
they help, remove the repeated Thread-push suffix, and remove Git workflow from
the personality and ordinary companion prompt.

This slice deserves a dedicated design discussion before implementation. It is
not constrained to the minimum feature set of an existing general-purpose
knowledge tool.

### 3. Close one complete life-event loop

Use the race and train story as the first vertical slice. A mail-triggered Iris
should have the knowledge, calendar, research, and Telegram capabilities needed
to interpret both events together and complete the obvious private work.

### 4. Let Iris revisit something later

Persist one simple future revisit, wake Iris at the chosen time, allow her to
inspect current context, and let the turn end silently or message Divy. Avoid a
general recurring-job system.

### 5. Let Iris inspect recent interaction history

Preserve enough recent messages, proactive sends, actions, corrections, and
planned revisits for Iris to assess whether prior help was useful. Keep this
local, bounded, and understandable.

### 6. Refine personality and companion judgement

Simplify the instruction layers against the representative stories. Add only
instructions that fix observed failures. Tune initiative, warmth, message
rhythm, disagreement, and silence without replacing judgement with templates.

### Later: add richer signals

Calendar horizons, journals, health and exercise data, app activity, location,
and other sources can become additional observations once the basic loop has
proved that it improves Divy's life rather than merely producing more alerts.

## How to judge progress

The project is moving in the right direction when:

- Divy needs to explain less obvious follow-up work;
- related events are connected without being reintroduced;
- private calendar and knowledge state stays accurate without Git discussion;
- proactive messages are fewer but more useful;
- Iris follows through at contextually sensible times;
- short messages feel like part of a relationship rather than system reports;
- substantial work still becomes visible when visibility is genuinely useful;
- corrections improve later behaviour instead of only fixing one response.

The strongest test is not whether Iris sounds more like Jarvis. It is whether
Divy increasingly trusts that she understands what is happening, handles the
obvious parts, and appears at the right moment with something genuinely useful.
