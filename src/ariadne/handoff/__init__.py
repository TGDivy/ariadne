"""Durable background handoffs into the shared Telegram conversation."""

from .coordinator import HandoffCoordinator, HandoffTurnTarget
from .models import ConversationHandoff, HandoffStatus
from .state import (
    ACTIVATION_KEY_ENVIRONMENT,
    ACTIVATION_SOURCE_ENVIRONMENT,
    STATE_ENVIRONMENT,
    HandoffError,
    HandoffState,
)

__all__ = [
    "ACTIVATION_KEY_ENVIRONMENT",
    "ACTIVATION_SOURCE_ENVIRONMENT",
    "STATE_ENVIRONMENT",
    "ConversationHandoff",
    "HandoffCoordinator",
    "HandoffError",
    "HandoffState",
    "HandoffStatus",
    "HandoffTurnTarget",
]
