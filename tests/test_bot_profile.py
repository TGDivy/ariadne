from pathlib import Path
from typing import Any

import pytest
from telegram import InputProfilePhotoStatic

from ariadne.scripts import bot_profile
from ariadne.scripts.bot_profile import configure


class FakeBot:
    def __init__(self, token: str) -> None:
        self.token = token
        self.calls: list[tuple[str, Any]] = []

    async def __aenter__(self) -> "FakeBot":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def set_my_name(self, name: str) -> bool:
        self.calls.append(("name", name))
        return True

    async def set_my_description(self, description: str) -> bool:
        self.calls.append(("description", description))
        return True

    async def set_my_short_description(self, short_description: str) -> bool:
        self.calls.append(("short_description", short_description))
        return True

    async def set_my_profile_photo(self, photo: InputProfilePhotoStatic) -> bool:
        self.calls.append(("photo", photo))
        return True


@pytest.fixture
def telegram(monkeypatch) -> FakeBot:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token-for-test")
    bot = FakeBot("token-for-test")
    monkeypatch.setattr(bot_profile, "Bot", lambda token: bot)
    return bot


async def test_only_the_fields_set_in_the_environment_are_applied(
    telegram: FakeBot, monkeypatch
) -> None:
    monkeypatch.setenv("ARIADNE_BOT_NAME", "Iris")
    monkeypatch.setenv("ARIADNE_BOT_SHORT_DESCRIPTION", "Your other half.")

    await configure()

    assert telegram.calls == [
        ("name", "Iris"),
        ("short_description", "Your other half."),
    ]


async def test_a_profile_photo_is_read_from_the_configured_path(
    telegram: FakeBot, monkeypatch, tmp_path: Path
) -> None:
    photo = tmp_path / "iris.jpg"
    photo.write_bytes(b"not a real image")
    monkeypatch.setenv("ARIADNE_BOT_PROFILE_PHOTO", str(photo))

    await configure()

    [(field, value)] = telegram.calls
    assert field == "photo"
    assert isinstance(value, InputProfilePhotoStatic)


async def test_nothing_set_leaves_the_bot_profile_untouched(telegram: FakeBot) -> None:
    await configure()

    assert telegram.calls == []
