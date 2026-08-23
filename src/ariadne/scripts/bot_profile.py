"""One-off configuration of Ariadne's Telegram bot identity."""

import argparse
import asyncio
from pathlib import Path

from telegram import Bot, InputProfilePhotoStatic

from ariadne.config import Settings, load_settings


async def configure(settings: Settings) -> None:
    """Apply whichever bot-identity fields are configured."""
    identity = settings.telegram.identity
    applied: list[str] = []

    async with Bot(settings.telegram_bot_token) as bot:
        if identity.name:
            await bot.set_my_name(identity.name)
            applied.append(f"name: {identity.name!r}")

        if identity.description:
            await bot.set_my_description(identity.description)
            applied.append("description")

        if identity.short_description:
            await bot.set_my_short_description(identity.short_description)
            applied.append("short description")

        if identity.profile_photo:
            await bot.set_my_profile_photo(
                InputProfilePhotoStatic(identity.profile_photo)
            )
            applied.append(f"profile photo: {identity.profile_photo}")

    if applied:
        print("Updated " + ", ".join(applied) + ".")
    else:
        print("Nothing to set under [telegram.identity].")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    asyncio.run(configure(load_settings(args.config)))


if __name__ == "__main__":
    main()
