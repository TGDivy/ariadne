"""Stable failures for the grocery ordering boundary."""


class GroceryError(RuntimeError):
    """Base class for safe grocery failures."""


class GroceryStateError(GroceryError):
    """A requested draft transition is invalid or stale."""


class GroceryApprovalError(GroceryError):
    """Checkout lacks a current exact owner approval."""


class GroceryAdapterError(GroceryError):
    """A retailer page could not be interpreted conservatively."""


class GroceryTakeoverRequired(GroceryAdapterError):
    """The owner must privately complete login, MFA, or another challenge."""


class GroceryUncertainError(GroceryError):
    """Checkout may already have been submitted and must not be retried."""


__all__ = [
    "GroceryAdapterError",
    "GroceryApprovalError",
    "GroceryError",
    "GroceryStateError",
    "GroceryTakeoverRequired",
    "GroceryUncertainError",
]
