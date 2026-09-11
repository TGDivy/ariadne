from pathlib import Path

import pytest

from ariadne.telegram.panels import TelegramControlPanel, TelegramControlPanelStore


def test_panel_store_keeps_one_restart_safe_panel_per_chat(tmp_path: Path) -> None:
    path = tmp_path / "private" / "telegram.sqlite3"
    first = TelegramControlPanelStore(path)
    first.set(TelegramControlPanel(7, 10, "settings"))
    first.set(TelegramControlPanel(7, 11, "status"))
    first.set(TelegramControlPanel(8, 12, "wakeups"))

    restarted = TelegramControlPanelStore(path)

    assert restarted.get(7) == TelegramControlPanel(7, 11, "status")
    assert restarted.get(8) == TelegramControlPanel(8, 12, "wakeups")
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert path.stat().st_mode & 0o777 == 0o600


def test_panel_cleanup_is_guarded_against_stale_races(tmp_path: Path) -> None:
    state = TelegramControlPanelStore(tmp_path / "telegram.sqlite3")
    state.set(TelegramControlPanel(7, 11, "status"))

    assert state.clear(7, expected_message_id=10) is False
    assert state.get(7) == TelegramControlPanel(7, 11, "status")
    assert state.clear(7, expected_message_id=11) is True
    assert state.get(7) is None


def test_panel_kind_must_be_short_and_visible(tmp_path: Path) -> None:
    state = TelegramControlPanelStore(tmp_path / "telegram.sqlite3")

    with pytest.raises(ValueError, match="short kind"):
        state.set(TelegramControlPanel(7, 11, " "))
    with pytest.raises(ValueError, match="short kind"):
        state.set(TelegramControlPanel(7, 11, "x" * 65))
