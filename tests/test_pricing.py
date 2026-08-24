from decimal import Decimal

import pytest

from ariadne.pricing import CODEX_FLEX_RATES, codex_flex_equivalent


@pytest.mark.parametrize(
    ("model", "input_rate", "cached_rate", "output_rate"),
    [
        ("gpt-5.6-sol", "125", "12.5", "750"),
        ("gpt-5.6-terra", "50", "5", "300"),
        ("gpt-5.6-luna", "5", "0.5", "30"),
    ],
)
def test_flex_rate_card(
    model: str, input_rate: str, cached_rate: str, output_rate: str
) -> None:
    rate = CODEX_FLEX_RATES[model]

    assert rate.input_credits_per_million == Decimal(input_rate)
    assert rate.cached_input_credits_per_million == Decimal(cached_rate)
    assert rate.output_credits_per_million == Decimal(output_rate)


def test_flex_equivalent_uses_uncached_cached_and_output_categories() -> None:
    equivalent = codex_flex_equivalent(
        "gpt-5.6-sol",
        input_tokens=1_000_000,
        cached_input_tokens=900_000,
        output_tokens=10_000,
    )

    assert equivalent is not None
    assert equivalent.credits == Decimal("31.25")
    assert equivalent.usd == Decimal("1.25")


def test_flex_equivalent_does_not_double_charge_reasoning_or_cache_writes() -> None:
    equivalent = codex_flex_equivalent(
        "gpt-5.6-luna",
        input_tokens=2_000_000,
        cached_input_tokens=1_000_000,
        output_tokens=1_000_000,
    )

    assert equivalent is not None
    assert equivalent.credits == Decimal("35.5")


def test_flex_equivalent_refuses_to_guess_for_unknown_models() -> None:
    assert (
        codex_flex_equivalent(
            "gpt-unknown",
            input_tokens=1_000,
            cached_input_tokens=500,
            output_tokens=100,
        )
        is None
    )
