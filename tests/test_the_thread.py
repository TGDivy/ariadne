from pathlib import Path

from ariadne.the_thread import build_developer_instructions


def test_core_documents_are_snapshotted_into_new_thread_instructions(
    tmp_path: Path,
) -> None:
    identity = tmp_path / "Ariadne" / "Identity.md"
    identity.parent.mkdir()
    identity.write_text("Be direct and kind.", encoding="utf-8")

    rules = tmp_path / "Ariadne" / "OperatingRules.md"
    rules.write_text("Ask before changing the mission.", encoding="utf-8")

    profile = tmp_path / "Ariadne" / "Profile.md"
    profile.write_text("This belongs in the vault, not the prompt.", encoding="utf-8")

    instructions = build_developer_instructions(tmp_path)

    assert "Be direct and kind." in instructions
    assert "Ask before changing the mission." in instructions
    assert "This belongs in the vault, not the prompt." not in instructions
