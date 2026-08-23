import pytest

from ariadne.instructions import fill, render


def test_filling_refuses_to_leave_a_placeholder_unresolved() -> None:
    with pytest.raises(KeyError, match="human"):
        fill("Hello {{ human }}", {})


def test_rendering_substitutes_every_occurrence() -> None:
    rendered = render("grounding", human="Example User")

    assert "Example User's own computer" in rendered
    assert "{{" not in rendered
