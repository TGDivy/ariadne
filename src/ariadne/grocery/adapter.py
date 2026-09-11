"""Small retailer contract above the generic private browser service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import (
    BasketItem,
    FulfilmentMode,
    FulfilmentSlot,
    GroceryDraft,
    OrderConfirmation,
    OrderSnapshot,
    ProductCandidate,
    RequestedItem,
)


@dataclass(frozen=True, slots=True)
class CheckoutTarget:
    """A fresh semantic checkout button and its browser-state binding."""

    ref: str
    page_revision: int
    state_digest: str


@dataclass(frozen=True, slots=True)
class CheckoutResult:
    """The only three safe interpretations of a checkout response."""

    status: str
    confirmation: OrderConfirmation | None = None
    reason: str | None = None


class RetailerAdapter(Protocol):
    """Semantic retailer operations; page selectors never cross this boundary."""

    async def search(
        self, draft: GroceryDraft, request: RequestedItem
    ) -> tuple[ProductCandidate, ...]: ...

    async def add_product(
        self,
        draft: GroceryDraft,
        request: RequestedItem,
        candidate: ProductCandidate,
    ) -> BasketItem: ...

    async def list_slots(
        self, draft: GroceryDraft, mode: FulfilmentMode
    ) -> tuple[FulfilmentSlot, ...]: ...

    async def choose_slot(self, draft: GroceryDraft, slot: FulfilmentSlot) -> None: ...

    async def read_basket(self, draft: GroceryDraft) -> OrderSnapshot: ...

    async def prepare_checkout(self, draft: GroceryDraft) -> CheckoutTarget: ...

    async def submit_checkout(
        self,
        draft: GroceryDraft,
        target: CheckoutTarget,
        *,
        operation_id: str,
        approval_id: str,
    ) -> CheckoutResult: ...

    async def request_takeover(
        self, draft: GroceryDraft, reason: str
    ) -> str | None: ...

    async def resume(self, draft: GroceryDraft) -> None: ...

    async def release(self, draft: GroceryDraft) -> None: ...


__all__ = ["CheckoutResult", "CheckoutTarget", "RetailerAdapter"]
