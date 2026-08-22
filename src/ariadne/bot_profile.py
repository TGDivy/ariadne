"""One-off configuration of Ariadne's Telegram bot identity."""

import asyncio
import os
from pathlib import Path

from telegram import Bot, InputProfilePhotoStatic


async def configure() -> None:
    """Apply whichever bot-identity fields are set in the environment."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    applied: list[str] = []

    async with Bot(token) as bot:
        name = os.environ.get("ARIADNE_BOT_NAME")
        if name:
            await bot.set_my_name(name)
            applied.append(f"name: {name!r}")

        description = os.environ.get("ARIADNE_BOT_DESCRIPTION")
        if description:
            await bot.set_my_description(description)
            applied.append("description")

        short_description = os.environ.get("ARIADNE_BOT_SHORT_DESCRIPTION")
        if short_description:
            await bot.set_my_short_description(short_description)
            applied.append("short description")

        photo = os.environ.get("ARIADNE_BOT_PROFILE_PHOTO")
        if photo:
            await bot.set_my_profile_photo(
                InputProfilePhotoStatic(Path(photo).expanduser())
            )
            applied.append(f"profile photo: {photo}")

    if applied:
        print("Updated " + ", ".join(applied) + ".")
    else:
        print(
            "Nothing to set. Set any of ARIADNE_BOT_NAME, ARIADNE_BOT_DESCRIPTION, "
            "ARIADNE_BOT_SHORT_DESCRIPTION, ARIADNE_BOT_PROFILE_PHOTO and run again."
        )


def main() -> None:
    """Run once: `uv run --env-file .env python -m ariadne.bot_profile`."""
    asyncio.run(configure())


if __name__ == "__main__":
    main()
