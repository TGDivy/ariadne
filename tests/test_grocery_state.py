from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from grocery_fixtures import NOW, basket_item, requested, slot, snapshot, substitution

from ariadne.grocery.errors import (
    GroceryApprovalError,
    GroceryStateError,
    GroceryUncertainError,
)
from ariadne.grocery.models import (
    ApprovalStatus,
    BrowserAttachment,
    CheckoutStatus,
    DownstreamStatus,
    FulfilmentMode,
    GroceryState,
    OrderConfirmation,
    material_differences,
)
from ariadne.grocery.store import GroceryStore

OWNER = 7
TOLERANCE = Decimal("1.00")


class Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path: Path, clock: Clock) -> GroceryStore:
    state = GroceryStore(tmp_path / "private" / "grocery.sqlite3", clock=clock)
    state.initialize()
    return state


def reviewed(store: GroceryStore, **kwargs: object) -> str:
    """Drive one draft to review with an exact captured basket."""
    draft = store.create_draft((requested(),), mode=FulfilmentMode.DELIVERY)
    draft = store.save(
        draft.model_copy(update={"state": GroceryState.BUILD}),
        expected_revision=draft.revision,
        reason="build",
    )
    draft = store.save(
        draft.model_copy(
            update={
                "state": GroceryState.RESOLVE,
                "basket": (basket_item(),),
                "selected_slot": slot(),
            }
        ),
        expected_revision=draft.revision,
        reason="resolve",
    )
    store.save(
        draft.model_copy(
            update={
                "state": GroceryState.REVIEW,
                "snapshot": snapshot(**kwargs),  # type: ignore[arg-type]
            }
        ),
        expected_revision=draft.revision,
        reason="review",
    )
    return draft.draft_id


def approved(store: GroceryStore) -> tuple[str, str]:
    draft_id = reviewed(store)
    approval = store.create_approval(
        draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
    )
    store.attach_approval_message(approval.approval_id, 41)
    selection = store.decide_approval(
        approval.approval_id,
        user_id=OWNER,
        chat_id=OWNER,
        message_id=41,
        decision="approve",
    )
    assert selection.outcome == "accepted"
    return draft_id, approval.approval_id


def test_state_file_is_private_and_a_draft_needs_real_items(
    tmp_path: Path,
) -> None:
    store = GroceryStore(tmp_path / "nested" / "grocery.sqlite3")
    store.initialize()

    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    with pytest.raises(GroceryStateError, match="at least one requested item"):
        store.create_draft(())
    with pytest.raises(GroceryStateError, match="unique"):
        store.create_draft((requested("a"), requested("a")))
    with pytest.raises(GroceryStateError, match="location cannot be fixed"):
        store.create_draft((requested(),), location="12 Example Street")


def test_only_declared_transitions_are_allowed(store: GroceryStore) -> None:
    draft = store.create_draft((requested(),))

    with pytest.raises(GroceryStateError, match="cannot move from understand"):
        store.save(
            draft.model_copy(update={"state": GroceryState.CHECKOUT}),
            expected_revision=draft.revision,
            reason="skip the whole lifecycle",
        )

    moved = store.save(
        draft.model_copy(update={"state": GroceryState.BUILD}),
        expected_revision=draft.revision,
        reason="build",
    )
    assert moved.revision == draft.revision + 1

    with pytest.raises(GroceryStateError, match="read it again"):
        store.save(
            moved.model_copy(update={"state": GroceryState.RESOLVE}),
            expected_revision=draft.revision,
            reason="stale write",
        )


def test_attaching_a_browser_lease_does_not_invalidate_approval(
    store: GroceryStore,
) -> None:
    draft_id, approval_id = approved(store)
    before = store.get(draft_id)

    store.attach_browser(
        draft_id,
        BrowserAttachment(
            task_id=before.task_id, lease_token="lease-1", profile="personal"
        ),
    )

    after = store.get(draft_id)
    assert after.revision == before.revision
    assert after.browser is not None
    assert "lease-1" not in str(after.public_payload())
    live = store.live_approval(draft_id)
    assert live is not None and live.approval_id == approval_id
    assert live.status == ApprovalStatus.APPROVED


def test_approval_requires_a_reviewed_basket_and_stays_singular(
    store: GroceryStore,
) -> None:
    draft = store.create_draft((requested(),))
    with pytest.raises(GroceryApprovalError, match="must be in review"):
        store.create_approval(
            draft.draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
        )

    draft_id = reviewed(store)
    store.create_approval(draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900)
    with pytest.raises(GroceryApprovalError, match="already has a live approval"):
        store.create_approval(
            draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
        )
    with pytest.raises(ValueError, match="30-3600"):
        store.create_approval(
            reviewed(store), owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=5
        )


def test_only_the_owner_and_the_exact_card_can_decide(store: GroceryStore) -> None:
    draft_id = reviewed(store)
    approval = store.create_approval(
        draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
    )
    store.attach_approval_message(approval.approval_id, 41)

    intruder = store.decide_approval(
        approval.approval_id,
        user_id=OWNER + 1,
        chat_id=OWNER,
        message_id=41,
        decision="approve",
    )
    assert intruder.outcome == "unauthorized"

    other_card = store.decide_approval(
        approval.approval_id,
        user_id=OWNER,
        chat_id=OWNER,
        message_id=99,
        decision="approve",
    )
    assert other_card.outcome == "stale"

    unknown = store.decide_approval(
        "not-an-approval",
        user_id=OWNER,
        chat_id=OWNER,
        message_id=41,
        decision="approve",
    )
    assert unknown.outcome == "stale"
    assert store.get(draft_id).state == GroceryState.REVIEW


def test_a_repeated_tap_is_recorded_once(store: GroceryStore) -> None:
    draft_id = reviewed(store)
    approval = store.create_approval(
        draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
    )
    store.attach_approval_message(approval.approval_id, 41)

    first = store.decide_approval(
        approval.approval_id,
        user_id=OWNER,
        chat_id=OWNER,
        message_id=41,
        decision="approve",
    )
    second = store.decide_approval(
        approval.approval_id,
        user_id=OWNER,
        chat_id=OWNER,
        message_id=41,
        decision="cancel",
    )

    assert first.outcome == "accepted"
    assert second.outcome == "already_decided"
    assert store.get(draft_id).state == GroceryState.APPROVED


def test_changing_the_basket_invalidates_a_live_approval(
    store: GroceryStore,
) -> None:
    draft_id, _ = approved(store)
    draft = store.get(draft_id)

    store.save(
        draft.model_copy(update={"state": GroceryState.REVIEW}),
        expected_revision=draft.revision,
        reason="Owner added another item",
    )

    assert store.live_approval(draft_id) is None
    with pytest.raises(GroceryApprovalError, match="does not have a current exact"):
        store.mark_revalidating(draft_id)


def test_an_expired_approval_returns_the_draft_to_review(
    store: GroceryStore, clock: Clock
) -> None:
    draft_id, approval_id = approved(store)

    clock.advance(901)

    assert store.live_approval(draft_id) is None
    approval = store.approval(approval_id)
    assert approval is not None and approval.status == ApprovalStatus.EXPIRED
    assert store.get(draft_id).state == GroceryState.REVIEW


def test_checkout_consumes_its_approval_exactly_once(store: GroceryStore) -> None:
    draft_id, approval_id = approved(store)
    draft, approval = store.mark_revalidating(draft_id)
    assert draft.state == GroceryState.REVALIDATE
    assert approval.approval_id == approval_id

    operation = store.begin_checkout(
        draft_id,
        approval_id=approval_id,
        operation_id="checkout.1",
        live=snapshot(),
        weighted_tolerance=TOLERANCE,
    )

    assert operation.status == CheckoutStatus.SUBMITTING
    used = store.approval(approval_id)
    assert used is not None and used.status == ApprovalStatus.USED
    assert store.get(draft_id).state == GroceryState.CHECKOUT

    with pytest.raises(GroceryUncertainError, match="already attempted"):
        store.begin_checkout(
            draft_id,
            approval_id=approval_id,
            operation_id="checkout.2",
            live=snapshot(),
            weighted_tolerance=TOLERANCE,
        )


def test_checkout_refuses_a_basket_that_moved_beyond_tolerance(
    store: GroceryStore,
) -> None:
    draft_id, approval_id = approved(store)
    store.mark_revalidating(draft_id)

    with pytest.raises(GroceryApprovalError, match="tolerance"):
        store.begin_checkout(
            draft_id,
            approval_id=approval_id,
            operation_id="checkout.1",
            live=snapshot((basket_item(unit_price="9.99"),)),
            weighted_tolerance=TOLERANCE,
        )


def test_a_confirmed_order_records_evidence_and_opens_follow_through(
    store: GroceryStore,
) -> None:
    draft_id, approval_id = approved(store)
    store.mark_revalidating(draft_id)
    store.begin_checkout(
        draft_id,
        approval_id=approval_id,
        operation_id="checkout.1",
        live=snapshot(),
        weighted_tolerance=TOLERANCE,
    )
    confirmation = OrderConfirmation(
        order_number="WR-TEST-1",
        confirmed_at=NOW,
        evidence="Fixture confirmation page.",
    )

    operation = store.confirm_checkout("checkout.1", confirmation)

    assert operation.status == CheckoutStatus.SUCCEEDED
    assert store.get(draft_id).state == GroceryState.CONFIRMED
    assert store.confirmation(draft_id) == confirmation
    record = store.downstream(draft_id)
    assert record is not None
    assert record.calendar_status == DownstreamStatus.PENDING

    # Confirming again is idempotent rather than a second order.
    assert store.confirm_checkout("checkout.1", confirmation).status == (
        CheckoutStatus.SUCCEEDED
    )


def test_a_restart_during_submission_becomes_uncertain_not_retried(
    store: GroceryStore,
) -> None:
    draft_id, approval_id = approved(store)
    store.mark_revalidating(draft_id)
    store.begin_checkout(
        draft_id,
        approval_id=approval_id,
        operation_id="checkout.1",
        live=snapshot(),
        weighted_tolerance=TOLERANCE,
    )

    recovered = store.recover_interrupted_checkouts()

    assert [item.operation_id for item in recovered] == ["checkout.1"]
    assert recovered[0].status == CheckoutStatus.UNCERTAIN
    assert store.get(draft_id).state == GroceryState.UNCERTAIN
    with pytest.raises(GroceryUncertainError, match="already attempted"):
        store.begin_checkout(
            draft_id,
            approval_id=approval_id,
            operation_id="checkout.2",
            live=snapshot(),
            weighted_tolerance=TOLERANCE,
        )


def test_an_uncertain_checkout_is_resolved_only_by_definitive_evidence(
    store: GroceryStore,
) -> None:
    draft_id, approval_id = approved(store)
    store.mark_revalidating(draft_id)
    store.begin_checkout(
        draft_id,
        approval_id=approval_id,
        operation_id="checkout.1",
        live=snapshot(),
        weighted_tolerance=TOLERANCE,
    )
    store.mark_checkout_uncertain("checkout.1", "response lost")

    with pytest.raises(GroceryStateError, match="definitive order number"):
        store.reconcile_uncertain(draft_id, outcome="confirmed_succeeded")

    draft = store.reconcile_uncertain(
        draft_id,
        outcome="confirmed_succeeded",
        confirmation=OrderConfirmation(
            order_number="WR-TEST-2",
            confirmed_at=NOW,
            evidence="Owner read the retailer's own order history.",
        ),
    )

    assert draft.state == GroceryState.CONFIRMED
    assert store.downstream(draft_id) is not None
    with pytest.raises(GroceryStateError, match="not uncertain"):
        store.reconcile_uncertain(draft_id, outcome="confirmed_not_submitted")


def test_an_uncertain_checkout_can_be_confirmed_as_never_submitted(
    store: GroceryStore,
) -> None:
    draft_id, approval_id = approved(store)
    store.mark_revalidating(draft_id)
    store.begin_checkout(
        draft_id,
        approval_id=approval_id,
        operation_id="checkout.1",
        live=snapshot(),
        weighted_tolerance=TOLERANCE,
    )
    store.mark_checkout_uncertain("checkout.1", "response lost")

    draft = store.reconcile_uncertain(draft_id, outcome="confirmed_not_submitted")

    assert draft.state == GroceryState.REJECTED
    assert store.confirmation(draft_id) is None


def test_a_submitted_order_cannot_be_cancelled_as_unsubmitted(
    store: GroceryStore,
) -> None:
    draft_id, approval_id = approved(store)
    store.mark_revalidating(draft_id)
    store.begin_checkout(
        draft_id,
        approval_id=approval_id,
        operation_id="checkout.1",
        live=snapshot(),
        weighted_tolerance=TOLERANCE,
    )

    with pytest.raises(GroceryStateError, match="cannot be cancelled as unsubmitted"):
        store.cancel(draft_id)


def test_the_handoff_outbox_holds_one_entry_per_confirmed_order(
    store: GroceryStore,
) -> None:
    draft_id = reviewed(store)

    first = store.enqueue_handoff(draft_id, {"kind": "grocery_order_confirmed"})
    second = store.enqueue_handoff(draft_id, {"kind": "grocery_order_confirmed"})

    assert first == second
    assert [item["draft_id"] for item in store.pending_handoffs()] == [draft_id]


def test_material_differences_name_every_change_that_breaks_approval() -> None:
    approved_snapshot = snapshot()

    assert (
        material_differences(
            approved_snapshot, approved_snapshot, weighted_tolerance=TOLERANCE
        )
        == ()
    )
    assert "fulfilment slot changed" in material_differences(
        approved_snapshot,
        snapshot(fulfilment=slot("slot.friday", hours=48)),
        weighted_tolerance=TOLERANCE,
    )
    assert "fulfilment mode changed" in material_differences(
        approved_snapshot,
        snapshot(mode=FulfilmentMode.COLLECTION),
        weighted_tolerance=TOLERANCE,
    )
    assert "substitutions changed" in material_differences(
        approved_snapshot,
        snapshot(substitutions=(substitution(),)),
        weighted_tolerance=TOLERANCE,
    )
    assert "fees changed" in material_differences(
        approved_snapshot, snapshot(fees="3.50"), weighted_tolerance=TOLERANCE
    )
    assert "basket items changed" in material_differences(
        approved_snapshot,
        snapshot((basket_item(), basket_item("milk", product_id="wr.milk"))),
        weighted_tolerance=TOLERANCE,
    )
    assert "item oats changed" in material_differences(
        approved_snapshot,
        snapshot((basket_item(quantity=3),)),
        weighted_tolerance=TOLERANCE,
    )


def test_a_fixed_price_change_breaks_approval_but_weighted_drift_may_not() -> None:
    approved_snapshot = snapshot((basket_item(unit_price="2.00"),))
    nudged = snapshot((basket_item(unit_price="2.40"),))

    differences = material_differences(
        approved_snapshot, nudged, weighted_tolerance=TOLERANCE
    )
    assert "fixed-price item oats changed price" in differences

    weighted_approved = snapshot(
        (basket_item(unit_price="2.00", variable_weight=True),)
    )
    weighted_live = snapshot((basket_item(unit_price="2.40", variable_weight=True),))
    assert (
        material_differences(
            weighted_approved, weighted_live, weighted_tolerance=TOLERANCE
        )
        == ()
    )

    beyond = snapshot((basket_item(unit_price="4.00", variable_weight=True),))
    assert "total moved beyond the weighted-item tolerance" in material_differences(
        weighted_approved, beyond, weighted_tolerance=TOLERANCE
    )


def test_the_approval_digest_ignores_capture_time_but_not_content() -> None:
    first = snapshot(captured_at=NOW)
    later = snapshot(captured_at=NOW + timedelta(minutes=5))
    changed = snapshot((basket_item(quantity=2),))

    assert first.digest == later.digest
    assert first.digest != changed.digest


def test_confirmation_and_slot_timestamps_must_carry_an_offset() -> None:
    with pytest.raises(ValueError, match="offset"):
        OrderConfirmation(
            order_number="WR-1",
            confirmed_at=datetime(2026, 9, 11, 9, 0),
            evidence="Naive timestamp.",
        )
    with pytest.raises(ValueError, match="timezone offsets"):
        slot().model_copy(
            update={"starts_at": datetime(2026, 9, 11, 9, 0)}
        ).model_validate(
            {
                **slot().model_dump(),
                "starts_at": datetime(2026, 9, 11, 9, 0),
            }
        )
    with pytest.raises(ValueError, match="end after it starts"):
        slot().model_validate(
            {
                **slot().model_dump(),
                "ends_at": datetime(2026, 9, 11, 1, 0, tzinfo=UTC),
            }
        )
