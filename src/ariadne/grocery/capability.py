"""Stable names shared by grocery profiles, configuration, and the MCP tools."""

from __future__ import annotations

STATE_ENVIRONMENT = "ARIADNE_GROCERY_STATE"
PROFILE_ENVIRONMENT = "ARIADNE_GROCERY_BROWSER_PROFILE"
BASE_URL_ENVIRONMENT = "ARIADNE_GROCERY_BASE_URL"
CHECKOUT_ENVIRONMENT = "ARIADNE_GROCERY_CHECKOUT"
TOLERANCE_ENVIRONMENT = "ARIADNE_GROCERY_TOLERANCE"
APPROVAL_TTL_ENVIRONMENT = "ARIADNE_GROCERY_APPROVAL_TTL"

ENVIRONMENT_NAMES = (
    STATE_ENVIRONMENT,
    PROFILE_ENVIRONMENT,
    BASE_URL_ENVIRONMENT,
    CHECKOUT_ENVIRONMENT,
    TOLERANCE_ENVIRONMENT,
    APPROVAL_TTL_ENVIRONMENT,
)

TOOLS = (
    "start_grocery_order",
    "read_grocery_order",
    "set_grocery_fulfilment",
    "search_grocery_products",
    "add_grocery_product",
    "omit_grocery_item",
    "list_grocery_slots",
    "choose_grocery_slot",
    "review_and_approve_grocery_order",
    "checkout_grocery_order",
    "cancel_grocery_order",
    "request_grocery_takeover",
    "resume_grocery_order",
    "repair_grocery_follow_through",
)

__all__ = [
    "APPROVAL_TTL_ENVIRONMENT",
    "BASE_URL_ENVIRONMENT",
    "CHECKOUT_ENVIRONMENT",
    "ENVIRONMENT_NAMES",
    "PROFILE_ENVIRONMENT",
    "STATE_ENVIRONMENT",
    "TOLERANCE_ENVIRONMENT",
    "TOOLS",
]
