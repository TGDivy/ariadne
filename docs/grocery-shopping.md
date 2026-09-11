# Grocery ordering operations

Grocery ordering turns a natural request into a carefully assembled Waitrose basket,
shows Divy the exact order, and — only once that order is explicitly approved —
completes checkout in his existing account. It is built entirely on the general
[browser capability](browser-control.md); there is no private or assumed consumer
API. Waitrose-specific navigation lives in one adapter so ordinary conversation
never depends on page selectors.

Nothing here grants new authority. Building a basket is reversible preparation.
Spending money is not, and every checkout needs a fresh trusted approval bound to
the exact basket.

## Enable it deliberately

`[grocery]` requires `[browser]`. Both are off in `config.example.toml`.

```toml
[grocery]
enabled = true
state = "~/.local/state/ariadne/grocery.sqlite3"
browser_profile = "personal"
base_url = "https://www.waitrose.com"
# Real checkout stays off until the dry runs below pass.
checkout_enabled = false
weighted_tolerance = "1.00"
approval_ttl_seconds = 900
```

- `browser_profile` names the persistent Chromium profile that already holds the
  Waitrose login. Create and sign into it through takeover first — never give Iris
  credentials or MFA codes.
- `base_url` must be HTTPS except for `localhost`/`127.0.0.1`, which exists so the
  fixture shop in `tests/test_grocery_waitrose.py` can exercise the same adapter.
- `weighted_tolerance` is the pounds of drift permitted on variable-weight items
  before an approval is invalidated. A fixed-price change always invalidates it.
- `approval_ttl_seconds` bounds how long an approval stays usable, because prices
  and slots move.

The state database is created mode `0600` under a `0700` directory. It holds order
drafts, their revision history, approvals, checkout operations, confirmations,
follow-through status, and a completion outbox. It never stores card details,
passwords, or session cookies — those stay inside the browser profile.

`ariadne config show` reports the whole `[grocery]` block; none of it is secret.

## Order lifecycle

One request is one durable draft with an idempotent task id and an explicit state:

```text
understand ─▶ build ─▶ resolve ─▶ review ─▶ approved ─▶ revalidate ─▶ checkout
                 ▲         │         ▲                       │            │
                 └─────────┘         └───────────────────────┘      ┌─────┴─────┐
                                       any material change       confirmed  rejected
                                                                        uncertain
```

- **Understand** derives a bounded item list, quantities, timing, and fulfilment
  mode. Delivery and collection are materially different logistics, so an unstated
  mode is asked about rather than guessed.
- **Build** attaches the persistent profile, searches, and adds products. Candidates
  are ranked with visible evidence; anything violating a firm dietary or allergy
  constraint is removed outright. A product whose name is not the requested one is a
  substitution and must record whether it came from Iris, the retailer, or a
  reusable rule Divy already wrote down.
- **Resolve** records unavailable items with real reasons and reads live slots.
- **Review** re-reads the live basket, checks that its lines reconcile with the
  retailer's own subtotal, fees, and total, and enforces minimum spend before
  presenting anything.
- **Approved** is set only by a trusted Telegram button from the configured owner on
  the exact review card.
- **Revalidate** immediately re-reads the basket before checkout. Any material
  difference returns to review and invalidates the approval.
- **Checkout** submits exactly once and records `confirmed`, `rejected`, or
  `uncertain`.

Approval binds to the retailer, item list and quantities, fulfilment mode, slot,
location, substitutions, omissions, fees, and total. Adding an item, changing a
substitution or slot, moving the fulfilment location, or crossing the tolerance
invalidates it and returns to review.

## Preferences are evidence, not permission

Grocery knowledge stays in the ordinary personal knowledge base as editable prose,
not a product ontology. Preferences carry an explicit strength — `firm`, `observed`,
or `tentative` — and constraints carry a kind — `allergy`, `dietary`, or `dislike`.

- A firm allergy or dietary constraint removes a matching candidate from the ranking
  entirely.
- A soft dislike only lowers a candidate's rank; the product is still offered.
- A substitution that engages a firm safety constraint — asking for "oat milk" under
  a firm dairy rule, say — needs a firm reusable rule for the replacement. An
  unrelated swap is not frozen just because an allergy is on file.
- A favourite or a prior order raises a rank and is shown as evidence. It is never
  treated as standing permission to rebuy.

## Failure and recovery

- Login expiry, MFA, CAPTCHA, consent changes, and unfamiliar checkout steps pause
  the draft in `needs_takeover` and return the private takeover URL. Iris never
  works around a site control.
- An ambiguous or missing checkout control stops before submission and keeps the
  draft. Coordinate guessing is refused for a consequential button.
- Out-of-stock items and disappeared slots return to resolve/review rather than
  being silently dropped after approval.
- Exactly-once checkout is enforced in SQLite: a second attempt on a draft that
  already has a checkout operation is refused. A crash mid-submission is recovered
  as `uncertain` at startup, and `uncertain` is resolved only by the owner reading
  Waitrose's own definitive order history:

  ```bash
  # After the owner has checked the retailer's order list.
  ariadne-browser recovery list
  ```

  Then record exactly one outcome through `read_grocery_order` /
  `repair_grocery_follow_through` in conversation. Never resubmit.
- Calendar, knowledge, and the conversational completion update are attempted only
  after definitive confirmation, and each is retried independently by
  `repair_grocery_follow_through` if it fails.

## The completion handoff

A confirmed order writes one row into `grocery_handoff_outbox`. That decouples
grocery from the conversational-handoff feature, which lands on a separate branch:
until it merges, the outbox is durable evidence that a completion update is owed,
and `read_grocery_order` surfaces it. Once
`hand_off_to_telegram_conversation` exists, drain the outbox into it rather than
sending Telegram text from the grocery path.

## What Iris can and cannot do

Enabled for the Telegram profile only. Proactive stewardship deliberately does
**not** get these tools yet; the plan requires several reliable owner-directed
orders first, and enabling it is a one-line change to `TELEGRAM_PROFILE`'s siblings
in `src/ariadne/profile.py` when that time comes.

Out of scope by design: autonomous checkout under a standing budget, storing card
details or passwords in Ariadne, alcohol or other regulated products, comparing
retailers, returns and refunds, delivery-issue negotiation, CAPTCHA bypass,
nutrition or medical claims, and inferring consumption from elapsed time alone.

## Exact home-server smoke tests

These are live-only. Passing CI does not claim any of them, and none of them have
been performed: everything in this repository was developed against a local fixture
shop with a disposable browser profile.

1. Run the full repository checks, then
   `ARIADNE_REQUIRE_BROWSER_TESTS=1 uv run pytest tests/test_grocery_waitrose.py`
   and confirm the thirteen fixture scenarios pass rather than skip.
2. With `[grocery].enabled = true` and `checkout_enabled = false`, sign into
   Waitrose in the `personal` browser profile through takeover. Restart the browser
   daemon and confirm the session survives.
3. Ask Iris for a small list in Telegram. Confirm she asks about delivery versus
   collection when you have not said, searches, and shows candidates whose evidence
   matches reality.
4. Check the review card against the live Waitrose basket by eye: every item,
   quantity, size, substitution, omission, the slot, fees, and the total.
5. Tap Cancel. Confirm the draft is cancelled, the browser profile is released, and
   the Waitrose basket is left in an explained state.
6. Repeat to review, then change the basket in a separate browser tab. Confirm the
   card reports that the basket changed and refuses the stale approval.
7. Let an approval expire. Confirm the draft returns to review and the card settles
   as expired.
8. Confirm the real Waitrose selectors still parse: search listings, quantity
   controls, both delivery and collection slot lists, the trolley summary, minimum
   spend, fees, and the final checkout control. This is the check most likely to
   break, because the site can change at any time.
9. Force a login expiry (sign out in another session) mid-draft. Confirm Iris pauses
   for takeover with the private URL instead of retrying.
10. Restart Ariadne with a draft in review and with a draft approved. Confirm nothing
    is submitted and the approval state is honest after the restart.
11. Only after several reliable dry runs: set `checkout_enabled = true` and place one
    **owner-observed, explicitly approved, low-value** order. Watch the confirmation
    number, the Calendar entry for the delivery or collection window, the knowledge
    record, and the completion message. Then set it back to `false` until you decide
    otherwise.

Record the date, host revision, and pass/fail in private deployment notes. Do not
commit screenshots, order numbers, addresses, basket contents, or profile data.
