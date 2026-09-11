"""Waitrose grocery ordering MCP capability."""

from __future__ import annotations

import os
import sqlite3
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from ..browser.capability import SOCKET_ENVIRONMENT as BROWSER_SOCKET_ENVIRONMENT
from ..browser.client import BrowserClient
from ..grocery.capability import (
    APPROVAL_TTL_ENVIRONMENT,
    BASE_URL_ENVIRONMENT,
    CHECKOUT_ENVIRONMENT,
    PROFILE_ENVIRONMENT,
    STATE_ENVIRONMENT,
    TOLERANCE_ENVIRONMENT,
)
from ..grocery.errors import GroceryError
from ..grocery.models import (
    FulfilmentMode,
    PreferenceEvidence,
    ProductConstraint,
    RequestedItem,
    SubstitutionSource,
)
from ..grocery.service import GroceryService
from ..grocery.store import GroceryStore
from ..grocery.telegram import wait_for_owner_approval
from ..grocery.waitrose import WaitroseAdapter
from ..telegram.history import TelegramMessageStore
from ..telegram.questions import QUESTION_STATE_ENVIRONMENT

MAX_REQUESTED_ITEMS = 60


def _required(name: str, description: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ToolError(f"{description} is not configured for this turn.")
    return value


def _service() -> GroceryService:
    """Build the turn-scoped service from the profile's private environment."""
    store = GroceryStore(Path(_required(STATE_ENVIRONMENT, "Grocery ordering")))
    socket = Path(_required(BROWSER_SOCKET_ENVIRONMENT, "The private browser"))
    try:
        tolerance = Decimal(os.environ.get(TOLERANCE_ENVIRONMENT, "1.00"))
        approval_ttl = int(os.environ.get(APPROVAL_TTL_ENVIRONMENT, "900"))
    except (InvalidOperation, ValueError) as error:
        raise ToolError("Grocery ordering limits are misconfigured.") from error
    adapter = WaitroseAdapter(
        store,
        BrowserClient(socket),
        profile=_required(PROFILE_ENVIRONMENT, "The grocery browser profile"),
        base_url=os.environ.get(BASE_URL_ENVIRONMENT, "https://www.waitrose.com"),
    )
    try:
        return GroceryService(
            store,
            adapter,
            weighted_tolerance=tolerance,
            approval_ttl_seconds=approval_ttl,
            checkout_enabled=os.environ.get(CHECKOUT_ENVIRONMENT, "0") == "1",
        )
    except ValueError as error:
        raise ToolError(str(error)) from error


def _fail(error: Exception) -> ToolError:
    return ToolError(str(error))


def _items(items: list[dict[str, Any]]) -> tuple[RequestedItem, ...]:
    if not items:
        raise ToolError("A grocery order needs at least one requested item.")
    if len(items) > MAX_REQUESTED_ITEMS:
        raise ToolError(f"Keep a grocery order under {MAX_REQUESTED_ITEMS} items.")
    try:
        return tuple(RequestedItem.model_validate(item) for item in items)
    except ValueError as error:
        raise ToolError(f"A requested grocery item was invalid: {error}") from error


def _preferences(values: list[dict[str, Any]]) -> tuple[PreferenceEvidence, ...]:
    try:
        return tuple(PreferenceEvidence.model_validate(value) for value in values)
    except ValueError as error:
        raise ToolError(f"A grocery preference was invalid: {error}") from error


def _constraints(values: list[dict[str, Any]]) -> tuple[ProductConstraint, ...]:
    try:
        return tuple(ProductConstraint.model_validate(value) for value in values)
    except ValueError as error:
        raise ToolError(f"A grocery constraint was invalid: {error}") from error


def start_grocery_order(
    items: list[dict[str, Any]],
    mode: FulfilmentMode | None = None,
    requested_window: str | None = None,
    preferences: list[dict[str, Any]] | None = None,
    constraints: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Open one durable Waitrose order draft from an understood request.

    Each item needs `item_id`, `query`, an optional `quantity`, `notes`, and
    `allow_substitution`. Supply `preferences` (`pattern`, `kind`, `strength`)
    and `constraints` (`name`, `kind`, `excluded_terms`) from what you actually
    know; mark a habit `observed` or `tentative` rather than `firm`. A prior
    order is evidence, not permission to rebuy. Leave `mode` unset and ask when
    delivery and collection would mean materially different logistics.
    """
    try:
        draft = _service().start(
            _items(items),
            mode=mode,
            requested_window=requested_window,
            preferences=_preferences(preferences or []),
            constraints=_constraints(constraints or []),
        )
    except (GroceryError, ValueError, sqlite3.Error) as error:
        raise _fail(error) from error
    return {"draft": draft.public_payload()}


def read_grocery_order(draft_id: str) -> dict[str, Any]:
    """Read one durable grocery draft, including its current state and basket."""
    try:
        service = _service()
        draft = service.store.get(draft_id)
        approval = service.store.live_approval(draft_id)
        checkout = service.store.checkout(draft_id)
        downstream = service.store.downstream(draft_id)
    except (GroceryError, sqlite3.Error) as error:
        raise _fail(error) from error
    return {
        "draft": draft.public_payload(),
        "approval": approval.model_dump(mode="json") if approval else None,
        "checkout": checkout.model_dump(mode="json") if checkout else None,
        "follow_through": downstream.model_dump(mode="json") if downstream else None,
    }


def set_grocery_fulfilment(
    draft_id: str,
    mode: FulfilmentMode,
    requested_window: str | None = None,
) -> dict[str, Any]:
    """Record delivery or collection once it is genuinely known, not assumed."""
    try:
        draft = _service().set_fulfilment(
            draft_id, mode, requested_window=requested_window
        )
    except (GroceryError, sqlite3.Error) as error:
        raise _fail(error) from error
    return {"draft": draft.public_payload()}


async def search_grocery_products(draft_id: str, item_id: str) -> dict[str, Any]:
    """Search Waitrose for one requested item and rank allowed candidates.

    Candidates that break a firm dietary or allergy constraint are removed, and
    each remaining one carries the evidence behind its rank.
    """
    try:
        candidates = await _service().search(draft_id, item_id)
    except (GroceryError, sqlite3.Error) as error:
        raise _fail(error) from error
    return {
        "item_id": item_id,
        "candidates": [candidate.model_dump(mode="json") for candidate in candidates],
    }


async def add_grocery_product(
    draft_id: str,
    item_id: str,
    product_id: str,
    substitution_source: SubstitutionSource | None = None,
    substitution_reason: str | None = None,
) -> dict[str, Any]:
    """Add one searched product to the live basket at the requested quantity.

    A product whose name is not the requested one is a substitution and must
    say where it came from: your own `iris` recommendation, the retailer's
    `retailer` proposal, or an existing `reusable_rule` Divy already recorded.
    Substitutions stay visible at review.
    """
    try:
        draft = await _service().add_product(
            draft_id,
            item_id,
            product_id,
            substitution_source=substitution_source,
            substitution_reason=substitution_reason,
        )
    except (GroceryError, sqlite3.Error) as error:
        raise _fail(error) from error
    return {"draft": draft.public_payload()}


def omit_grocery_item(draft_id: str, item_id: str, reason: str) -> dict[str, Any]:
    """Record an unavailable or deliberately dropped item with its real reason."""
    try:
        draft = _service().omit(draft_id, item_id, reason)
    except (GroceryError, sqlite3.Error) as error:
        raise _fail(error) from error
    return {"draft": draft.public_payload()}


async def list_grocery_slots(
    draft_id: str, mode: FulfilmentMode | None = None
) -> dict[str, Any]:
    """Read the live delivery or collection windows Waitrose currently offers."""
    try:
        draft = await _service().list_slots(draft_id, mode)
    except (GroceryError, sqlite3.Error) as error:
        raise _fail(error) from error
    return {"draft": draft.public_payload()}


async def choose_grocery_slot(draft_id: str, slot_id: str) -> dict[str, Any]:
    """Reserve one listed slot. Do not silently take an inconvenient or paid one."""
    try:
        draft = await _service().choose_slot(draft_id, slot_id)
    except (GroceryError, sqlite3.Error) as error:
        raise _fail(error) from error
    return {"draft": draft.public_payload()}


async def review_and_approve_grocery_order(draft_id: str) -> dict[str, Any]:
    """Re-read the live basket, show Divy the exact order, and wait for approval.

    This sends one compact Telegram review card with the retailer, fulfilment
    mode, slot, items, substitutions, omissions, fees, and current total, then
    blocks this turn until Divy approves or cancels. Approval binds to exactly
    that basket and expires; adding items, changing substitutions or slot, or
    crossing the price tolerance invalidates it and returns here.
    """
    service = _service()
    token = _required("TELEGRAM_BOT_TOKEN", "Telegram")
    try:
        owner = int(_required("TELEGRAM_ALLOWED_USER_ID", "Telegram"))
    except ValueError as error:
        raise ToolError("Telegram is not reachable from this runtime.") from error
    history = TelegramMessageStore(
        Path(_required(QUESTION_STATE_ENVIRONMENT, "Private Telegram state"))
    )
    try:
        approval = await wait_for_owner_approval(
            service,
            draft_id,
            token=token,
            owner_user_id=owner,
            history=history,
        )
    except (GroceryError, sqlite3.Error) as error:
        raise _fail(error) from error
    return {
        "approval": approval.model_dump(mode="json"),
        "draft": service.store.get(draft_id).public_payload(),
    }


async def checkout_grocery_order(draft_id: str) -> dict[str, Any]:
    """Revalidate the approved order and submit it exactly once.

    Returns `confirmed` with a definitive order number, `review_required` when
    the live basket moved, `rejected` when Waitrose definitively refused, or
    `uncertain`. Never call this again after `uncertain`: inspect Waitrose's own
    order history and tell Divy instead.
    """
    try:
        return await _service().checkout(draft_id)
    except (GroceryError, sqlite3.Error) as error:
        raise _fail(error) from error


async def cancel_grocery_order(
    draft_id: str, reason: str = "Cancelled by owner"
) -> dict[str, Any]:
    """Abandon an unsubmitted order and release the browser profile."""
    try:
        draft = await _service().cancel(draft_id, reason)
    except (GroceryError, sqlite3.Error) as error:
        raise _fail(error) from error
    return {"draft": draft.public_payload()}


async def request_grocery_takeover(draft_id: str, reason: str) -> dict[str, Any]:
    """Pause and ask Divy to take the browser over privately.

    Use this for login, MFA, CAPTCHA, a consent change, or an unfamiliar
    checkout step. Never enter credentials or work around a site control.
    """
    try:
        return await _service().request_takeover(draft_id, reason)
    except (GroceryError, sqlite3.Error) as error:
        raise _fail(error) from error


async def resume_grocery_order(draft_id: str) -> dict[str, Any]:
    """Continue after Divy releases the browser; the basket must be re-reviewed."""
    try:
        draft = await _service().resume(draft_id)
    except (GroceryError, sqlite3.Error) as error:
        raise _fail(error) from error
    return {"draft": draft.public_payload()}


async def repair_grocery_follow_through(draft_id: str) -> dict[str, Any]:
    """Retry the Calendar, knowledge, and conversational writes for one order.

    Only a definitively confirmed order has repairable follow-through. Each
    write is independent, so a failure is retried without redoing the others.
    """
    try:
        return await _service().repair_downstream(draft_id)
    except (GroceryError, ValueError, sqlite3.Error) as error:
        raise _fail(error) from error


Annotations = dict[str, bool]


def register_tools(server: FastMCP) -> None:
    """Register grocery operations with their real-world action hints."""
    read_only: Annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    private_write: Annotations = {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    retailer_write: Annotations = {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    consequential: Annotations = {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }
    server.tool(start_grocery_order, annotations=private_write)
    server.tool(read_grocery_order, annotations=read_only)
    server.tool(set_grocery_fulfilment, annotations=private_write)
    server.tool(search_grocery_products, annotations=retailer_write)
    server.tool(add_grocery_product, annotations=retailer_write)
    server.tool(omit_grocery_item, annotations=private_write)
    server.tool(list_grocery_slots, annotations=retailer_write)
    server.tool(choose_grocery_slot, annotations=retailer_write)
    server.tool(review_and_approve_grocery_order, annotations=retailer_write)
    server.tool(checkout_grocery_order, annotations=consequential)
    server.tool(cancel_grocery_order, annotations=retailer_write)
    server.tool(request_grocery_takeover, annotations=retailer_write)
    server.tool(resume_grocery_order, annotations=retailer_write)
    server.tool(repair_grocery_follow_through, annotations=private_write)


__all__ = [
    "add_grocery_product",
    "cancel_grocery_order",
    "checkout_grocery_order",
    "choose_grocery_slot",
    "list_grocery_slots",
    "omit_grocery_item",
    "read_grocery_order",
    "register_tools",
    "repair_grocery_follow_through",
    "request_grocery_takeover",
    "resume_grocery_order",
    "review_and_approve_grocery_order",
    "search_grocery_products",
    "set_grocery_fulfilment",
    "start_grocery_order",
]
