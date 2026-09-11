from __future__ import annotations

import pytest
from grocery_fixtures import allergy, candidate, preference, requested

from ariadne.grocery.errors import GroceryStateError
from ariadne.grocery.matching import (
    constraint_violations,
    normalized,
    rank_candidates,
    require_safe_substitution,
)
from ariadne.grocery.models import (
    ConstraintKind,
    PreferenceEvidence,
    PreferenceKind,
    PreferenceStrength,
    ProductConstraint,
)


def test_normalization_ignores_punctuation_and_case() -> None:
    assert normalized("Waitrose  Duchy ORGANIC, Oats!") == "waitrose duchy organic oats"


def test_an_exact_name_outranks_a_merely_overlapping_product() -> None:
    ranked = rank_candidates(
        requested(query="porridge oats"),
        (
            candidate("wr.jumbo", "Jumbo Porridge Oats With Honey"),
            candidate("wr.exact", "Porridge Oats"),
        ),
    )

    assert [item.product_id for item in ranked] == ["wr.exact", "wr.jumbo"]


def test_preference_strength_orders_candidates_and_records_its_evidence() -> None:
    ranked = rank_candidates(
        requested(query="oats"),
        (
            candidate("wr.plain", "Plain Oats"),
            candidate("wr.duchy", "Duchy Organic Oats"),
        ),
        preferences=(preference("Duchy Organic", strength=PreferenceStrength.FIRM),),
    )

    assert ranked[0].product_id == "wr.duchy"
    assert "firm preference: Duchy Organic" in ranked[0].evidence

    tentative = rank_candidates(
        requested(query="oats"),
        (
            candidate("wr.plain", "Plain Oats", unit_price="0.90"),
            candidate("wr.duchy", "Duchy Organic Oats", unit_price="3.50"),
        ),
        preferences=(
            preference("Duchy Organic", strength=PreferenceStrength.TENTATIVE),
        ),
    )

    # A tentative habit nudges the order; it does not overwhelm a closer match.
    assert {item.product_id for item in tentative} == {"wr.plain", "wr.duchy"}
    assert (
        "tentative preference: Duchy Organic"
        in tentative[0].evidence + tentative[1].evidence
    )


def test_an_avoidance_pushes_a_disliked_product_down_without_removing_it() -> None:
    ranked = rank_candidates(
        requested(query="oats"),
        (candidate("wr.honey", "Oats With Honey"), candidate("wr.plain", "Oats")),
        preferences=(
            preference(
                "With Honey",
                kind=PreferenceKind.AVOID,
                strength=PreferenceStrength.OBSERVED,
            ),
        ),
    )

    assert [item.product_id for item in ranked] == ["wr.plain", "wr.honey"]
    assert "observed avoidance: With Honey" in ranked[1].evidence


def test_favourites_and_prior_orders_are_evidence_not_permission() -> None:
    ranked = rank_candidates(
        requested(query="oats"),
        (
            candidate("wr.plain", "Oats"),
            candidate("wr.favourite", "Oats", favourite=True, prior_order_count=4),
        ),
    )

    assert ranked[0].product_id == "wr.favourite"
    # The alternative is still offered rather than silently rebought.
    assert len(ranked) == 2


def test_a_firm_constraint_removes_a_candidate_entirely() -> None:
    unsafe = candidate("wr.peanut", "Peanut Butter Oats")

    assert constraint_violations(unsafe, (allergy(),)) == ("Peanut allergy",)
    assert rank_candidates(
        requested(query="oats"),
        (unsafe, candidate("wr.plain", "Oats")),
        constraints=(allergy(),),
    ) == rank_candidates(requested(query="oats"), (candidate("wr.plain", "Oats"),))


def test_a_dietary_tag_can_trip_a_firm_constraint() -> None:
    tagged = candidate("wr.milk", "Breakfast Oats", dietary_tags=("contains milk",))
    dairy = ProductConstraint(
        name="Dairy free",
        kind=ConstraintKind.DIETARY,
        excluded_terms=("milk",),
        strength=PreferenceStrength.FIRM,
    )

    assert constraint_violations(tagged, (dairy,)) == ("Dairy free",)


def test_a_soft_dislike_does_not_remove_a_candidate() -> None:
    disliked = ProductConstraint(
        name="Not keen on honey",
        kind=ConstraintKind.DISLIKE,
        excluded_terms=("honey",),
        strength=PreferenceStrength.OBSERVED,
    )
    honey = candidate("wr.honey", "Oats With Honey")

    assert constraint_violations(honey, (disliked,)) == ()


def test_substitution_is_refused_across_a_firm_dietary_boundary() -> None:
    dairy = ProductConstraint(
        name="Dairy free",
        kind=ConstraintKind.DIETARY,
        excluded_terms=("milk",),
        strength=PreferenceStrength.FIRM,
    )

    with pytest.raises(GroceryStateError, match="firm constraint"):
        require_safe_substitution(
            requested(query="oat milk"),
            candidate("wr.dairy", "Semi Skimmed Milk", dietary_tags=("milk",)),
            preferences=(),
            constraints=(dairy,),
        )

    # A safe product still needs an explicit reusable rule to cross the boundary.
    with pytest.raises(GroceryStateError, match="reusable rule"):
        require_safe_substitution(
            requested(query="oat milk"),
            candidate("wr.soya", "Soya Drink"),
            preferences=(),
            constraints=(dairy,),
        )

    require_safe_substitution(
        requested(query="oat milk"),
        candidate("wr.soya", "Soya Drink"),
        preferences=(
            PreferenceEvidence(
                pattern="Soya Drink",
                kind=PreferenceKind.ALLOWED_SUBSTITUTION,
                strength=PreferenceStrength.FIRM,
            ),
        ),
        constraints=(dairy,),
    )


def test_an_item_marked_no_substitutions_is_never_replaced() -> None:
    with pytest.raises(GroceryStateError, match="does not allow substitutions"):
        require_safe_substitution(
            requested(query="porridge oats", allow_substitution=False),
            candidate("wr.jumbo", "Jumbo Oats"),
            preferences=(),
            constraints=(),
        )


def test_substitution_is_free_when_no_safety_boundary_applies() -> None:
    require_safe_substitution(
        requested(query="porridge oats"),
        candidate("wr.jumbo", "Jumbo Oats"),
        preferences=(),
        constraints=(allergy(),),
    )
