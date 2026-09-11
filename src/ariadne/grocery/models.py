"""Durable semantic records for carefully approved grocery orders."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]
Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$",
    ),
]
BoundedText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class GroceryState(StrEnum):
    """Explicit stages of one durable order draft."""

    UNDERSTAND = "understand"
    BUILD = "build"
    RESOLVE = "resolve"
    REVIEW = "review"
    APPROVED = "approved"
    REVALIDATE = "revalidate"
    CHECKOUT = "checkout"
    NEEDS_TAKEOVER = "needs_takeover"
    UNCERTAIN = "uncertain"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class FulfilmentMode(StrEnum):
    DELIVERY = "delivery"
    COLLECTION = "collection"


class PreferenceStrength(StrEnum):
    FIRM = "firm"
    OBSERVED = "observed"
    TENTATIVE = "tentative"


class PreferenceKind(StrEnum):
    PREFER = "prefer"
    AVOID = "avoid"
    ALLOWED_SUBSTITUTION = "allowed_substitution"


class ConstraintKind(StrEnum):
    ALLERGY = "allergy"
    DIETARY = "dietary"
    DISLIKE = "dislike"


class SubstitutionSource(StrEnum):
    IRIS = "iris"
    RETAILER = "retailer"
    REUSABLE_RULE = "reusable_rule"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    USED = "used"


class CheckoutStatus(StrEnum):
    SUBMITTING = "submitting"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    UNCERTAIN = "uncertain"


class DownstreamStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class GroceryModel(BaseModel):
    """Strict immutable base used by records persisted as JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class RequestedItem(GroceryModel):
    item_id: Identifier
    query: BoundedText
    quantity: int = Field(default=1, ge=1, le=99)
    notes: str | None = Field(default=None, max_length=500)
    allow_substitution: bool = True

    @field_validator("notes", mode="before")
    @classmethod
    def empty_notes_are_absent(cls, value: object) -> object:
        return value.strip() or None if isinstance(value, str) else value


class PreferenceEvidence(GroceryModel):
    """An editable preference, distinguished from a mere prior purchase."""

    pattern: BoundedText
    kind: PreferenceKind = PreferenceKind.PREFER
    strength: PreferenceStrength = PreferenceStrength.TENTATIVE
    product_id: Identifier | None = None
    reason: str | None = Field(default=None, max_length=500)


class ProductConstraint(GroceryModel):
    """A firm boundary or softer dislike applied during candidate matching."""

    name: BoundedText
    kind: ConstraintKind
    excluded_terms: tuple[BoundedText, ...] = ()
    strength: PreferenceStrength = PreferenceStrength.FIRM


class ProductCandidate(GroceryModel):
    product_id: Identifier
    name: BoundedText
    size: str | None = Field(default=None, max_length=200)
    unit_price: Money
    estimated_line_total: Money
    variable_weight: bool = False
    favourite: bool = False
    prior_order_count: int = Field(default=0, ge=0)
    evidence: tuple[str, ...] = ()
    dietary_tags: tuple[str, ...] = ()
    match_score: int = 0


class BasketItem(GroceryModel):
    requested_item_id: Identifier
    product_id: Identifier
    name: BoundedText
    size: str | None = Field(default=None, max_length=200)
    quantity: int = Field(ge=1, le=99)
    unit_price: Money
    line_total: Money
    variable_weight: bool = False
    evidence: tuple[str, ...] = ()


class Substitution(GroceryModel):
    requested_item_id: Identifier
    requested: BoundedText
    replacement_product_id: Identifier
    replacement: BoundedText
    source: SubstitutionSource
    reusable_rule: bool = False
    reason: str | None = Field(default=None, max_length=500)


class OmittedItem(GroceryModel):
    requested_item_id: Identifier
    requested: BoundedText
    reason: BoundedText


class FulfilmentSlot(GroceryModel):
    slot_id: Identifier
    mode: FulfilmentMode
    starts_at: datetime
    ends_at: datetime
    fee: Money = Decimal("0.00")
    location: BoundedText
    convenient: bool | None = None

    @field_validator("ends_at")
    @classmethod
    def end_follows_start(cls, value: datetime, info: Any) -> datetime:
        start = info.data.get("starts_at")
        if isinstance(start, datetime) and value <= start:
            raise ValueError("A fulfilment slot must end after it starts.")
        return value

    @field_validator("starts_at", "ends_at")
    @classmethod
    def timestamps_include_offsets(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Fulfilment timestamps must include timezone offsets.")
        return value


class OrderSnapshot(GroceryModel):
    """The complete material order state to which approval is bound."""

    retailer: Literal["waitrose"] = "waitrose"
    items: tuple[BasketItem, ...]
    substitutions: tuple[Substitution, ...] = ()
    omitted: tuple[OmittedItem, ...] = ()
    mode: FulfilmentMode
    slot: FulfilmentSlot
    location: BoundedText
    subtotal: Money
    fees: Money = Decimal("0.00")
    total: Money
    minimum_spend: Money | None = None
    captured_at: datetime

    @field_validator("captured_at")
    @classmethod
    def captured_at_includes_offset(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Snapshot timestamps must include a timezone offset.")
        return value

    def material_payload(self) -> dict[str, object]:
        """Return a stable, private-data-minimal approval binding."""
        return {
            "retailer": self.retailer,
            "items": [
                {
                    "requested_item_id": item.requested_item_id,
                    "product_id": item.product_id,
                    "name": item.name,
                    "size": item.size,
                    "quantity": item.quantity,
                    "unit_price": str(item.unit_price),
                    "line_total": str(item.line_total),
                    "variable_weight": item.variable_weight,
                }
                for item in sorted(
                    self.items, key=lambda value: value.requested_item_id
                )
            ],
            "substitutions": [
                substitution.model_dump(mode="json")
                for substitution in sorted(
                    self.substitutions, key=lambda value: value.requested_item_id
                )
            ],
            "omitted": [
                omitted.model_dump(mode="json")
                for omitted in sorted(
                    self.omitted, key=lambda value: value.requested_item_id
                )
            ],
            "mode": self.mode.value,
            "slot": self.slot.model_dump(mode="json"),
            "location": self.location,
            "subtotal": str(self.subtotal),
            "fees": str(self.fees),
            "total": str(self.total),
            "minimum_spend": (
                str(self.minimum_spend) if self.minimum_spend is not None else None
            ),
        }

    @property
    def digest(self) -> str:
        encoded = json.dumps(
            self.material_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class BrowserAttachment(GroceryModel):
    task_id: Identifier
    lease_token: BoundedText
    profile: BoundedText


class GroceryDraft(GroceryModel):
    draft_id: Identifier
    task_id: Identifier
    state: GroceryState
    revision: int = Field(ge=1)
    retailer: Literal["waitrose"] = "waitrose"
    requested_items: tuple[RequestedItem, ...]
    requested_window: str | None = Field(default=None, max_length=500)
    mode: FulfilmentMode | None = None
    location: str | None = Field(default=None, max_length=500)
    preferences: tuple[PreferenceEvidence, ...] = ()
    constraints: tuple[ProductConstraint, ...] = ()
    candidates: dict[str, tuple[ProductCandidate, ...]] = Field(default_factory=dict)
    basket: tuple[BasketItem, ...] = ()
    substitutions: tuple[Substitution, ...] = ()
    omitted: tuple[OmittedItem, ...] = ()
    slots: tuple[FulfilmentSlot, ...] = ()
    selected_slot: FulfilmentSlot | None = None
    snapshot: OrderSnapshot | None = None
    browser: BrowserAttachment | None = None
    issue: str | None = Field(default=None, max_length=1_000)
    created_at: datetime
    updated_at: datetime

    def public_payload(self) -> dict[str, object]:
        payload = self.model_dump(mode="json", exclude={"browser"})
        payload["browser_attached"] = self.browser is not None
        if self.snapshot is not None:
            payload["snapshot_digest"] = self.snapshot.digest
        return payload


class GroceryApproval(GroceryModel):
    approval_id: Identifier
    draft_id: Identifier
    revision: int = Field(ge=1)
    snapshot_digest: BoundedText
    owner_user_id: int = Field(gt=0)
    chat_id: int
    message_id: int | None = None
    status: ApprovalStatus
    expires_at: datetime
    created_at: datetime
    decided_at: datetime | None = None


class CheckoutOperation(GroceryModel):
    operation_id: Identifier
    draft_id: Identifier
    approval_id: Identifier
    status: CheckoutStatus
    result: dict[str, object] | None = None
    created_at: datetime
    updated_at: datetime


class OrderConfirmation(GroceryModel):
    order_number: BoundedText
    status: Literal["confirmed"] = "confirmed"
    confirmed_at: datetime
    evidence: str = Field(min_length=1, max_length=1_000)

    @field_validator("confirmed_at")
    @classmethod
    def confirmed_at_includes_offset(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Confirmation timestamps must include an offset.")
        return value


class DownstreamRecord(GroceryModel):
    draft_id: Identifier
    calendar_status: DownstreamStatus = DownstreamStatus.PENDING
    calendar_reference: str | None = None
    calendar_error: str | None = None
    knowledge_status: DownstreamStatus = DownstreamStatus.PENDING
    knowledge_reference: str | None = None
    knowledge_error: str | None = None
    handoff_status: DownstreamStatus = DownstreamStatus.PENDING
    handoff_reference: str | None = None
    handoff_error: str | None = None


def material_differences(
    approved: OrderSnapshot,
    live: OrderSnapshot,
    *,
    weighted_tolerance: Decimal,
) -> tuple[str, ...]:
    """Explain every change that invalidates an exact checkout approval."""
    differences: list[str] = []
    if approved.retailer != live.retailer:
        differences.append("retailer changed")
    if approved.mode != live.mode:
        differences.append("fulfilment mode changed")
    if approved.location != live.location:
        differences.append("fulfilment location changed")
    if approved.slot.model_dump(mode="json") != live.slot.model_dump(mode="json"):
        differences.append("fulfilment slot changed")
    if approved.substitutions != live.substitutions:
        differences.append("substitutions changed")
    if approved.omitted != live.omitted:
        differences.append("omitted items changed")
    if approved.fees != live.fees:
        differences.append("fees changed")
    if approved.minimum_spend != live.minimum_spend:
        differences.append("minimum spend changed")

    approved_items = {item.requested_item_id: item for item in approved.items}
    live_items = {item.requested_item_id: item for item in live.items}
    if approved_items.keys() != live_items.keys():
        differences.append("basket items changed")
    for item_id in approved_items.keys() & live_items.keys():
        old = approved_items[item_id]
        new = live_items[item_id]
        identity = (
            old.product_id,
            old.name,
            old.size,
            old.quantity,
            old.variable_weight,
        )
        if identity != (
            new.product_id,
            new.name,
            new.size,
            new.quantity,
            new.variable_weight,
        ):
            differences.append(f"item {item_id} changed")
        elif not old.variable_weight and (
            old.unit_price != new.unit_price or old.line_total != new.line_total
        ):
            differences.append(f"fixed-price item {item_id} changed price")

    if abs(live.total - approved.total) > weighted_tolerance:
        differences.append("total moved beyond the weighted-item tolerance")
    if abs(live.subtotal - approved.subtotal) > weighted_tolerance:
        differences.append("subtotal moved beyond the weighted-item tolerance")
    return tuple(dict.fromkeys(differences))


__all__ = [
    "ApprovalStatus",
    "BasketItem",
    "BrowserAttachment",
    "CheckoutOperation",
    "CheckoutStatus",
    "ConstraintKind",
    "DownstreamRecord",
    "DownstreamStatus",
    "FulfilmentMode",
    "FulfilmentSlot",
    "GroceryApproval",
    "GroceryDraft",
    "GroceryState",
    "Money",
    "OmittedItem",
    "OrderConfirmation",
    "OrderSnapshot",
    "PreferenceEvidence",
    "PreferenceKind",
    "PreferenceStrength",
    "ProductCandidate",
    "ProductConstraint",
    "RequestedItem",
    "Substitution",
    "SubstitutionSource",
    "material_differences",
]
