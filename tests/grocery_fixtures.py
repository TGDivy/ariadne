"""Shared synthetic grocery records; no real retailer data appears here."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from ariadne.grocery.models import (
    BasketItem,
    ConstraintKind,
    FulfilmentMode,
    FulfilmentSlot,
    OmittedItem,
    OrderSnapshot,
    PreferenceEvidence,
    PreferenceKind,
    PreferenceStrength,
    ProductCandidate,
    ProductConstraint,
    RequestedItem,
    Substitution,
    SubstitutionSource,
)

NOW = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)


def requested(
    item_id: str = "oats",
    query: str = "porridge oats",
    *,
    quantity: int = 1,
    allow_substitution: bool = True,
) -> RequestedItem:
    return RequestedItem(
        item_id=item_id,
        query=query,
        quantity=quantity,
        allow_substitution=allow_substitution,
    )


def candidate(
    product_id: str = "wr.oats",
    name: str = "Porridge Oats",
    *,
    unit_price: str = "1.50",
    quantity: int = 1,
    favourite: bool = False,
    prior_order_count: int = 0,
    dietary_tags: tuple[str, ...] = (),
    variable_weight: bool = False,
) -> ProductCandidate:
    price = Decimal(unit_price)
    return ProductCandidate(
        product_id=product_id,
        name=name,
        size="1kg",
        unit_price=price,
        estimated_line_total=(price * quantity).quantize(Decimal("0.01")),
        variable_weight=variable_weight,
        favourite=favourite,
        prior_order_count=prior_order_count,
        dietary_tags=dietary_tags,
    )


def basket_item(
    requested_item_id: str = "oats",
    *,
    product_id: str = "wr.oats",
    name: str = "Porridge Oats",
    quantity: int = 1,
    unit_price: str = "1.50",
    variable_weight: bool = False,
) -> BasketItem:
    price = Decimal(unit_price)
    return BasketItem(
        requested_item_id=requested_item_id,
        product_id=product_id,
        name=name,
        size="1kg",
        quantity=quantity,
        unit_price=price,
        line_total=(price * quantity).quantize(Decimal("0.01")),
        variable_weight=variable_weight,
    )


def slot(
    slot_id: str = "slot.thursday",
    *,
    mode: FulfilmentMode = FulfilmentMode.DELIVERY,
    fee: str = "0.00",
    location: str = "12 Example Street",
    hours: int = 24,
) -> FulfilmentSlot:
    starts = NOW + timedelta(hours=hours)
    return FulfilmentSlot(
        slot_id=slot_id,
        mode=mode,
        starts_at=starts,
        ends_at=starts + timedelta(hours=1),
        fee=Decimal(fee),
        location=location,
    )


def snapshot(
    items: tuple[BasketItem, ...] | None = None,
    *,
    mode: FulfilmentMode = FulfilmentMode.DELIVERY,
    fulfilment: FulfilmentSlot | None = None,
    fees: str = "0.00",
    substitutions: tuple[Substitution, ...] = (),
    omitted: tuple[OmittedItem, ...] = (),
    minimum_spend: str | None = None,
    captured_at: datetime | None = None,
) -> OrderSnapshot:
    lines = items if items is not None else (basket_item(),)
    window = fulfilment if fulfilment is not None else slot(mode=mode)
    subtotal = sum((line.line_total for line in lines), Decimal("0.00"))
    fee = Decimal(fees)
    return OrderSnapshot(
        items=lines,
        substitutions=substitutions,
        omitted=omitted,
        mode=mode,
        slot=window,
        location=window.location,
        subtotal=subtotal,
        fees=fee,
        total=subtotal + fee,
        minimum_spend=Decimal(minimum_spend) if minimum_spend is not None else None,
        captured_at=captured_at or NOW,
    )


def allergy(name: str = "Peanut allergy", term: str = "peanut") -> ProductConstraint:
    return ProductConstraint(
        name=name,
        kind=ConstraintKind.ALLERGY,
        excluded_terms=(term,),
        strength=PreferenceStrength.FIRM,
    )


def preference(
    pattern: str,
    *,
    kind: PreferenceKind = PreferenceKind.PREFER,
    strength: PreferenceStrength = PreferenceStrength.TENTATIVE,
) -> PreferenceEvidence:
    return PreferenceEvidence(pattern=pattern, kind=kind, strength=strength)


def substitution(
    requested_item_id: str = "oats",
    *,
    source: SubstitutionSource = SubstitutionSource.IRIS,
) -> Substitution:
    return Substitution(
        requested_item_id=requested_item_id,
        requested="porridge oats",
        replacement_product_id="wr.jumbo",
        replacement="Jumbo Oats",
        source=source,
    )
