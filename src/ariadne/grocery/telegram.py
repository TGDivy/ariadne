"""Trusted Telegram review card and callback boundary for grocery checkout."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from contextlib import suppress

from telegram import Bot, Message, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from ..telegram.history import (
    TelegramHistoryMessage,
    TelegramMessageStore,
    telegram_message_time,
)
from ..telegram.rich import ButtonStyle, RichBotAPI, RichButton
from .errors import GroceryAdapterError, GroceryApprovalError, GroceryStateError
from .models import ApprovalStatus, GroceryApproval, OrderSnapshot
from .service import GroceryService

CALLBACK_PREFIX = "grocery:"
LOGGER = logging.getLogger(__name__)

SETTLED_LABELS: dict[ApprovalStatus, tuple[str, ButtonStyle]] = {
    ApprovalStatus.APPROVED: ("Approved", "success"),
    ApprovalStatus.USED: ("Order submitted", "success"),
    ApprovalStatus.CANCELLED: ("Cancelled", "danger"),
    ApprovalStatus.EXPIRED: ("Expired", "danger"),
    ApprovalStatus.INVALIDATED: ("Basket changed", "danger"),
}


def parse_approval_callback(value: str) -> tuple[str, str] | None:
    if not value.startswith(CALLBACK_PREFIX):
        return None
    payload = value.removeprefix(CALLBACK_PREFIX)
    approval_id, separator, decision = payload.rpartition(":")
    if not separator or not approval_id or decision not in {"approve", "cancel"}:
        return None
    return approval_id, decision


def review_markdown(snapshot: OrderSnapshot) -> str:
    """Render a compact exact basket summary without hiding material details."""
    mode = snapshot.mode.value.title()
    slot = snapshot.slot
    lines = [
        f"**Waitrose · {mode}**",
        (
            f"{slot.starts_at:%a %d %b, %H:%M}–{slot.ends_at:%H:%M} · "
            f"{snapshot.location}"
        ),
        "",
    ]
    lines.extend(
        f"- {item.quantity} × {item.name}"
        + (f" ({item.size})" if item.size else "")
        + f" — £{item.line_total}"
        for item in snapshot.items
    )
    if snapshot.substitutions:
        lines.extend(("", "**Substitutions**"))
        lines.extend(
            f"- {value.requested} → {value.replacement} ({value.source.value})"
            for value in snapshot.substitutions
        )
    if snapshot.omitted:
        lines.extend(("", "**Unavailable / omitted**"))
        lines.extend(
            f"- {value.requested}: {value.reason}" for value in snapshot.omitted
        )
    lines.extend(
        (
            "",
            f"Subtotal £{snapshot.subtotal} · fees £{snapshot.fees}",
            f"**Estimated total £{snapshot.total}**",
            "Approval covers exactly this basket, slot, location and substitutions, "
            "apart from the configured weighted-item tolerance.",
        )
    )
    return "\n".join(lines)


class GroceryApprovalCard:
    def __init__(self, bot: Bot) -> None:
        self._rich = RichBotAPI(bot)

    async def send(self, approval: GroceryApproval, snapshot: OrderSnapshot) -> Message:
        return await self._rich.send(
            chat_id=approval.chat_id,
            markdown=review_markdown(snapshot),
            buttons=(
                RichButton(
                    f"Approve £{snapshot.total}",
                    "callback_data",
                    f"{CALLBACK_PREFIX}{approval.approval_id}:approve",
                    style="success",
                ),
                RichButton(
                    "Cancel",
                    "callback_data",
                    f"{CALLBACK_PREFIX}{approval.approval_id}:cancel",
                    style="danger",
                ),
            ),
            buttons_per_row=2,
        )

    async def settle(
        self,
        approval: GroceryApproval,
        snapshot: OrderSnapshot,
    ) -> None:
        if approval.message_id is None or approval.status == ApprovalStatus.PENDING:
            return
        label, style = SETTLED_LABELS[approval.status]
        await self._rich.edit_by_id(
            chat_id=approval.chat_id,
            message_id=approval.message_id,
            markdown=review_markdown(snapshot),
            buttons=(RichButton(label, "disabled", style=style),),
        )


async def wait_for_owner_approval(
    service: GroceryService,
    draft_id: str,
    *,
    token: str,
    owner_user_id: int,
    history: TelegramMessageStore,
) -> GroceryApproval:
    draft = service.store.get(draft_id)
    if draft.state.value != "review" or draft.snapshot is None:
        draft = await service.prepare_review(draft_id)
    assert draft.snapshot is not None
    approval = service.store.live_approval(draft_id)
    if approval is not None and approval.status == ApprovalStatus.APPROVED:
        return approval
    if approval is None:
        approval = service.store.create_approval(
            draft_id,
            owner_user_id=owner_user_id,
            chat_id=owner_user_id,
            ttl_seconds=service.approval_ttl_seconds,
        )
    history.initialize()
    try:
        async with Bot(token) as bot:
            card = GroceryApprovalCard(bot)
            if approval.message_id is None:
                message = await card.send(approval, draft.snapshot)
                try:
                    history.record(
                        TelegramHistoryMessage(
                            chat_id=owner_user_id,
                            message_id=message.message_id,
                            sent_at=telegram_message_time(message),
                            speaker="iris",
                            source="telegram",
                            content_type="text",
                            text=review_markdown(draft.snapshot),
                        )
                    )
                except (OSError, sqlite3.Error, ValueError) as error:
                    raise GroceryApprovalError(
                        f"Telegram delivered grocery review message ID "
                        f"{message.message_id}, but durable history failed. Do not "
                        "send a duplicate review automatically."
                    ) from error
                approval = service.store.attach_approval_message(
                    approval.approval_id, message.message_id
                )
            while approval.status == ApprovalStatus.PENDING:
                await asyncio.sleep(0.2)
                current = service.store.approval(approval.approval_id)
                if current is None:
                    raise GroceryApprovalError("Grocery approval state disappeared.")
                approval = current
            if approval.status == ApprovalStatus.APPROVED:
                return approval
            with suppress(TelegramError):
                await card.settle(approval, draft.snapshot)
    except TelegramError as error:
        raise GroceryApprovalError(
            "Telegram could not deliver grocery review."
        ) from error
    if approval.status == ApprovalStatus.CANCELLED:
        raise GroceryApprovalError("The owner cancelled this grocery order.")
    if approval.status == ApprovalStatus.EXPIRED:
        raise GroceryApprovalError("The grocery approval expired; refresh the basket.")
    raise GroceryApprovalError("The exact grocery approval was invalidated.")


class GroceryApprovalController:
    """Apply callbacks only from the configured owner and exact review message."""

    def __init__(self, service: GroceryService, owner_user_id: int) -> None:
        self.service = service
        self.owner_user_id = owner_user_id

    async def callback(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        query = update.callback_query
        if query is None or query.data is None:
            return
        parsed = parse_approval_callback(query.data)
        message = query.message
        if parsed is None or message is None:
            await query.answer("That grocery approval is no longer available.")
            return
        approval_id, decision = parsed
        user_id = query.from_user.id
        chat_id = message.chat.id
        selection = self.service.store.decide_approval(
            approval_id,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message.message_id,
            decision="approve" if decision == "approve" else "cancel",
        )
        notices = {
            "accepted": "Approved." if decision == "approve" else "Cancelled.",
            "already_decided": "That choice was already recorded.",
            "inactive": "The basket changed or this approval expired.",
            "stale": "That grocery approval is stale.",
            "unauthorized": "Only the configured owner can approve checkout.",
        }
        await query.answer(notices[selection.outcome], show_alert=False)
        approval = selection.approval
        if approval is None:
            return
        current = self.service.store.approval(approval.approval_id) or approval
        draft = self.service.store.get(approval.draft_id)
        if draft.snapshot is not None:
            with suppress(TelegramError):
                await GroceryApprovalCard(context.bot).settle(current, draft.snapshot)
        if selection.outcome == "accepted" and decision == "cancel":
            try:
                await self.service.adapter.release(draft)
            except (GroceryAdapterError, GroceryStateError):
                # Cancellation is already durable; a stale lease expires on its
                # own and must not turn a recorded decision into an error.
                LOGGER.info(
                    "Grocery browser release after cancellation failed",
                    exc_info=True,
                )


__all__ = [
    "CALLBACK_PREFIX",
    "SETTLED_LABELS",
    "GroceryApprovalCard",
    "GroceryApprovalController",
    "parse_approval_callback",
    "review_markdown",
    "wait_for_owner_approval",
]
