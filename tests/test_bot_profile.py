from pathlib import Path
from typing import Any

import pytest
from telegram import InputProfilePhotoStatic

from ariadne.config import load_settings
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
    bot = FakeBot("token-for-test")
    monkeypatch.setattr(bot_profile, "Bot", lambda token: bot)
    return bot


def config(tmp_path: Path, identity: str = "") -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    path = tmp_path / "config.toml"
    path.write_text(
        f'''\
version = 1
human_name = "Divy"
vault = "{tmp_path}"
[telegram]
bot_token = "token-for-test"
allowed_user_id = 7
{identity}
''',
        encoding="utf-8",
    )
    return path


async def test_only_the_fields_set_in_toml_are_applied(
    telegram: FakeBot, tmp_path: Path
) -> None:
    path = config(
        tmp_path,
        """\
[telegram.identity]
name = "Iris"
short_description = "Your other half."
""",
    )

    await configure(load_settings(path, environ={}))

    assert telegram.calls == [
        ("name", "Iris"),
        ("short_description", "Your other half."),
    ]


async def test_a_profile_photo_is_read_from_the_configured_path(
    telegram: FakeBot, tmp_path: Path
) -> None:
    photo = tmp_path / "iris.jpg"
    photo.write_bytes(b"not a real image")
    path = config(
        tmp_path,
        f'''\
[telegram.identity]
profile_photo = "{photo}"
''',
    )

    await configure(load_settings(path, environ={}))

    [(field, value)] = telegram.calls
    assert field == "photo"
    assert isinstance(value, InputProfilePhotoStatic)


async def test_nothing_set_leaves_the_bot_profile_untouched(
    telegram: FakeBot, tmp_path: Path
) -> None:
    await configure(load_settings(config(tmp_path), environ={}))

    assert telegram.calls == []
