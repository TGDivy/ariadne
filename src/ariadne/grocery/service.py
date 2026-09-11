"""Order orchestration above durable state and the retailer recipe."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from ..browser.client import BrowserRemoteError
from .adapter import RetailerAdapter
from .downstream import OrderDownstream
from .errors import (
    GroceryAdapterError,
    GroceryApprovalError,
    GroceryStateError,
    GroceryTakeoverRequired,
)
from .matching import rank_candidates, require_safe_substitution
from .models import (
    FulfilmentMode,
    GroceryDraft,
    GroceryState,
    OmittedItem,
    PreferenceEvidence,
    ProductCandidate,
    ProductConstraint,
    RequestedItem,
    Substitution,
    SubstitutionSource,
    material_differences,
)
from .store import GroceryStore


class GroceryService:
    """Drive Understand → Build → Resolve → Review → Checkout → Confirm."""

    def __init__(
        self,
        store: GroceryStore,
        adapter: RetailerAdapter,
        *,
        weighted_tolerance: Decimal = Decimal("1.00"),
        approval_ttl_seconds: int = 15 * 60,
        checkout_enabled: bool = False,
        downstream: OrderDownstream | None = None,
    ) -> None:
        if weighted_tolerance < 0 or weighted_tolerance > Decimal("20.00"):
            raise ValueError("Weighted-item tolerance must be between £0 and £20.")
        if not 30 <= approval_ttl_seconds <= 3600:
            raise ValueError("Approval TTL must be between 30 and 3600 seconds.")
        self.store = store
        self.adapter = adapter
        self.weighted_tolerance = weighted_tolerance
        self.approval_ttl_seconds = approval_ttl_seconds
        self.checkout_enabled = checkout_enabled
        self.downstream = downstream or OrderDownstream(store)

    def start(
        self,
        items: tuple[RequestedItem, ...],
        *,
        mode: FulfilmentMode | None = None,
        requested_window: str | None = None,
        location: str | None = None,
        preferences: tuple[PreferenceEvidence, ...] = (),
        constraints: tuple[ProductConstraint, ...] = (),
    ) -> GroceryDraft:
        draft = self.store.create_draft(
            items,
            mode=mode,
            requested_window=requested_window,
            location=location,
            preferences=preferences,
            constraints=constraints,
        )
        if mode is None:
            return draft
        return self.store.save(
            draft.model_copy(update={"state": GroceryState.BUILD}),
            expected_revision=draft.revision,
            reason="Resolved fulfilment mode and began basket build",
        )

    def set_fulfilment(
        self,
        draft_id: str,
        mode: FulfilmentMode,
        *,
        requested_window: str | None = None,
        location: str | None = None,
    ) -> GroceryDraft:
        draft = self.store.get(draft_id)
        if draft.state not in {GroceryState.UNDERSTAND, GroceryState.RESOLVE}:
            raise GroceryStateError(
                "Fulfilment can be selected only while understanding or resolving."
            )
        state = (
            GroceryState.BUILD
            if draft.state == GroceryState.UNDERSTAND
            else draft.state
        )
        return self.store.save(
            draft.model_copy(
                update={
                    "state": state,
                    "mode": mode,
                    "requested_window": requested_window or draft.requested_window,
                    "location": location,
                    "selected_slot": None,
                    "slots": (),
                    "snapshot": None,
                }
            ),
            expected_revision=draft.revision,
            reason="Selected delivery or collection logistics",
        )

    async def search(self, draft_id: str, item_id: str) -> tuple[ProductCandidate, ...]:
        draft = self.store.get(draft_id)
        self._require_state(draft, GroceryState.BUILD, GroceryState.RESOLVE)
        request = self._request(draft, item_id)
        candidates = rank_candidates(
            request,
            await self.adapter.search(draft, request),
            preferences=draft.preferences,
            constraints=draft.constraints,
        )
        proposed_candidates = dict(draft.candidates)
        proposed_candidates[item_id] = candidates
        self.store.save(
            draft.model_copy(update={"candidates": proposed_candidates}),
            expected_revision=draft.revision,
            reason=f"Refreshed Waitrose candidates for {item_id}",
        )
        return candidates

    async def add_product(
        self,
        draft_id: str,
        item_id: str,
        product_id: str,
        *,
        substitution_source: SubstitutionSource | None = None,
        substitution_reason: str | None = None,
    ) -> GroceryDraft:
        draft = self.store.get(draft_id)
        self._require_state(draft, GroceryState.BUILD, GroceryState.RESOLVE)
        request = self._request(draft, item_id)
        candidate = next(
            (
                value
                for value in draft.candidates.get(item_id, ())
                if value.product_id == product_id
            ),
            None,
        )
        if candidate is None:
            raise GroceryStateError("Search again before selecting that product.")
        exact = normalized_equal(request.query, candidate.name)
        substitution: Substitution | None = None
        if not exact:
            require_safe_substitution(
                request,
                candidate,
                preferences=draft.preferences,
                constraints=draft.constraints,
            )
            if substitution_source is None:
                raise GroceryStateError(
                    "A non-exact product must be recorded explicitly as a substitution."
                )
            substitution = Substitution(
                requested_item_id=item_id,
                requested=request.query,
                replacement_product_id=candidate.product_id,
                replacement=candidate.name,
                source=substitution_source,
                reusable_rule=substitution_source == SubstitutionSource.REUSABLE_RULE,
                reason=substitution_reason,
            )
        item = await self.adapter.add_product(draft, request, candidate)
        basket = tuple(
            value for value in draft.basket if value.requested_item_id != item_id
        ) + (item,)
        substitutions = tuple(
            value for value in draft.substitutions if value.requested_item_id != item_id
        )
        if substitution is not None:
            substitutions += (substitution,)
        omitted = tuple(
            value for value in draft.omitted if value.requested_item_id != item_id
        )
        return self.store.save(
            draft.model_copy(
                update={
                    "basket": basket,
                    "substitutions": substitutions,
                    "omitted": omitted,
                    "state": GroceryState.BUILD,
                    "snapshot": None,
                }
            ),
            expected_revision=draft.revision,
            reason=f"Added selected Waitrose product for {item_id}",
        )

    def omit(self, draft_id: str, item_id: str, reason: str) -> GroceryDraft:
        draft = self.store.get(draft_id)
        self._require_state(draft, GroceryState.BUILD, GroceryState.RESOLVE)
        request = self._request(draft, item_id)
        omitted = tuple(
            value for value in draft.omitted if value.requested_item_id != item_id
        ) + (
            OmittedItem(
                requested_item_id=item_id,
                requested=request.query,
                reason=reason,
            ),
        )
        basket = tuple(
            value for value in draft.basket if value.requested_item_id != item_id
        )
        substitutions = tuple(
            value for value in draft.substitutions if value.requested_item_id != item_id
        )
        return self.store.save(
            draft.model_copy(
                update={
                    "omitted": omitted,
                    "basket": basket,
                    "substitutions": substitutions,
                    "state": GroceryState.RESOLVE,
                    "snapshot": None,
                }
            ),
            expected_revision=draft.revision,
            reason=f"Recorded unavailable or deliberately omitted item {item_id}",
        )

    async def list_slots(
        self, draft_id: str, mode: FulfilmentMode | None = None
    ) -> GroceryDraft:
        draft = self.store.get(draft_id)
        self._require_state(draft, GroceryState.BUILD, GroceryState.RESOLVE)
        selected_mode = mode or draft.mode
        if selected_mode is None:
            raise GroceryStateError("Ask whether this is delivery or collection first.")
        slots = await self.adapter.list_slots(draft, selected_mode)
        return self.store.save(
            draft.model_copy(
                update={
                    "state": GroceryState.RESOLVE,
                    "mode": selected_mode,
                    "slots": slots,
                    "selected_slot": None,
                    "snapshot": None,
                }
            ),
            expected_revision=draft.revision,
            reason=f"Read live Waitrose {selected_mode.value} slots",
        )

    async def choose_slot(self, draft_id: str, slot_id: str) -> GroceryDraft:
        draft = self.store.get(draft_id)
        self._require_state(draft, GroceryState.RESOLVE)
        slot = next((value for value in draft.slots if value.slot_id == slot_id), None)
        if slot is None:
            raise GroceryStateError("List fresh slots before choosing that window.")
        await self.adapter.choose_slot(draft, slot)
        return self.store.save(
            draft.model_copy(
                update={
                    "selected_slot": slot,
                    "mode": slot.mode,
                    "location": slot.location,
                    "snapshot": None,
                }
            ),
            expected_revision=draft.revision,
            reason="Selected the explicit live fulfilment slot",
        )

    async def prepare_review(self, draft_id: str) -> GroceryDraft:
        draft = self.store.get(draft_id)
        self._require_state(draft, GroceryState.RESOLVE)
        resolved = {item.requested_item_id for item in draft.basket} | {
            item.requested_item_id for item in draft.omitted
        }
        missing = [
            item.query for item in draft.requested_items if item.item_id not in resolved
        ]
        if missing:
            raise GroceryStateError(
                "Resolve every requested item before review: " + ", ".join(missing)
            )
        snapshot = await self.adapter.read_basket(draft)
        if (
            snapshot.minimum_spend is not None
            and snapshot.subtotal < snapshot.minimum_spend
        ):
            raise GroceryStateError(
                f"Waitrose minimum spend is £{snapshot.minimum_spend}; the basket is "
                f"£{snapshot.subtotal}."
            )
        return self.store.save(
            draft.model_copy(
                update={
                    "state": GroceryState.REVIEW,
                    "snapshot": snapshot,
                    "basket": snapshot.items,
                    "issue": None,
                }
            ),
            expected_revision=draft.revision,
            reason="Captured exact live basket for owner review",
        )

    async def checkout(self, draft_id: str) -> dict[str, object]:
        if not self.checkout_enabled:
            raise GroceryApprovalError(
                "Real grocery checkout is disabled. Complete documented dry runs and "
                "enable it only for an owner-observed low-value order."
            )
        draft, approval = self.store.mark_revalidating(draft_id)
        approved = draft.snapshot
        assert approved is not None
        try:
            live = await self.adapter.read_basket(draft)
        except GroceryTakeoverRequired:
            await self._pause_for_takeover(
                draft, "Private attention before revalidation"
            )
            raise
        differences = material_differences(
            approved,
            live,
            weighted_tolerance=self.weighted_tolerance,
        )
        if differences:
            reviewed = self.store.return_to_review(
                draft_id,
                live,
                reason="; ".join(differences),
            )
            return {
                "status": "review_required",
                "differences": list(differences),
                "draft": reviewed.public_payload(),
            }
        target = await self.adapter.prepare_checkout(draft)
        remaining = int((approval.expires_at - datetime.now(UTC)).total_seconds())
        if remaining < 30:
            self.store.return_to_review(
                draft_id, live, reason="Approval expired before final checkout"
            )
            raise GroceryApprovalError("Approval expired before final checkout.")
        operation_id = f"checkout.{draft.draft_id}.{draft.revision}"
        await self._record_browser_confirmation(
            draft,
            target,
            approval.approval_id,
            min(remaining, 3600),
        )
        operation = self.store.begin_checkout(
            draft_id,
            approval_id=approval.approval_id,
            operation_id=operation_id,
            live=live,
            weighted_tolerance=self.weighted_tolerance,
        )
        if operation.status.value == "succeeded":
            return {
                "status": "confirmed",
                "checkout": operation.model_dump(mode="json"),
            }
        try:
            result = await self.adapter.submit_checkout(
                self.store.get(draft_id),
                target,
                operation_id=operation_id,
                approval_id=approval.approval_id,
            )
        except (BrowserRemoteError, GroceryAdapterError) as error:
            uncertain = self.store.mark_checkout_uncertain(operation_id, str(error))
            return {
                "status": "uncertain",
                "checkout": uncertain.model_dump(mode="json"),
                "instruction": (
                    "Inspect Waitrose orders, confirmation page, and mail; do not "
                    "submit again unless the owner verifies it was not submitted."
                ),
            }
        if result.status == "rejected":
            rejected = self.store.reject_checkout(
                operation_id, result.reason or "Waitrose definitively rejected checkout"
            )
            await self._release_safely(self.store.get(draft_id))
            return {"status": "rejected", "checkout": rejected.model_dump(mode="json")}
        if result.status != "confirmed" or result.confirmation is None:
            uncertain = self.store.mark_checkout_uncertain(
                operation_id, result.reason or "Checkout response was not definitive"
            )
            return {
                "status": "uncertain",
                "checkout": uncertain.model_dump(mode="json"),
                "instruction": (
                    "Do not retry; reconcile against definitive retailer state."
                ),
            }
        confirmed = self.store.confirm_checkout(operation_id, result.confirmation)
        downstream = await self.downstream.repair(draft_id)
        await self._release_safely(self.store.get(draft_id))
        return {
            "status": "confirmed",
            "order": result.confirmation.model_dump(mode="json"),
            "checkout": confirmed.model_dump(mode="json"),
            "downstream": downstream.model_dump(mode="json"),
        }

    async def cancel(
        self, draft_id: str, reason: str = "Cancelled by owner"
    ) -> GroceryDraft:
        draft = self.store.cancel(draft_id, reason=reason)
        await self._release_safely(draft)
        return draft

    async def request_takeover(self, draft_id: str, reason: str) -> dict[str, object]:
        draft = self.store.get(draft_id)
        return await self._pause_for_takeover(draft, reason)

    async def resume(self, draft_id: str) -> GroceryDraft:
        draft = self.store.get(draft_id)
        self._require_state(draft, GroceryState.NEEDS_TAKEOVER)
        await self.adapter.resume(draft)
        return self.store.save(
            draft.model_copy(
                update={
                    "state": GroceryState.RESOLVE,
                    "issue": "Review basket and slots after owner takeover",
                    "snapshot": None,
                }
            ),
            expected_revision=draft.revision,
            reason="Resumed after private takeover; review required",
        )

    async def repair_downstream(self, draft_id: str) -> dict[str, object]:
        return (await self.downstream.repair(draft_id)).model_dump(mode="json")

    async def _pause_for_takeover(
        self, draft: GroceryDraft, reason: str
    ) -> dict[str, object]:
        takeover_url = await self.adapter.request_takeover(draft, reason)
        if draft.state != GroceryState.NEEDS_TAKEOVER:
            draft = self.store.save(
                draft.model_copy(
                    update={"state": GroceryState.NEEDS_TAKEOVER, "issue": reason}
                ),
                expected_revision=draft.revision,
                reason="Paused for private browser takeover",
            )
        return {"draft": draft.public_payload(), "takeover_url": takeover_url}

    async def _record_browser_confirmation(
        self,
        draft: GroceryDraft,
        target: object,
        approval_id: str,
        ttl_seconds: int,
    ) -> None:
        session = getattr(self.adapter, "session", None)
        confirmation = getattr(session, "confirmation", None)
        if confirmation is None:
            # Test adapters can enforce equivalent semantics internally. Production's
            # Waitrose adapter always exposes the trusted browser confirmation method.
            return
        await confirmation(
            draft,
            target,
            approval_id=approval_id,
            ttl_seconds=ttl_seconds,
        )

    async def _release_safely(self, draft: GroceryDraft) -> None:
        try:
            await self.adapter.release(draft)
        except GroceryAdapterError:
            # The durable terminal state is authoritative; a stale lease is recoverable.
            return

    @staticmethod
    def _request(draft: GroceryDraft, item_id: str) -> RequestedItem:
        request = next(
            (item for item in draft.requested_items if item.item_id == item_id), None
        )
        if request is None:
            raise GroceryStateError("Requested grocery item does not exist.")
        return request

    @staticmethod
    def _require_state(draft: GroceryDraft, *states: GroceryState) -> None:
        if draft.state not in states:
            names = ", ".join(state.value for state in states)
            raise GroceryStateError(
                f"Grocery draft is {draft.state.value}; expected one of: {names}."
            )


def normalized_equal(left: str, right: str) -> bool:
    """Treat punctuation/case variants as the same requested product name."""
    from .matching import normalized

    return normalized(left) == normalized(right)


__all__ = ["GroceryService", "normalized_equal"]
