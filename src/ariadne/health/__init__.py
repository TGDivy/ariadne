"""Typed access to Ariadne's configured health-data boundary."""

from .client import (
    IthacaAuthenticationError,
    IthacaClient,
    IthacaError,
    IthacaNotFoundError,
    IthacaRequestError,
    IthacaResponseError,
    IthacaUnavailableError,
)
from .models import WorkoutActivityType

__all__ = [
    "IthacaAuthenticationError",
    "IthacaClient",
    "IthacaError",
    "IthacaNotFoundError",
    "IthacaRequestError",
    "IthacaResponseError",
    "IthacaUnavailableError",
    "WorkoutActivityType",
]
