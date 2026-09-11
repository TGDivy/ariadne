from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from grocery_fixtures import (
    NOW,
    allergy,
    basket_item,
    candidate,
    preference,
    requested,
    slot,
    snapshot,
)

from ariadne.browser.client import BrowserRemoteError
from ariadne.grocery.adapter import CheckoutResult, CheckoutTarget
from ariadne.grocery.downstream import OrderDownstream
from ariadne.grocery.errors import (
    GroceryAdapterError,
    GroceryApprovalError,
    GroceryStateError,
    GroceryTakeoverRequired,
)
from ariadne.grocery.models import (
    BasketItem,
    DownstreamStatus,
    FulfilmentMode,
    FulfilmentSlot,
    GroceryDraft,
    GroceryState,
    OrderConfirmation,
    OrderSnapshot,
    PreferenceStrength,
    ProductCandidate,
    RequestedItem,
    SubstitutionSource,
)
from ariadne.grocery.service import GroceryService
from ariadne.grocery.store import GroceryStore

OWNER = 7


class FakeAdapter:
    """A retailer double that records what the service actually asked it to do."""

    def __init__(
        self,
        *,
        candidates: tuple[ProductCandidate, ...] = (),
        slots: tuple[FulfilmentSlot, ...] = (),
        basket: OrderSnapshot | None = None,
        result: CheckoutResult | None = None,
    ) -> None:
        self.candidates = candidates or (candidate(),)
        self.slots = slots or (slot(),)
        self.basket = basket
        self.result = result or CheckoutResult(
            "confirmed",
            confirmation=OrderConfirmation(
                order_number="WR-FIXTURE-1",
                confirmed_at=NOW,
                evidence="Fixture confirmation page.",
            ),
        )
        self.baskets_read = 0
        self.submissions = 0
        self.released = 0
        self.takeovers: list[str] = []
        self.resumed = 0
        self.search_error: Exception | None = None
        self.basket_error: Exception | None = None
        self.submit_error: Exception | None = None

    async def search(
        self, draft: GroceryDraft, request: RequestedItem
    ) -> tuple[ProductCandidate, ...]:
        if self.search_error is not None:
            raise self.search_error
        return self.candidates

    async def add_product(
        self,
        draft: GroceryDraft,
        request: RequestedItem,
        chosen: ProductCandidate,
    ) -> BasketItem:
        return basket_item(
            request.item_id,
            product_id=chosen.product_id,
            name=chosen.name,
            quantity=request.quantity,
            unit_price=str(chosen.unit_price),
            variable_weight=chosen.variable_weight,
        )

    async def list_slots(
        self, draft: GroceryDraft, mode: FulfilmentMode
    ) -> tuple[FulfilmentSlot, ...]:
        return tuple(value for value in self.slots if value.mode == mode)

    async def choose_slot(self, draft: GroceryDraft, chosen: FulfilmentSlot) -> None:
        return None

    async def read_basket(self, draft: GroceryDraft) -> OrderSnapshot:
        self.baskets_read += 1
        if self.basket_error is not None:
            raise self.basket_error
        if self.basket is not None:
            return self.basket
        assert draft.selected_slot is not None
        return snapshot(
            draft.basket,
            mode=draft.mode or FulfilmentMode.DELIVERY,
            fulfilment=draft.selected_slot,
        )

    async def prepare_checkout(self, draft: GroceryDraft) -> CheckoutTarget:
        return CheckoutTarget("r1.1-4", 1, "digest")

    async def submit_checkout(
        self,
        draft: GroceryDraft,
        target: CheckoutTarget,
        *,
        operation_id: str,
        approval_id: str,
    ) -> CheckoutResult:
        self.submissions += 1
        if self.submit_error is not None:
            raise self.submit_error
        return self.result

    async def request_takeover(self, draft: GroceryDraft, reason: str) -> str | None:
        self.takeovers.append(reason)
        return "https://browser.home.private"

    async def resume(self, draft: GroceryDraft) -> None:
        self.resumed += 1

    async def release(self, draft: GroceryDraft) -> None:
        self.released += 1


def build(
    tmp_path: Path,
    adapter: FakeAdapter | None = None,
    **kwargs: object,
) -> tuple[GroceryService, FakeAdapter, GroceryStore]:
    store = GroceryStore(tmp_path / "grocery.sqlite3")
    store.initialize()
    retailer = adapter or FakeAdapter()
    service = GroceryService(store, retailer, **kwargs)  # type: ignore[arg-type]
    return service, retailer, store


async def to_review(
    service: GroceryService,
    *,
    quantity: int = 1,
    constraints: tuple[object, ...] = (),
    preferences: tuple[object, ...] = (),
) -> str:
    draft = service.start(
        (requested(quantity=quantity),),
        mode=FulfilmentMode.DELIVERY,
        constraints=constraints,  # type: ignore[arg-type]
        preferences=preferences,  # type: ignore[arg-type]
    )
    await service.search(draft.draft_id, "oats")
    await service.add_product(draft.draft_id, "oats", "wr.oats")
    await service.list_slots(draft.draft_id)
    await service.choose_slot(draft.draft_id, "slot.thursday")
    await service.prepare_review(draft.draft_id)
    return draft.draft_id


def approve(store: GroceryStore, draft_id: str) -> str:
    approval = store.create_approval(
        draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
    )
    store.attach_approval_message(approval.approval_id, 41)
    store.decide_approval(
        approval.approval_id,
        user_id=OWNER,
        chat_id=OWNER,
        message_id=41,
        decision="approve",
    )
    return approval.approval_id


def test_service_limits_are_validated(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="tolerance"):
        build(tmp_path, weighted_tolerance=Decimal("50.00"))
    with pytest.raises(ValueError, match="Approval TTL"):
        build(tmp_path, approval_ttl_seconds=5)


async def test_an_unstated_fulfilment_mode_waits_for_the_owner(
    tmp_path: Path,
) -> None:
    service, _, _ = build(tmp_path)

    draft = service.start((requested(),))

    assert draft.state == GroceryState.UNDERSTAND
    assert draft.mode is None
    with pytest.raises(GroceryStateError, match="is understand"):
        await service.list_slots(draft.draft_id)

    resolved = service.set_fulfilment(draft.draft_id, FulfilmentMode.COLLECTION)
    assert resolved.state == GroceryState.BUILD
    assert resolved.mode == FulfilmentMode.COLLECTION


async def test_slots_cannot_be_listed_while_the_logistics_are_unknown(
    tmp_path: Path,
) -> None:
    service, _, store = build(tmp_path)
    draft = service.start((requested(),))
    built = store.save(
        draft.model_copy(update={"state": GroceryState.BUILD}),
        expected_revision=draft.revision,
        reason="Owner started choosing products before deciding logistics",
    )

    assert built.mode is None
    with pytest.raises(GroceryStateError, match="delivery or collection"):
        await service.list_slots(built.draft_id)


async def test_collection_is_supported_alongside_delivery(tmp_path: Path) -> None:
    collection = slot("slot.collect", mode=FulfilmentMode.COLLECTION, location="Shop")
    service, _, _ = build(tmp_path, FakeAdapter(slots=(slot(), collection)))
    draft = service.start((requested(),), mode=FulfilmentMode.COLLECTION)
    await service.search(draft.draft_id, "oats")
    await service.add_product(draft.draft_id, "oats", "wr.oats")

    listed = await service.list_slots(draft.draft_id)

    assert [value.slot_id for value in listed.slots] == ["slot.collect"]
    chosen = await service.choose_slot(draft.draft_id, "slot.collect")
    assert chosen.mode == FulfilmentMode.COLLECTION
    assert chosen.location == "Shop"


async def test_a_non_exact_product_must_be_recorded_as_a_substitution(
    tmp_path: Path,
) -> None:
    service, _, _ = build(
        tmp_path, FakeAdapter(candidates=(candidate("wr.jumbo", "Jumbo Oats"),))
    )
    draft = service.start((requested(),), mode=FulfilmentMode.DELIVERY)
    await service.search(draft.draft_id, "oats")

    with pytest.raises(
        GroceryStateError, match="recorded explicitly as a substitution"
    ):
        await service.add_product(draft.draft_id, "oats", "wr.jumbo")

    updated = await service.add_product(
        draft.draft_id,
        "oats",
        "wr.jumbo",
        substitution_source=SubstitutionSource.IRIS,
        substitution_reason="Closest available size",
    )

    assert [value.replacement for value in updated.substitutions] == ["Jumbo Oats"]
    assert updated.substitutions[0].reusable_rule is False


async def test_a_search_must_precede_selecting_a_product(tmp_path: Path) -> None:
    service, _, _ = build(tmp_path)
    draft = service.start((requested(),), mode=FulfilmentMode.DELIVERY)

    with pytest.raises(GroceryStateError, match="Search again"):
        await service.add_product(draft.draft_id, "oats", "wr.oats")


async def test_review_requires_every_requested_item_to_be_resolved(
    tmp_path: Path,
) -> None:
    service, _, _ = build(tmp_path)
    draft = service.start(
        (requested(), requested("milk", "semi skimmed milk")),
        mode=FulfilmentMode.DELIVERY,
    )
    await service.search(draft.draft_id, "oats")
    await service.add_product(draft.draft_id, "oats", "wr.oats")
    await service.list_slots(draft.draft_id)
    await service.choose_slot(draft.draft_id, "slot.thursday")

    with pytest.raises(GroceryStateError, match="semi skimmed milk"):
        await service.prepare_review(draft.draft_id)

    service.omit(draft.draft_id, "milk", "Out of stock in every size")
    reviewed = await service.prepare_review(draft.draft_id)

    assert reviewed.state == GroceryState.REVIEW
    assert [value.reason for value in reviewed.omitted] == [
        "Out of stock in every size"
    ]


async def test_a_basket_under_the_minimum_spend_stops_before_review(
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter(basket=snapshot(minimum_spend="40.00"))
    service, _, _ = build(tmp_path, adapter)
    draft = service.start((requested(),), mode=FulfilmentMode.DELIVERY)
    await service.search(draft.draft_id, "oats")
    await service.add_product(draft.draft_id, "oats", "wr.oats")
    await service.list_slots(draft.draft_id)
    await service.choose_slot(draft.draft_id, "slot.thursday")

    with pytest.raises(GroceryStateError, match="minimum spend"):
        await service.prepare_review(draft.draft_id)


async def test_checkout_is_refused_until_it_is_deliberately_enabled(
    tmp_path: Path,
) -> None:
    service, adapter, store = build(tmp_path)
    draft_id = await to_review(service)
    approve(store, draft_id)

    with pytest.raises(GroceryApprovalError, match="disabled"):
        await service.checkout(draft_id)

    assert adapter.submissions == 0
    assert store.get(draft_id).state == GroceryState.APPROVED


async def test_checkout_without_approval_never_reaches_the_retailer(
    tmp_path: Path,
) -> None:
    service, adapter, _ = build(tmp_path, checkout_enabled=True)
    draft_id = await to_review(service)

    with pytest.raises(GroceryApprovalError, match="current exact approval"):
        await service.checkout(draft_id)

    assert adapter.submissions == 0


async def test_a_confirmed_order_records_follow_through(tmp_path: Path) -> None:
    service, adapter, store = build(tmp_path, checkout_enabled=True)
    draft_id = await to_review(service)
    approve(store, draft_id)

    result = await service.checkout(draft_id)

    assert result["status"] == "confirmed"
    assert adapter.submissions == 1
    assert adapter.released == 1
    assert store.get(draft_id).state == GroceryState.CONFIRMED
    record = store.downstream(draft_id)
    assert record is not None
    # Calendar and knowledge are not configured here, so they are honestly
    # skipped rather than silently reported as done.
    assert record.calendar_status == DownstreamStatus.SKIPPED
    assert record.knowledge_status == DownstreamStatus.SKIPPED
    assert record.handoff_status == DownstreamStatus.SUCCEEDED
    assert [item["draft_id"] for item in store.pending_handoffs()] == [draft_id]


async def test_a_basket_that_moved_returns_to_review_instead_of_checking_out(
    tmp_path: Path,
) -> None:
    service, adapter, store = build(tmp_path, checkout_enabled=True)
    draft_id = await to_review(service)
    approve(store, draft_id)
    adapter.basket = snapshot((basket_item(quantity=4),))

    result = await service.checkout(draft_id)

    assert result["status"] == "review_required"
    assert "item oats changed" in result["differences"]  # type: ignore[operator]
    assert adapter.submissions == 0
    assert store.get(draft_id).state == GroceryState.REVIEW
    assert store.live_approval(draft_id) is None


async def test_a_definitive_rejection_is_not_retried(tmp_path: Path) -> None:
    adapter = FakeAdapter(
        result=CheckoutResult("rejected", reason="payment was declined")
    )
    service, _, store = build(tmp_path, adapter, checkout_enabled=True)
    draft_id = await to_review(service)
    approve(store, draft_id)

    result = await service.checkout(draft_id)

    assert result["status"] == "rejected"
    assert store.get(draft_id).state == GroceryState.REJECTED
    assert adapter.released == 1


async def test_a_lost_submission_response_stays_uncertain(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    adapter.submit_error = BrowserRemoteError("timeout", "The action timed out.")
    service, _, store = build(tmp_path, adapter, checkout_enabled=True)
    draft_id = await to_review(service)
    approve(store, draft_id)

    result = await service.checkout(draft_id)

    assert result["status"] == "uncertain"
    assert "do not" in str(result["instruction"]).casefold()
    assert store.get(draft_id).state == GroceryState.UNCERTAIN
    # The profile is deliberately not released while the outcome is unknown.
    assert adapter.released == 0


async def test_a_response_without_a_definitive_outcome_stays_uncertain(
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter(result=CheckoutResult("uncertain", reason="no order number"))
    service, _, store = build(tmp_path, adapter, checkout_enabled=True)
    draft_id = await to_review(service)
    approve(store, draft_id)

    result = await service.checkout(draft_id)

    assert result["status"] == "uncertain"
    assert store.get(draft_id).state == GroceryState.UNCERTAIN


async def test_takeover_pauses_the_draft_and_offers_the_private_view(
    tmp_path: Path,
) -> None:
    service, adapter, store = build(tmp_path)
    draft = service.start((requested(),), mode=FulfilmentMode.DELIVERY)

    paused = await service.request_takeover(draft.draft_id, "Waitrose asked for MFA")

    assert paused["takeover_url"] == "https://browser.home.private"
    assert adapter.takeovers == ["Waitrose asked for MFA"]
    assert store.get(draft.draft_id).state == GroceryState.NEEDS_TAKEOVER

    resumed = await service.resume(draft.draft_id)
    assert adapter.resumed == 1
    assert resumed.state == GroceryState.RESOLVE
    assert resumed.snapshot is None
    assert "review" in str(resumed.issue).casefold()


async def test_login_expiry_during_revalidation_pauses_rather_than_submits(
    tmp_path: Path,
) -> None:
    service, adapter, store = build(tmp_path, checkout_enabled=True)
    draft_id = await to_review(service)
    approve(store, draft_id)
    adapter.basket_error = GroceryTakeoverRequired("Waitrose signed the session out.")

    with pytest.raises(GroceryTakeoverRequired):
        await service.checkout(draft_id)

    assert adapter.submissions == 0
    assert store.get(draft_id).state == GroceryState.NEEDS_TAKEOVER
    assert adapter.takeovers


async def test_cancellation_releases_the_browser_profile(tmp_path: Path) -> None:
    service, adapter, store = build(tmp_path)
    draft_id = await to_review(service)

    cancelled = await service.cancel(draft_id, "Changed my mind")

    assert cancelled.state == GroceryState.CANCELLED
    assert adapter.released == 1
    assert store.live_approval(draft_id) is None


async def test_a_stale_lease_does_not_break_a_recorded_cancellation(
    tmp_path: Path,
) -> None:
    class StaleAdapter(FakeAdapter):
        async def release(self, draft: GroceryDraft) -> None:
            raise GroceryAdapterError("The lease already expired.")

    service, _, store = build(tmp_path, StaleAdapter())
    draft_id = await to_review(service)

    cancelled = await service.cancel(draft_id)

    assert cancelled.state == GroceryState.CANCELLED
    assert store.get(draft_id).state == GroceryState.CANCELLED


async def test_a_firm_constraint_removes_unsafe_candidates_from_search(
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter(
        candidates=(
            candidate("wr.peanut", "Peanut Oats"),
            candidate("wr.plain", "Porridge Oats"),
        )
    )
    service, _, _ = build(tmp_path, adapter)
    draft = service.start(
        (requested(),), mode=FulfilmentMode.DELIVERY, constraints=(allergy(),)
    )

    candidates = await service.search(draft.draft_id, "oats")

    assert [item.product_id for item in candidates] == ["wr.plain"]


async def test_preferences_carry_their_evidence_into_the_draft(
    tmp_path: Path,
) -> None:
    adapter = FakeAdapter(
        candidates=(
            candidate("wr.duchy", "Duchy Organic Porridge Oats"),
            candidate("wr.plain", "Porridge Oats"),
        )
    )
    service, _, store = build(tmp_path, adapter)
    draft = service.start(
        (requested(),),
        mode=FulfilmentMode.DELIVERY,
        preferences=(
            preference("Duchy Organic", strength=PreferenceStrength.OBSERVED),
        ),
    )

    await service.search(draft.draft_id, "oats")

    stored = store.get(draft.draft_id).candidates["oats"]
    duchy = next(item for item in stored if item.product_id == "wr.duchy")
    plain = next(item for item in stored if item.product_id == "wr.plain")

    assert "observed preference: Duchy Organic" in duchy.evidence
    assert plain.evidence == ()
    # An observed habit lifts the score without overturning an exact name match.
    assert duchy.match_score > candidate("wr.duchy", duchy.name).match_score
    assert plain.match_score > duchy.match_score


async def test_follow_through_is_repairable_after_a_downstream_failure(
    tmp_path: Path,
) -> None:
    class BrokenCalendar:
        def write(self, draft: GroceryDraft, confirmation: OrderConfirmation) -> str:
            raise RuntimeError("Calendar is unreachable")

    store = GroceryStore(tmp_path / "grocery.sqlite3")
    store.initialize()
    adapter = FakeAdapter()
    broken = BrokenCalendar()
    service = GroceryService(
        store,
        adapter,  # type: ignore[arg-type]
        checkout_enabled=True,
        downstream=OrderDownstream(store, calendar=broken),  # type: ignore[arg-type]
    )
    draft_id = await to_review(service)
    approve(store, draft_id)

    result = await service.checkout(draft_id)

    assert result["status"] == "confirmed"
    record = store.downstream(draft_id)
    assert record is not None
    assert record.calendar_status == DownstreamStatus.FAILED
    assert record.handoff_status == DownstreamStatus.SUCCEEDED

    class WorkingCalendar:
        def write(self, draft: GroceryDraft, confirmation: OrderConfirmation) -> str:
            return "calendar-event-1"

    service.downstream = OrderDownstream(store, calendar=WorkingCalendar())  # type: ignore[arg-type]
    repaired = await service.repair_downstream(draft_id)

    assert repaired["calendar_status"] == "succeeded"
    assert repaired["calendar_reference"] == "calendar-event-1"
    # The handoff is not duplicated by a repair.
    assert len(store.pending_handoffs()) == 1
