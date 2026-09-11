"""Deterministic product matching that keeps evidence and constraints visible."""

from __future__ import annotations

import re

from .errors import GroceryStateError
from .models import (
    ConstraintKind,
    PreferenceEvidence,
    PreferenceKind,
    PreferenceStrength,
    ProductCandidate,
    ProductConstraint,
    RequestedItem,
)

_TOKEN = re.compile(r"[a-z0-9]+")
_STRENGTH_SCORE = {
    PreferenceStrength.FIRM: 120,
    PreferenceStrength.OBSERVED: 35,
    PreferenceStrength.TENTATIVE: 10,
}


def normalized(value: str) -> str:
    return " ".join(_TOKEN.findall(value.casefold()))


def _matches(candidate: ProductCandidate, evidence: PreferenceEvidence) -> bool:
    if evidence.product_id is not None and evidence.product_id == candidate.product_id:
        return True
    return normalized(evidence.pattern) in normalized(candidate.name)


def constraint_violations(
    candidate: ProductCandidate,
    constraints: tuple[ProductConstraint, ...],
) -> tuple[str, ...]:
    haystack = normalized(" ".join((candidate.name, *candidate.dietary_tags)))
    violations = []
    for constraint in constraints:
        if constraint.strength != PreferenceStrength.FIRM:
            continue
        terms = tuple(normalized(term) for term in constraint.excluded_terms)
        if any(term and term in haystack for term in terms):
            violations.append(constraint.name)
    return tuple(violations)


def rank_candidates(
    request: RequestedItem,
    candidates: tuple[ProductCandidate, ...],
    *,
    preferences: tuple[PreferenceEvidence, ...] = (),
    constraints: tuple[ProductConstraint, ...] = (),
) -> tuple[ProductCandidate, ...]:
    """Rank allowed candidates without turning order history into permission."""
    request_tokens = set(_TOKEN.findall(request.query.casefold()))
    ranked: list[ProductCandidate] = []
    for candidate in candidates:
        if constraint_violations(candidate, constraints):
            continue
        candidate_tokens = set(_TOKEN.findall(candidate.name.casefold()))
        overlap = len(request_tokens & candidate_tokens)
        score = overlap * 15 - len(request_tokens - candidate_tokens) * 4
        if normalized(request.query) == normalized(candidate.name):
            score += 80
        elif normalized(request.query) in normalized(candidate.name):
            score += 35
        if candidate.favourite:
            score += 20
        if candidate.prior_order_count:
            score += min(candidate.prior_order_count, 5) * 3
        evidence = list(candidate.evidence)
        for preference in preferences:
            if not _matches(candidate, preference):
                continue
            weight = _STRENGTH_SCORE[preference.strength]
            if preference.kind == PreferenceKind.AVOID:
                score -= weight
                evidence.append(
                    f"{preference.strength.value} avoidance: {preference.pattern}"
                )
            else:
                score += weight
                evidence.append(
                    f"{preference.strength.value} preference: {preference.pattern}"
                )
        ranked.append(
            candidate.model_copy(
                update={"match_score": score, "evidence": tuple(evidence)}
            )
        )
    return tuple(
        sorted(
            ranked,
            key=lambda value: (
                -value.match_score,
                value.estimated_line_total,
                value.name.casefold(),
            ),
        )
    )


def require_safe_substitution(
    request: RequestedItem,
    candidate: ProductCandidate,
    *,
    preferences: tuple[PreferenceEvidence, ...],
    constraints: tuple[ProductConstraint, ...],
) -> None:
    """Reject substitutions that lack permission across a firm safety boundary."""
    if not request.allow_substitution:
        raise GroceryStateError(f"{request.query} does not allow substitutions.")
    violations = constraint_violations(candidate, constraints)
    if violations:
        raise GroceryStateError(
            "Substitution conflicts with firm constraint(s): " + ", ".join(violations)
        )
    engaged = engaged_safety_constraints(request, constraints)
    if not engaged:
        return
    reusable = any(
        preference.kind == PreferenceKind.ALLOWED_SUBSTITUTION
        and preference.strength == PreferenceStrength.FIRM
        and _matches(candidate, preference)
        for preference in preferences
    )
    if not reusable:
        names = ", ".join(constraint.name for constraint in engaged)
        raise GroceryStateError(
            "A replacement cannot cross an allergy/dietary boundary without a "
            f"firm reusable rule ({names}). Keep it as a review proposal instead."
        )


def engaged_safety_constraints(
    request: RequestedItem,
    constraints: tuple[ProductConstraint, ...],
) -> tuple[ProductConstraint, ...]:
    """Return the firm allergy/dietary rules this specific request depends on.

    A recorded allergy must not freeze every unrelated swap, so a constraint is
    engaged only when the requested product itself names it — asking for "oat
    milk" under a firm dairy rule, for instance. Any replacement that would
    itself violate a constraint is already rejected before this runs.
    """
    requested = normalized(request.query)
    engaged: list[ProductConstraint] = []
    for constraint in constraints:
        if constraint.kind not in {ConstraintKind.ALLERGY, ConstraintKind.DIETARY}:
            continue
        if constraint.strength != PreferenceStrength.FIRM:
            continue
        markers = (constraint.name, *constraint.excluded_terms)
        if any(
            normalized(marker) and normalized(marker) in requested for marker in markers
        ):
            engaged.append(constraint)
    return tuple(engaged)


__all__ = [
    "constraint_violations",
    "engaged_safety_constraints",
    "normalized",
    "rank_candidates",
    "require_safe_substitution",
]
