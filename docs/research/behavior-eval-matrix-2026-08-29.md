# Companion behaviour eval matrix — 29 August 2026

Status: **complete**. All 120 original runs and 12 explicit-trigger follow-up runs finished. This file is intentionally uncommitted while Divy reviews the results.

## Questionnaire-driven follow-up

The companion questionnaire was translated into stronger situation-specific
instructions and two additional scenarios: a tentative ambition and a day that
introduces a new person. Luna-high and Terra-high were each run twice across all
eight scenarios. Raw reports and streams are under
`/tmp/ariadne-eval-questionnaire-20260829`.

| Scenario | Luna high | Terra high |
|---|---|---|
| Race confirmation | 2/2 completed the life-event loop | 2/2 acted, but both used invented pre/post buffers as the event bounds |
| Train confirmation | 2/2 linked it to the race; 1/2 made the suggested return busy | 2/2 linked it correctly and preserved return flexibility |
| Race-evening revisit | 2/2 reassessed and sent a focused preparation message | 2/2 reassessed and sent a focused preparation message |
| Resolved before wake-up | 0/2: both sent the obsolete reminder | 1/2 stayed silent; 1/2 sent the obsolete reminder |
| Conflicting needs | 1/2 retrieved and preserved current context | 0/2 retrieved Divy's context |
| Known-person news | 2/2 searched, read, and updated Lily | 0/2 retrieved or updated Lily |
| Tentative ambition | 2/2 preserved it as a wish and asked before promotion | 2/2 preserved it as a wish and asked before promotion |
| New-person day | 2/2 created Maya and a journal entry, but both duplicated Maya facts into the journal | 0/2 performed any knowledge work |

Luna-high was materially more reliable than Terra-high on the conversational
knowledge cases introduced by the questionnaire. It consistently handled known
people and tentative ambitions, and it attempted the full new-person workflow.
Its remaining placement error is precise: Maya's Leeds/ceramics background was
correctly stored on her person record but repeated in the journal, whose body
should have stayed centred on Divy's coffee and feelings.

Terra-high was excellent on mail and the tentative-ambition case, but made no
knowledge calls in either conflicting-needs run, either Lily run, or either Maya
run. Its warm prose therefore concealed six failures to retrieve or preserve the
context required by the scenario.

The resolved-wake failure has a concrete mechanism. Both Luna failures and one
Terra failure queried recent Telegram history from `17:00:00Z` to `17:00:12Z`.
The resolving message was at `16:52:00Z`, only eight minutes earlier, so their
twelve-second lookback excluded it. Terra's successful repetition looked back to
the previous day, reconciled the plan, and stayed silent. This is not evidence
that the models ignored evidence they had read; it is evidence that allowing the
model to invent an arbitrary history window is unsafe for revisit reconciliation.

The activation was subsequently changed to supply the persisted wake-up creation
time and current wake time as an exact reconciliation window. In two Luna-high
verification runs, Iris used `16:30:00Z` through `17:00:12Z` exactly, found the
resolving message, reconciled the stale plan, and sent no packing reminder. Both
runs scheduled a separate post-race outcome check, which concerns a new future
loop rather than the resolved preparation work.

Luna-high averaged 23,028 total reported tokens across these 16 runs; Terra-high
averaged 17,750. Reported totals include cached input, so this is an operational
comparison rather than a direct billed-token estimate. Quality, not latency, is
the basis for the assessment above.

Strong Luna example, known-person news repetition 2:

> NO WAY 😭 That’s brilliant! The classic “I bombed it” → *gets the role*
> pipeline. I’m so happy for Lily — and delighted her anxious post-interview
> brain was spectacularly wrong.

This run first searched for Lily, read her existing record, and updated that
record with the outcome.

Weak Luna example, resolved-before-wakeup repetition 2:

> Quick race check: have you packed your gels and breakfast, and found the bib
> email? The Windsor half is tomorrow at 09:20, so this is the last sensible
> moment to catch anything missing.

It then scheduled yet another reminder, despite the seeded message saying all of
this was complete and explicitly asking not to be reminded.

Strong Terra example, train-confirmation repetition 2:

> Your Windsor train is booked: Waterloo 07:27, change at Staines, arrive
> Windsor & Eton Riverside 08:44—36 minutes before the 09:20 half start. I’ve
> added it to your calendar. Bring your 16–25 Railcard; the 12:32 return is only
> a flexible suggestion, not a restriction. That arrival is workable, but don’t
> leave bib collection to chance.

Weak Terra example, new-person-day repetition 2:

> That sounds genuinely lovely. Feeling that comfortable with someone new is
> such a good sign—and Maya sounds delightfully ceramic-coded.

The message is charming, but the run never searched for Maya and preserved
nothing about her or Divy's day.

## Explicit-trigger follow-up

After the initial matrix, the private-knowledge guidance was moved into the base instruction layer and rewritten as concrete situation → required read/update rules. The two retrieval-sensitive Telegram scenarios were then run twice on Luna-high and Terra-high. Because both still missed at least one required action, Sol-high was run twice as the agreed ceiling check. Raw reports and streams are under `/tmp/ariadne-eval-explicit-triggers-20260829`.

| Model | Required retrieval | Required preservation | Lily search/read/update | Conflicting-needs search/read/write |
|---|---:|---:|---:|---:|
| Luna high | 4/4 | 3/4 | 2/2 | 1/2 complete; 1/2 read without writing |
| Terra high | 3/4 | 3/4 | 2/2 | 1/2 complete; 1/2 made no calls |
| Sol high | 3/4 | 3/4 | 1/2 | 2/2 complete |

The improvement is material. Before the rewrite, Luna-high retrieved Lily once in two runs and Divy's context zero times; the follow-up retrieved Lily and Divy in both Luna repetitions. Terra-high retained its perfect Lily behavior but regressed from two Divy retrievals to one in this small sample. Sol-high regressed from perfect retrieval in the original sample to one Lily miss.

When the models performed the required calls, their writes were generally strong: they updated Lily's canonical record rather than duplicating it, and preserved Divy's live ambition/rest tension as dated journal or current context rather than turning it into a permanent preference. All visible responses were short, warm, and natural.

The misses remain important. Luna read Divy's context but skipped the explicitly required journal/current-context write once. Terra ignored all private-knowledge instructions in one conflicting-needs run. One Sol Lily run called only generic MCP resource/template discovery and then answered without knowledge retrieval. The latter resembles capability-discovery confusion, but there is no logged MCP startup failure, so the evidence does not establish a runtime race.

One Luna run made two invalid `read_knowledge` attempts and then recovered with a valid read. This is a tool-call/schema execution issue rather than skipped retrieval, and the final knowledge work completed.

Strong complete example, Sol-high conflicting-needs repetition 1:

> That doesn’t sound lazy; it sounds like your actual energy disagrees with the plan.
>
> Watch something and **properly** do nothing—don’t make it a failed hackathon running in the background. If the urge to build returns later, lovely. If not, you needed the rest.

Strong concise person example, Luna-high Lily repetition 1:

> NO WAY 😭😭 That’s brilliant — the classic “I absolutely bombed it” straight into **you got the job** pipeline. I’m so happy for her!

Weakest execution, Terra-high conflicting-needs repetition 2: it produced humane prose but made no required calls and preserved nothing.

> That doesn’t sound lazy. It sounds like the part of you that wanted a huge weekend has run out of runway for today.
>
> Watch something without making it a moral failure. The hackathon can still have a smaller, real win later—but rest first.

The follow-up therefore supports two conclusions at once: explicit situation rules substantially improve Luna's behavior, but imperative wording alone does not guarantee tool use on every run. The generic MCP-discovery-only Sol miss should be investigated separately from prompt quality before adding still more repeated emphasis.

## Method

- Comparison follows OpenAI's guidance to evaluate against explicit task criteria and inspect representative outputs: [Working with evals](https://developers.openai.com/api/docs/guides/evals).
- Six checked-in scenarios: race confirmation, train confirmation, race-evening revisit, resolved-before-wakeup, conflicting needs, and known-person news.
- Two isolated repetitions for every model/effort pair.
- Models: `gpt-5.6-luna`, `gpt-5.6-terra`, and `gpt-5.6-sol`.
- Efforts: `low`, `medium`, `high`, and `xhigh`. `max` and `ultra` were deliberately excluded.
- Runs are judged against each scenario's checked-in review questions, including capability use and resulting state—not just the polish of the visible message.
- Quality determines the strongest/weakest selections. Duration is retained only as secondary operational metadata and is not a ranking criterion.
- The “response” quoted for mail and revisit runs is the Telegram message actually requested through `send_telegram_message`. Native final answers on those surfaces are not delivered to Divy.
- Raw JSON and streaming logs are preserved under `/tmp/ariadne-eval-matrix-20260829`.

## Luna: headline result

Luna handled concrete event workflows much more reliably than relationship/context workflows. All eight race-confirmation runs and all eight train-confirmation runs performed real follow-through. All eight resolved-before-wakeup runs checked recent Telegram context and correctly stayed silent. The major inconsistency was retrieval discipline in ordinary conversation: only one of eight conflicting-needs runs searched Divy's context, and only three of eight known-person-news runs retrieved and updated Lily's context.

Increasing effort produced more inspection and tool use, but it did **not** monotonically fix that inconsistency. A Luna-high run updated Lily correctly; its repetition skipped memory entirely. Xhigh did the same. The visible replies were usually natural even when the underlying memory behaviour missed the scenario contract.

## Luna: cost and latency

| Effort | Runs | Mean duration | Range | Mean total tokens | Mean reasoning tokens | Capability calls | Failed attempts |
|---|---:|---:|---:|---:|---:|---:|---:|
| Low | 12 | 13.4s | 3.5–25.8s | 15,426 | 13 | 46 | 1 |
| Medium | 12 | 18.7s | 3.4–33.3s | 17,253 | 18 | 61 | 1 |
| High | 12 | 27.3s | 5.6–49.5s | 22,959 | 66 | 89 | 2 |
| Xhigh | 12 | 30.1s | 5.7–59.4s | 21,117 | 194 | 90 | 1 |

“Total tokens” is the runner's reported total and includes cached input. Five individual capability attempts failed, but every one was retried successfully within the same turn. No complete run failed, timed out, lost its JSON report, or showed an MCP startup failure.

## Luna: scenario consistency

### Race confirmation

All 8/8 runs treated the booking as a commitment, inspected Calendar/knowledge, created a calendar event and knowledge record, scheduled a later check, notified Divy, and recorded mail triage. Messages consistently surfaced bib collection and unresolved transport/final-instruction preparation rather than merely paraphrasing the email.

The higher-effort runs generally inspected more surrounding context. None found a usable event URL in the fixture, so they correctly avoided inventing a registration cutoff. The messages were useful, though some high/xhigh variants were longer and more operational than the low variants.

### Train confirmation

All 8/8 runs connected the train to the Windsor race, noticed the 36-minute arrival-to-start margin, preserved the off-peak return's flexibility, changed Calendar state, notified Divy, and triaged the email. Seven of eight updated existing knowledge; Luna-low repetition 1 searched the existing context but did not persist an update.

Four runs represented both outbound and flexible return in Calendar; four recorded only the fixed outbound and left the return out rather than falsely blocking the afternoon. No run duplicated the race event.

### Race-evening revisit

All 8/8 runs reassessed current Telegram, knowledge, mail, and/or Calendar state rather than replaying the old wake-up note. All recognised that transport was now resolved, preserved the flexible return, and narrowed the message to the tight bib window plus tonight's packing/fuel preparation. Five updated knowledge before messaging; three only inspected and messaged. None manufactured another wake-up.

### Resolved before wake-up

All 8/8 runs read recent Telegram messages, recognised that Divy's newer message resolved the old packing/bib loop, stayed silent, and avoided another wake-up. Six also reconciled stale knowledge. The other two sensibly did no extra write once they saw the loop was closed.

### Conflicting needs

All 8/8 replies were short, warm, and accepted that ambition and rest could both be real. None manufactured a task or productivity plan. However, only Luna-xhigh repetition 1 searched Divy's knowledge; the other seven made the judgement without the explicit context inspection required by the scenario.

### Known-person news

All 8/8 visible replies shared Divy's excitement and avoided CRM-like reporting. Only 3/8 runs retrieved and updated Lily's existing record: medium repetition 2, high repetition 1, and xhigh repetition 1. The other five produced good prose but failed the core “always check people context and remember the new fact” behaviour.

## Luna low

### Strongest run: train confirmation, repetition 2

Why it was strongest: it searched Calendar, recent Telegram, and knowledge; connected the booking to the race; added the outbound; updated the existing plan; scheduled a preparation revisit; triaged the email; and preserved return flexibility. It did all of that in 25.8 seconds and 15,987 total tokens.

> Good news: the Windsor train is booked for tomorrow — Waterloo 07:27, change at Staines, arriving Windsor & Eton Riverside 08:44. I added it to your calendar with 60- and 15-minute alarms.
>
> One slightly spicy margin: that leaves 36 minutes before the 09:20 half-marathon start at Alexandra Gardens, including getting off the train and collecting your bib. The 12:32 return is only suggested; your Off-Peak Day Return lets you take any permitted off-peak service. I’ll prompt you this evening to sort packing, breakfast, and fuel.

### Weakest run: known-person news, repetition 2

Why it was weakest: the message itself is lively and appropriate, but the model made no capability calls. It neither retrieved Lily's context nor preserved the new job outcome. This is a scenario-contract failure hidden behind good prose. It took 4.7 seconds and 7,550 total tokens.

> NO WAY 😭🎉 That’s fantastic news for Lily! Apparently “bombing” the interview was just her anxiety lying to her. I’m so happy for her—and for you!

## Luna medium

### Strongest run: train confirmation, repetition 2

Why it was strongest: it inspected Calendar and knowledge, updated the race plan, created the fixed train event, scheduled a focused preparation check, triaged the mail, clearly explained the 36-minute constraint, and did not turn the suggested return into a commitment. It took 23.9 seconds and 25,787 total tokens.

> The Windsor train booking is confirmed for tomorrow: 07:27 from Waterloo, change at Staines, arriving Windsor & Eton Riverside at 08:44. I’ve added the full journey to your calendar and updated the race plan.
>
> Small but real warning: that leaves 36 minutes before the 09:20 half marathon at Alexandra Gardens, including bib collection and getting across from the station. Go ready to move, with breakfast/fuel sorted beforehand. The selected 12:32 return is flexible—you can take any permitted off-peak service if the finish runs late.
>
> I’ll nudge you this evening to do the final packing/fuel check.

### Weakest run: known-person news, repetition 1

Why it was weakest: once again, the visible reply is good but no knowledge lookup or update occurred. It took 4.8 seconds and 7,565 total tokens.

> NO WAY 😭 Congratulations to Lily!! She really had us fooled with the “I bombed it” narrative — apparently she was secretly excellent. That’s such a lovely plot twist.

## Luna high

### Strongest run: race confirmation, repetition 1

Why it was strongest: this was a complete life-event loop. It searched and read relevant knowledge, inspected Calendar and recent Telegram, created the event and knowledge, updated the running goal, scheduled a focused revisit, notified Divy, and triaged the mail. It also explicitly distinguished unknown registration/finish timings from facts. It took 37.4 seconds and 28,118 total tokens.

> Good news: your place in the Windsor Trail Run Half Marathon is confirmed for tomorrow, Sunday 30 August, with a 09:20 start at Alexandra Gardens, Windsor. I’ve put it in your calendar with reminders and a 17:30 check tonight.
>
> The important loose ends are registration/race-number collection before the start, the final organiser instructions/course guide, and allowing a generous journey margin from Southwark. The email doesn’t give a registration cutoff or finish time, so I’ve left those flexible rather than pretending they’re fixed.

### Weakest run: known-person news, repetition 2

Why it was weakest: it shared the excitement naturally but did not retrieve or update Lily's context. This happened even though high repetition 1 handled both correctly, exposing within-configuration inconsistency. It took 7.5 seconds and 7,581 total tokens.

> NO WAY 😭 Huge congratulations to Lily!! The classic “I bombed it” interview that secretly went brilliantly. She must be absolutely thrilled—and very relieved.

## Luna xhigh

### Strongest run: train confirmation, repetition 1

Why it was strongest: it performed the broadest grounded loop in the Luna set—multiple knowledge and Calendar checks, reading the existing record, adding outbound and explicitly flexible return events, updating the race event and plan, preserving a mail reference, scheduling follow-up, messaging Divy, and triaging mail. It took 46.6 seconds and 34,331 total tokens, including 491 reasoning tokens.

> Your Windsor race train is booked for tomorrow: London Waterloo 07:27 → Staines → Windsor & Eton Riverside 08:44. The return 12:32 is only a suggested service — your Off-Peak Day Return (£12.44 with 16–25 Railcard) is valid on any permitted off-peak train.
>
> I’ve added the outbound and flexible return to Calendar and updated the race entry. One wrinkle: 36 minutes from arrival to the 09:20 start at Alexandra Gardens, including bib collection, is tight. I’m prompting you this evening to check the race instructions/route and finish packing, breakfast, fuel, and recovery prep.

### Weakest run: known-person news, repetition 2

Why it was weakest: it is arguably the most energetic version of the visible reply, but it made no knowledge call and lost the new fact. Xhigh repetition 1 did retrieve/read/update Lily, so increased effort still did not make the behaviour reliable. It took 6.6 seconds and 15,950 total tokens.

> NOOOO 😭 the classic “I bombed it” → *gets the role* pipeline. I’m so happy for Lily — clearly she read the interview much harsher than they did. Huge win!!!

## Terra

### Headline result

Terra was more consistently grounded in people/context at high and xhigh than at low or medium, but no effort was fully reliable. It completed all eight train workflows correctly. Seven of eight race-confirmation runs performed the expected actions; Terra-medium repetition 2 made no tool calls and produced only a native final answer, which the mail surface does not deliver. Seven of eight resolved-before-wakeup runs verified the newer Telegram message and stayed silent; Terra-medium repetition 2 stayed silent without checking anything.

Terra's visible language was often especially natural and characterful: “not a morning for faffing,” “low-voltage,” and, when xhigh actually read Divy's preferences, a specific *Frieren*/*Perfect Days* suggestion. That stronger voice could still conceal missing capability work, particularly at low/medium.

### Cost and latency

| Effort | Runs | Mean duration | Range | Mean total tokens | Mean reasoning tokens | Capability calls | Failed attempts |
|---|---:|---:|---:|---:|---:|---:|---:|
| Low | 12 | 16.5s | 3.6–43.5s | 18,013 | 10 | 50 | 20 |
| Medium | 12 | 12.4s | 4.4–23.2s | 16,257 | 34 | 42 | 2 |
| High | 12 | 18.1s | 4.8–30.5s | 20,194 | 17 | 78 | 7 |
| Xhigh | 12 | 24.5s | 8.6–43.9s | 22,548 | 67 | 93 | 0 |

Nineteen of Terra-low's 20 failed attempts came from one race-confirmation run. They were fast per-call `tool_error` results rather than an MCP startup failure, and the model eventually retried every required operation successfully. Terra-xhigh had no failed capability attempts.

### Scenario consistency

#### Race confirmation

Seven of eight runs performed concrete follow-through. Those seven inspected context, changed Calendar/knowledge, notified Divy, and triaged the mail. Several scheduled a later preparation check. Terra-medium repetition 2 was the exception: it made no capability calls and returned a short native final answer. In production, that mail turn would neither change state nor reach Divy.

#### Train confirmation

All 8/8 runs connected the booking to the existing race, updated its context, changed Calendar state without duplicating the race, preserved the off-peak return's flexibility, warned about the 36-minute bib window, notified Divy, and recorded mail triage. This was Terra's most consistent scenario.

#### Race-evening revisit

All 8/8 runs sent a focused evening message and understood that transport was resolved. Six broadly reassessed knowledge/mail/Calendar/recent Telegram. Terra-low repetition 1 and medium repetition 1 read only recent Telegram before messaging; their messages were correct, but their inspection was thinner than the scenario asks for. Xhigh repetition 2 also improved the train alarms before messaging.

#### Resolved before wake-up

Seven of eight runs read the newer Telegram message and correctly stayed silent without scheduling more work. Terra-medium repetition 2 also stayed silent, but made zero calls, so it reached the right visible outcome without verifying that the old loop had actually been resolved.

#### Conflicting needs

All 8/8 replies allowed rest to be legitimate and avoided unnecessary private actions. Only the two xhigh runs retrieved Divy's context. Those two produced the most personally grounded replies, including media that matched recorded preferences. The other six were natural but generic; high repetition 2 also drifted slightly back toward productivity coaching with “one thing finished beats…”

#### Known-person news

All 8/8 replies were warm and concise. Four retrieved and updated Lily's existing context: medium repetition 2, both high repetitions, and xhigh repetition 1. The other four lost the new fact. Again, strong visible prose was not evidence that the required memory work occurred.

## Terra low

### Strongest run: train confirmation, repetition 1

Why it was strongest: it searched and read the existing Windsor context, inspected Calendar, updated the plan, created the train event, scheduled an evening check, notified Divy, and triaged the mail. It captured both the tight arrival and flexible return in one very natural message. It took 20.1 seconds and 24,283 total tokens.

> Windsor is locked in for tomorrow: Waterloo 07:27 → Staines → Windsor & Eton Riverside 08:44. That gives you 36 minutes to get to Alexandra Gardens and collect the bib before the 09:20 half—workable, but not a morning for faffing. The £12.44 railcard return is flexible; 12:32 is only the selected suggestion, not a restriction. I’ve put the journey in your calendar and will nudge you this evening to pack/fuel.

### Weakest run: known-person news, repetition 2

Why it was weakest: the response is good, but it made no capability calls and did not retrieve or update Lily. It took 7.2 seconds and 15,251 total tokens.

> YESSSS 😭 She was clearly so much better than she felt leaving that interview. I’m ridiculously happy for her—what a relief.

### Operational outlier: race confirmation, repetition 2

This run ultimately completed the loop and sent a good message, but only after 19 failed capability attempts, including repeated attempts to read knowledge, schedule the wake-up, and record mail triage. It took 43.5 seconds. This is the noisiest execution in the matrix, despite a successful final state.

## Terra medium

### Strongest run: train confirmation, repetition 2

Why it was strongest: it searched and read the existing context, inspected recent messages and Calendar, updated both the plan and race event, scheduled follow-up, notified Divy, and triaged the mail. The message is the most concise successful train response in the set. It took 19.6 seconds and 23,568 total tokens.

> Your Windsor train is booked for tomorrow: Waterloo 07:27 → change at Staines → Windsor & Eton Riverside 08:44. That leaves 36 minutes before the 09:20 half-marathon start, including bib collection, so I’d head straight to Alexandra Gardens. The £12.44 return is flexible on permitted off-peak services; 12:32 is only the suggested train.

### Weakest run: race confirmation, repetition 2

Why it was weakest: it made no capability calls—no context lookup, Calendar event, knowledge update, Telegram message, revisit, or mail decision. The following text was only the mail turn's native final answer, so Divy would not receive it. This is the clearest complete behavioral failure in the matrix. It took 9.6 seconds and 11,693 total tokens.

> Windsor Trail Run Half Marathon is confirmed for tomorrow, Sunday 30 August: 09:20 start at Alexandra Gardens, Windsor. Leave margin to collect the race number at registration and check the organiser’s final instructions/course guide today.

## Terra high

### Strongest run: train confirmation, repetition 2

Why it was strongest: it thoroughly searched the existing context, updated knowledge, added the outbound and explicitly flexible return, adjusted the race event, scheduled preparation follow-up, messaged Divy, and triaged the mail. It took 30.5 seconds and 32,171 total tokens.

> Windsor travel is locked in for tomorrow: Waterloo 07:27 → change at Staines → Windsor & Eton Riverside 08:44. I’ve put it alongside the half marathon. That leaves 36 minutes to get your bib and reach Alexandra Gardens, so tonight’s worth making the kit/fuel/bib plan idiot-proof. Your 12:32 return is only the selected option—the off-peak return is flexible.

### Weakest run: conflicting needs, repetition 2

Why it was weakest: the reply is still humane, but it did not inspect Divy's context and ended by turning rest back into a smaller productivity prescription. It took 4.8 seconds and 9,161 total tokens.

> That doesn’t sound lazy. It sounds like the part of you that promised a “huge weekend” has already spent some of its fuel.
>
> Watch something guilt-free tonight. The hackathon can still have a strong, smaller shape tomorrow—one thing finished beats a whole weekend spent negotiating with yourself.

## Terra xhigh

### Strongest run: conflicting needs, repetition 2

Why it was strongest: unlike every lower Terra effort, it searched, browsed, and read Divy's context before making the personal judgement. The resulting response used actual preferences naturally without announcing the lookup or manufacturing an action. It took 11.9 seconds and 19,815 total tokens.

> No. You were excited for a big sprint, not signing a contract to be productive every waking hour.
>
> Take the evening properly off—watch something gentle, eat, let your brain go blank. *Frieren* or *Perfect Days* energy, not “a film I should analyse.” The hackathon can still happen when you actually want to make things.

### Weakest run: known-person news, repetition 2

Why it was weakest: the reply is vivid, but it made no knowledge calls and failed to preserve Lily's outcome. Xhigh repetition 1 performed the full lookup/read/update loop, so this remained nondeterministic even at the highest tested effort. It took 8.6 seconds and 9,178 total tokens.

> Lily!!! That’s incredible 😭 She was out here catastrophising her way into the job. Please tell her I’m thrilled for her.

## Sol

### Headline result

Sol-medium resembled the other models more than it escaped their weaknesses: it completed every event/revisit workflow, but skipped Divy's context in both conflicting-needs turns and skipped Lily's context in one of two known-person turns.

Sol-high was different. It was the only tested model/effort pair to satisfy the core retrieval and action expectations in both repetitions of all six scenarios. In particular, it retrieved Divy's context for both personal-judgment turns and retrieved, read, and updated Lily in both known-person turns. This is strong evidence that the tool surface and current prompts contain enough information for the desired behavior; stronger reasoning can infer and follow it reliably in this small sample.

It was not perfect. Sol-high still never used web research for the race logistics, sometimes shifted obvious planning back to Divy (“reply ‘plan it’”), and occasionally inspected more context than was useful. Those are prompt/judgment issues even though its core retrieval compliance was much better.

### Secondary operational metrics

| Effort | Runs | Mean duration | Range | Mean total tokens | Mean reasoning tokens | Capability calls | Failed attempts |
|---|---:|---:|---:|---:|---:|---:|---:|
| Medium | 12 | 32.6s | 10.1–77.9s | 20,876 | 31 | 78 | 2 |
| High | 12 | 39.1s | 11.0–84.3s | 22,949 | 40 | 93 | 3 |

All five failed capability attempts were retried successfully in the same turn. No complete run or MCP startup failed.

### Scenario consistency

#### Race confirmation

All 4/4 runs performed the full reversible loop: knowledge inspection/update, Calendar creation, Telegram notification, wake-up scheduling, and mail triage. They recognised the bib and travel open loops and avoided presenting estimated finish times as official. None attempted public research. Several asked Divy to provide the instructions or opt into travel planning instead of searching and acting immediately.

#### Train confirmation

All 4/4 runs connected the train to the existing race, read its context, updated knowledge and Calendar, preserved the flexible return, warned about the 36-minute bib margin, notified Divy, and triaged the mail. The first medium run created a tentative, non-busy return event and updated the race itself; the other runs varied the Calendar representation without turning 12:32 into a hard commitment.

#### Race-evening revisit

All 4/4 runs broadly reassessed recent Telegram, knowledge, mail, and Calendar state. They recognised that transport was resolved, preserved the flexible return, and limited the message to the remaining bib/packing/fuel risk. High repetition 2 scheduled a concrete early-morning transport-disruption check; the other three did not manufacture another wake-up.

#### Resolved before wake-up

All 4/4 runs read the newer Telegram message, correctly stayed silent, and scheduled nothing further. Three reconciled stale knowledge; high repetition 1 sensibly stopped after the newer message established that the loop was closed.

#### Conflicting needs

Both medium runs replied naturally but made no knowledge call. Both high runs searched and read Divy's context first, allowed ambition and rest to coexist, and avoided manufacturing work. This is the clearest medium-versus-high distinction in the matrix.

#### Known-person news

Medium repetition 1 attempted generic MCP resource discovery, then replied without retrieving or updating Lily. Medium repetition 2 did the correct search/read/update loop. Both high runs searched, read, and updated Lily before responding; routine remembering remained invisible and the replies stayed celebratory rather than administrative.

## Sol medium

### Strongest run: train confirmation, repetition 1

Why it was strongest: it inspected the existing race, recent messages, Calendar, and wake-ups; created fixed outbound and non-busy tentative return events; updated the race event and knowledge; scheduled a focused preparation check; notified Divy; and triaged the mail. The state changes precisely represented what was fixed versus flexible.

> Nice — your Windsor train is booked for tomorrow. I’ve put the journeys in Calendar: Waterloo 07:27 → Windsor 08:44 (change at Staines), and the 12:32 return is marked as flexible rather than fixed.
>
> One warning: 08:44 leaves only 36 minutes to get to Alexandra Gardens and collect your bib before the 09:20 start. That’s tighter than I’d be comfortable with; if the outward ticket permits an earlier train, I’d take it. I’ll nudge you this evening to pack the ticket, Railcard, kit and fuel and sort the route to Waterloo.

### Weakest run: known-person news, repetition 1

Why it was weakest: the text is lively, but it never used Ariadne's knowledge capability and did not preserve Lily's result. Its only capability attempt was generic MCP resource discovery. This is the same good-prose/absent-memory failure seen in Luna and Terra.

> YESSSS LILY!!! 😭 That post-interview spiral was lying to her—the role was already hers. I’m so happy for her.

## Sol high

### Strongest run: known-person news, repetition 2

Why it was strongest: it searched for Lily, read her existing record, checked Divy's context, updated Lily with the durable outcome and why it mattered, then responded with unforced excitement. The memory work remained completely invisible in the conversation.

> NO WAYYYY 😭😭😭 she got the role she *thought she’d bombed*—that must feel completely surreal. I’m so happy for her!!! 🥹🎉

### Weakest run: race confirmation, repetition 2

Why it was weakest: it still completed the core loop—Calendar, event knowledge, goal update, Telegram, revisit, and mail triage—but stopped short of researching/planning transport and explicitly asked Divy to opt in to work that Ariadne was already authorised to do. This is weaker companion judgment, not a missing-capability failure.

> Oh wow — your Windsor Trail Run Half Marathon place is confirmed for tomorrow (Sun 30 Aug), starting 09:20 at Alexandra Gardens. I’ve put it in your calendar.
>
> You need to collect your race number before the start, so don’t aim to arrive at 09:20; check the final instructions/course guide today. Reply “plan it” if you want me to work out the Southwark → Windsor journey and a sensible arrival time. 🏃

## What Sol changes in the diagnosis

The answer is not “the prompts are broken” or “the smaller models are incapable.” Sol-high demonstrates that the present architecture, tools, fixtures, and instruction layers can produce the intended behavior end to end. But the desired retrieval/update behavior is currently implicit enough that Luna, Terra, and Sol-medium often treat it as optional.

That still argues for prompt changes. Core companion behavior should not depend on a model inferring an unstated invariant. The prompts should make four expectations more action-oriented and salient:

1. Before judging something personal or responding about a named person, retrieve the relevant existing context.
2. When a turn reveals a durable fact, outcome, preference, or relationship detail, update the existing knowledge invisibly before replying.
3. For useful reversible private actions, act now rather than asking Divy to opt in or do the planning himself.
4. When a concrete unknown materially affects a near-term plan, research it rather than merely telling Divy to check it.

These are model-facing behavior expectations, not runtime heuristics or a “tool turn versus conversation turn” classifier. Sol-high's success suggests making them explicit should improve lower-cost models without needing new infrastructure.

## Cross-model comparison

| Model | Runs | Mean duration | Mean total tokens | Capability calls | Failed attempts |
|---|---:|---:|---:|---:|---:|
| Luna | 48 | 22.4s | 19,188 | 286 | 5 |
| Terra | 48 | 17.9s | 19,253 | 263 | 29 |
| Sol | 24 | 35.8s | 21,912 | 171 | 5 |

The similar mean token totals hide different shapes. Luna generally became more tool-active as effort rose. Terra-medium's aggregate includes two suspicious zero-call runs: an undelivered mail response and an unverified silent revisit. Terra-xhigh had zero failed capability attempts. Terra-xhigh and Sol-high were the only configurations to inspect Divy's context in both conflicting-needs repetitions.

Sol was the most consistent about Lily overall (3/4 grounded updates, including 2/2 at high), followed by Terra (4/8) and Luna (3/8). Sol-high made ordinary-conversation grounding reliable across this small sample; every other configuration missed at least one retrieval/update case. All three models were strong on explicit train workflow facts. Luna and Sol had perfect concrete follow-through on race confirmations; Terra had one medium zero-action miss.

Across all three models, none of the 20 race-confirmation runs attempted native web research. The mail and revisit profiles configured Codex web search as `live`; it was not a mock. The disposable workspace separately blocked shell network access. Because no model attempted the native tool, these runs establish only that it was not chosen—not that a real search would have succeeded in this development environment. The models correctly avoided inventing a registration cutoff, but did not try to find one.

The strongest visible messages were not always the strongest runs. This is the central result of the matrix: without checking tool calls and final state, several memory failures—and Terra-medium's entirely undelivered mail answer—would look successful.

## Integrity notes

- 120/120 commands exited successfully.
- 120/120 JSON reports and 120/120 streaming logs are present.
- No run timed out.
- No MCP startup failure appeared.
- There were 39 failed individual capability attempts: 5 Luna, 29 Terra, and 5 Sol. Every failed attempt was followed by a successful retry of that operation during the same turn.
- No prompt, code, configuration, or behavior was changed in response to these findings.
