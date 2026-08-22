"""Runnable GitHub webhook listener that triggers independent Codex turns."""
# ruff: noqa: E501

import asyncio
import logging
from collections.abc import Callable

import uvicorn
from fastapi import FastAPI, HTTPException, Request, status

from .codex import CodexConversation
from .config import GitHubWebhookSettings, Settings
from .github_webhook import (
    GitHubWebhookEvent,
    WebhookRejectedError,
    parse_verified_event,
)

LOGGER = logging.getLogger(__name__)


def create_app(settings: GitHubWebhookSettings, factory: Callable[[], CodexConversation]) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/github/webhook", status_code=status.HTTP_202_ACCEPTED)
    async def github_webhook(request: Request) -> dict[str, str]:
        try:
            event = parse_verified_event(request.headers, await request.body(), settings.secret, settings.allowed_repositories)
        except WebhookRejectedError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        asyncio.create_task(_trigger(event, factory))
        return {"status": "accepted", "delivery_id": event.delivery_id}

    return app


async def _trigger(event: GitHubWebhookEvent, factory: Callable[[], CodexConversation]) -> None:
    conversation = factory()
    prompt = (
        "A verified GitHub learning event arrived. Inspect the relevant evidence and update The Thread with a concise factual status. "
        "Create or update at most one reviewable assignment in the dedicated learning workspace when there is a clear bounded next step. "
        "Do not alter career documents or push directly to default branches.\n\n"
        f"Repository: {event.repository}\nEvent: {event.event_name}\nAction: {event.action or 'none'}\nDelivery ID: {event.delivery_id}"
    )
    try:
        async for _ in conversation.stream_reply(prompt):
            pass
    except Exception:
        LOGGER.exception("Codex webhook turn failed for %s", event.delivery_id)
    finally:
        await conversation.close()


def main() -> None:
    settings = Settings()
    webhook = settings.github_webhook_settings
    uvicorn.run(
        create_app(webhook, lambda: CodexConversation(settings.vault, settings.codex_turn_settings)),
        host=webhook.host,
        port=webhook.port,
    )
