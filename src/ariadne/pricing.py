"""Dated Codex flexible-usage equivalents derived from token usage."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

CODEX_FLEX_PRICING_SNAPSHOT: Final = "2026-08-24"
CREDITS_PER_USD: Final = Decimal(25)
_TOKENS_PER_MILLION: Final = Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class FlexRate:
    """Credits charged per million tokens in each Codex token category."""

    input_credits_per_million: Decimal
    cached_input_credits_per_million: Decimal
    output_credits_per_million: Decimal


@dataclass(frozen=True, slots=True)
class FlexEquivalent:
    """Gross flexible-usage equivalent, before any included plan allowance."""

    credits: Decimal
    usd: Decimal


CODEX_FLEX_RATES: Final[dict[str, FlexRate]] = {
    "gpt-5.6-sol": FlexRate(Decimal(125), Decimal("12.5"), Decimal(750)),
    "gpt-5.6-terra": FlexRate(Decimal(50), Decimal(5), Decimal(300)),
    "gpt-5.6-luna": FlexRate(Decimal(5), Decimal("0.5"), Decimal(30)),
}


def codex_flex_equivalent(
    model: str,
    *,
    input_tokens: int,
    cached_input_tokens: int,
    output_tokens: int,
) -> FlexEquivalent | None:
    """Calculate a gross Codex flexible-usage equivalent for one turn.

    Codex reports cached input as a subset of ``input_tokens``. Only the
    remainder receives the full input rate. Unknown models return ``None`` so
    callers cannot silently apply an incorrect rate.
    """
    rate = CODEX_FLEX_RATES.get(model)
    if rate is None:
        return None

    total_input = max(input_tokens, 0)
    cached_input = min(max(cached_input_tokens, 0), total_input)
    uncached_input = total_input - cached_input
    output = max(output_tokens, 0)
    credits = (
        Decimal(uncached_input) * rate.input_credits_per_million
        + Decimal(cached_input) * rate.cached_input_credits_per_million
        + Decimal(output) * rate.output_credits_per_million
    ) / _TOKENS_PER_MILLION
    return FlexEquivalent(credits=credits, usd=credits / CREDITS_PER_USD)
