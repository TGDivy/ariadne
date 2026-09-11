# Grocery shopping

Status: approved for implementation.

Depends on: browser control and conversational background handoffs. Proactive stewardship may consume the capability once it is proven with owner-directed orders.

## Outcome

Iris can turn a natural grocery request into a carefully assembled Waitrose basket, obtain Divy's approval for the exact order, complete checkout in his existing account, record the result, and add the delivery or collection window to Calendar. The experience removes the repetitive searching and basket work while leaving final spending and material substitutions under clear human control.

Waitrose is the first supported retailer, implemented through the general browser capability rather than a private or assumed consumer API. Keep retailer-specific navigation and extraction in a small adapter/recipe layer so ordinary conversation and future grocery reasoning do not depend on page selectors.

## Order lifecycle

Represent one grocery attempt as a small durable order draft with an idempotent task ID and explicit state:

1. **Understand:** derive a bounded requested list, quantities, timing, fulfilment mode, and known preferences from the current conversation and approved private context. Ask only where ambiguity could materially change the basket.
2. **Build:** attach to the persistent personal browser profile, search Waitrose, select products, and prepare the basket. Favour known choices, favourites, and relevant prior orders without assuming that every past purchase should recur.
3. **Resolve:** identify unavailable items, important size/price changes, minimum-order constraints, and proposed substitutions. Choose a suitable available delivery or collection slot using Calendar and stored preferences, but do not reserve a materially inconvenient or paid slot silently.
4. **Review:** re-read the live basket and present a compact Telegram summary containing retailer, fulfilment mode, slot, item/quantity list, substitutions or omissions, fees, and current estimated total. Link the summary to the durable draft and request explicit approval.
5. **Revalidate:** immediately before checkout, confirm that the live basket, fees, slot, address/location, and total still fit the approved snapshot and permitted tolerance. Any material difference returns to review.
6. **Checkout:** use payment and address details already held by Waitrose. Never ask the model to read, retain, or reproduce raw card details. Submit exactly once and do not infer failure from a lost response.
7. **Confirm:** find the retailer's definitive order status and number, retain concise confirmation evidence, update Calendar with the delivery/collection window, update relevant grocery knowledge, and send a conversational completion handoff.

Support both delivery and collection. Use the mode stated for the order or a clearly recorded preference; otherwise ask rather than silently choosing between materially different logistics.

## Preferences and substitutions

Keep grocery knowledge understandable and editable in the existing personal knowledge base rather than building a universal product ontology. It may include staple products, acceptable alternatives, brand/size preferences, dietary constraints, disliked products, typical quantities, delivery/collection preferences, and retailer-specific identifiers learned from confirmed orders.

Distinguish firm constraints from observed habits and tentative inference. A prior order is evidence, not permanent permission to rebuy it. Never substitute across a dietary/allergy constraint. Show substitutions at review until Divy has explicitly recorded a reusable rule for that product or category; preserve site-proposed substitutions separately from Iris's recommendation.

The initial source of need is Divy's message, list, or an explicitly relevant plan. Later stewardship may notice likely replenishment needs from confirmed order history, Calendar, health/workout plans, or knowledge, but it must preserve uncertainty about actual consumption and should prepare a suggested list or basket rather than fabricate need.

## Approval and price changes

The first release requires explicit approval for every checkout. Approval is bound to the draft ID, retailer, basket contents and quantities, fulfilment mode, slot, substitutions, and displayed total with a small configured price tolerance for retailer-weighted items or final adjustments. Adding items, changing substitutions, choosing a different slot, crossing the tolerance, or changing fulfilment location invalidates approval.

A Telegram approval is accepted only from the configured owner and should be concise and natural; it may use a trusted button backed by the durable draft or an unambiguous conversational confirmation. Expire approvals after a bounded time because availability and prices change. Cancellation before submission releases the browser task and leaves the Waitrose basket in an explained state.

Standing autonomous grocery budgets are a later policy layer, not part of the initial implementation. Design the order draft so a future explicit policy could constrain retailer, period, maximum spend, product classes, substitution rules, and allowed slots without weakening today's approval semantics.

## Failure and recovery

- Login, MFA, CAPTCHA, consent changes, or unfamiliar checkout steps pause for private human takeover.
- Site redesign or ambiguous semantic targets stop before checkout and retain a useful draft; do not improvise around a consequential button using uncertain coordinates.
- Out-of-stock items and disappeared slots return to Resolve/Review instead of being silently dropped after approval.
- After a timeout or crash during submission, inspect Waitrose orders, confirmation pages, email, and the durable draft before any retry. If the outcome remains uncertain, tell Divy and do not submit again.
- Calendar and knowledge updates occur only after definitive confirmation and remain repairable if a downstream write fails.
- If Waitrose blocks reliable automation, basket preparation plus human takeover is a valid degraded outcome; do not bypass its controls.

## Verification

- Deterministic tests cover draft state transitions, item matching, preference strength, substitution constraints, totals/tolerance, approval expiry/invalidation, owner authorization, and exactly-once checkout semantics.
- Local fixture-site scenarios cover search, favourites, variable-weight pricing, basket changes, unavailable products, substitutions, delivery and collection slots, minimum spend, fees, login expiry, CAPTCHA/takeover, checkout success, definitive rejection, and uncertain submission.
- Adapter contract tests use retained sanitized fixtures where permitted, while live Waitrose selectors and flows are validated manually because the website can change.
- End-to-end dry runs on the Linux home server build real baskets without checkout and exercise Telegram review, cancellation, restart recovery, and takeover.
- The first real purchase is owner-observed, explicitly approved, low value, and followed through confirmation, Calendar, and conversational handoff. Do not enable proactive basket building until several owner-directed dry runs are reliable.
- Run the complete formatting, lint, strict typing, and test suite.

## Out of scope

Autonomous checkout under a standing budget, raw payment-card or password storage in Ariadne, alcohol or regulated-product purchasing, multiple retailer comparison, returns/refunds, delivery issue negotiation, CAPTCHA bypass, nutrition or medical claims, and inferring consumption solely from elapsed time.
