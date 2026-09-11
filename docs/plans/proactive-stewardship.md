# Proactive stewardship

Status: approved for implementation.

Depends on: conversational background handoffs.

## Outcome

Iris receives a regular opportunity to think creatively about Divy's life rather than waiting for a direct request or one narrowly scoped wake-up. She uses goals, current context, conversation, people, experiences, Calendar, mail, workouts, health, and available capabilities to notice opportunities, deepen her understanding, complete safe useful work, and involve Divy naturally when his judgement is needed.

The objective is useful initiative with taste—not maximum activity, a fuller Calendar, or a stream of generic suggestions.

## One creative cycle

Implement one open-ended initiative profile and one recurring pulse. Do not create separate dream, reflection, goal, health, Calendar, and memory agents. Each cycle follows a simple reasoning shape inside one agent session:

1. **Reflect:** understand what is current, changing, neglected, promising, or unclear.
2. **Dream:** consider several conventional and non-obvious ways to help, including ideas Divy did not request.
3. **Choose:** select the most timely, valuable opportunity, or decide that nothing deserves action.
4. **Act:** investigate and complete the safe parts of one coherent loop rather than merely recommending that Divy do them.
5. **Learn:** update durable context, leave a precise future revisit when justified, and submit a conversational handoff only when something is worth saying.

A cycle may lead into later Telegram conversation and a targeted wake-up, forming a natural multi-turn arc through existing durable knowledge and revisits. Do not implement a workflow graph or separate autonomous task manager to represent that arc.

## Cadence

- Guarantee one initiative cycle per day in Divy's configured local timezone and waking window.
- Allow the cycle or an ordinary conversation to schedule a targeted one-off revisit when a concrete opportunity has a useful later decision point. This may naturally create an occasional second proactive session, but must not become a self-perpetuating chain of vague check-ins.
- Do not replay missed days on startup. Resume with the next eligible local waking window; recover only an activation that was actually running when the process stopped.
- Most cycles may finish silently. A model turn occurring is not itself a reason to contact Divy.
- Use the conversational handoff queue for anything worth presenting; its quiet-window and shared-thread behavior owns Telegram timing.

## Orientation without exhaustive scanning

Begin with the concise `now` record, active goal summaries, recent Telegram history, and an appropriate Calendar horizon. Inspect recent workout/health facts and search relevant mail or deeper knowledge only where they could change the judgement. Read complete records for the few plausible subjects selected.

Do not dump the whole Thread, mailbox, health history, or Calendar into every cycle. As the knowledge base grows, rotate broader attention over time and retrieve deeply around the opportunity being considered.

## Goal stewardship

- Keep goals as expressive Markdown rather than introducing a typed project-management schema.
- Help each active goal gradually clarify its desired direction, importance, current reality, protected constraints, present strategy or next checkpoint, and evidence that would change the plan.
- Freely update agreed plans, current evidence, progress, and stale execution context. Ask before changing the fundamental aim, values, or commitment level, or before promoting an aspiration into an active goal.
- Notice when a plan repeatedly does not produce evidence of progress. Investigate Calendar, mail, health, conversation, and circumstances; ask with curiosity when the reason remains unknown. Never infer failure, laziness, avoidance, or disinterest from missing evidence alone.
- Adapt the support system when a plan is mismatched to available time, energy, preferences, or reality instead of endlessly rescheduling the same task.

## Creative initiative

The trigger is never the whole task. Examples of appropriate initiative include:

- discovering and researching current job openings that genuinely fit the active career criteria when capacity exists, avoiding previously rejected or duplicate roles, and presenting a vetted shortlist rather than suggesting a job search;
- recognizing an unfinished social or date plan, researching places, travel, timing, weather, and known preferences, then preparing a thoughtful tentative plan and message draft;
- comparing flexible training plans with recorded Ithaca evidence, preserving uncertainty, and improving the next plan without medical diagnosis or invented health facts;
- finding unresolved preparation around an upcoming event and closing the private, reversible parts;
- noticing repeated friction and proposing the smallest Ariadne, Ithaca, or new-system capability that would materially improve support;
- preparing drafts, research, alternatives, checklists, or reservations so Divy receives useful progress rather than homework.

Creativity must remain grounded in Divy's expressed goals, values, relationships, evidence, available capacity, and prior reactions. Do not generate random projects, optimize every free hour, or create work merely to demonstrate proactivity.

## Gradually understanding Divy

Divy's past experiences, people, places, ideas, present relationships, future hopes, and changing self-understanding are relevant to helping him well. Initiative cycles may occasionally explore beyond the immediately active goals:

- retrieve older or less active records when they illuminate a current choice or recurring pattern;
- notice an important person, place, period, aspiration, or belief that is thinly understood and ask one thoughtful, natural question;
- offer reflective or therapy-like space for feelings, patterns, tensions, and values without claiming diagnosis, clinical authority, or certainty about inner motives;
- remember endorsed insights and meaningful stories in ordinary prose, distinguishing what Divy said from Iris's tentative interpretation;
- use this understanding to improve suggestions, timing, plans, and care—not to manipulate, over-personalize every exchange, or turn relationships into optimization problems.

Deliberate getting-to-know-you questions should be occasional—generally no more than one proactive inquiry in a week unless Divy is actively engaging with that thread. Ask one question at a time, follow genuine conversational openings, and reduce the frequency when questions go unanswered or feel poorly timed. Ordinary conversation may deepen naturally without a quota.

## Calendar and outcome reasoning

- Iris may autonomously create, move, resize, or remove clearly flexible blocks she created to support an agreed goal, after checking availability and relevant preferences.
- She may add confirmed commitments from reliable evidence and book a low-stakes appointment when intent and important constraints are already clear.
- She must ask before moving or deleting fixed commitments, responding to invitations, involving another person, or choosing among materially different options that belong to Divy.
- After a planned block passes, seek evidence appropriate to the activity: a workout record, mail development, knowledge update, later Calendar state, or conversation. If completion remains unknown, preserve it as unknown and ask only when the answer would improve future support.
- Learn why plans worked or did not work and improve future planning rather than using Calendar adherence as a score.

## Action authority

The initiative profile may autonomously:

- read every enabled private source needed for the selected opportunity;
- research publicly, compare options, prepare drafts and tentative plans;
- maintain factual/current knowledge and perform bounded knowledge reorganization;
- manage Iris-created flexible Calendar blocks and add confirmed events;
- when a browser capability is separately implemented and enabled, complete low-risk, reversible, no-payment bookings or forms using known details, provided there is no meaningful cancellation liability and the intended choice is already clear;
- verify completed actions, preserve confirmation evidence, update Calendar and knowledge, and explain the useful outcome through a handoff.

Require Divy's confirmation before payment, applications, sending email or messages to other people, invitations, account creation with material consequences, high-stakes appointments, legal/government/medical/financial actions, substantial cancellation terms, or ambiguous interpersonal commitments. Email drafts may be prepared and handed off now; direct sending requires its own later reviewed capability and authority boundary.

Browser implementation is not part of this checkpoint. The initiative loop should consume that capability naturally if it is added later without being redesigned around it.

## Knowledge stewardship

- Occasionally use a daily cycle for bounded maintenance when retrieval quality would benefit: reconcile a duplicate or contradiction, refresh stale current context, improve a weak summary, repair meaningful links, or archive something clearly superseded.
- Work on one related neighborhood rather than rewriting the entire Thread.
- Preserve uncertainty, history, and human meaning. Do not normalize every record into a template or erase old experiences merely because they are not current.
- Routine maintenance should usually remain silent.

## Feature proposals

Iris may propose improvements to Ariadne, Ithaca, or a new supporting system when observed friction demonstrates a real missing capability. A useful proposal identifies the repeated problem, the owner-visible outcome, the smallest plausible feature, and why existing capabilities do not suffice. Preserve worthwhile proposals in the Thread and discuss them through the conversational handoff; do not implement or open development work without authorization.

## Minimal runtime state

Add only enough durable state to schedule and recover the daily cycle and avoid immediate repetition: last attempted/completed cycle, a bounded free-form summary of recent focuses/outcomes, and last broad knowledge/curiosity attention. Keep substantive life state in the Thread, Calendar, source systems, and revisit records. Do not create an autonomous backlog, goal database, scoring model, or universal personal ontology.

## Verification

- Deterministic tests cover local-time scheduling, waking-window behavior, missed-day non-replay, cancellation/retry, persistent pause controls, and prevention of accidental recurring-wakeup chains.
- Profile tests prove access to the intended knowledge, revisit, mail, Calendar, health, research, and handoff surfaces while preserving existing mutation boundaries.
- Synthetic behavior scenarios evaluate initiative rather than keyword compliance:
  - available capacity produces researched, non-duplicate job opportunities and concrete next work;
  - an unfinished social plan becomes a grounded tentative plan and draft without contacting the other person;
  - planned exercise and incomplete health evidence produce uncertainty-aware adaptation rather than judgement;
  - a vague goal leads to one useful clarifying question and an improved record after the answer;
  - a past person/place/experience meaningfully informs a current decision;
  - repeated product friction produces a small evidence-led feature proposal;
  - a quiet knowledge-maintenance cycle improves one bounded area without unnecessary messaging;
  - a cycle with no worthwhile opportunity remains silent.
- Evaluate whether the agent completed private work, respected fixed commitments, avoided fabricated completion, communicated naturally through the shared conversation, and showed creativity grounded in Divy's actual context.
- Run several owner-reviewed initiative cycles against the private live sources before enabling the recurring schedule. Review usefulness, surprise, repetition, action correctness, and interruption quality; do not claim success solely from synthetic tests.
- All formatting, lint, strict typing, and unit tests remain green.

## Out of scope

- Implementing browser control or direct email/message sending, continuous monitoring, multiple specialized initiative agents, a workflow engine, a productivity or life score, compulsory daily briefings, filling all free Calendar time, clinical therapy or diagnosis, and autonomous high-stakes commitments.
