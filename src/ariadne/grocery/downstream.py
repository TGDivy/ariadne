"""Repairable Calendar, knowledge, and conversational completion writes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from ..calendar import ICloudCalendar
from ..knowledge.models import KnowledgeValidationError
from ..knowledge.store import KnowledgeStore
from .models import DownstreamRecord, DownstreamStatus, GroceryDraft, OrderConfirmation
from .store import GroceryStore


class CalendarOrderWriter(Protocol):
    def write(self, draft: GroceryDraft, confirmation: OrderConfirmation) -> str: ...


class KnowledgeOrderWriter(Protocol):
    def write(self, draft: GroceryDraft, confirmation: OrderConfirmation) -> str: ...


class ICloudOrderCalendar:
    """Create the confirmed delivery or collection window once."""

    def __init__(self, factory: Callable[[], ICloudCalendar]) -> None:
        self._factory = factory

    def write(self, draft: GroceryDraft, confirmation: OrderConfirmation) -> str:
        snapshot = draft.snapshot
        if snapshot is None:
            raise ValueError("Confirmed grocery order has no fulfilment snapshot.")
        mode = snapshot.mode.value.title()
        description = (
            f"Confirmed Waitrose order {confirmation.order_number}. "
            f"Estimated total £{snapshot.total}."
        )
        with self._factory() as calendar:
            result = calendar.create_event(
                title=f"Waitrose {snapshot.mode.value}",
                start=snapshot.slot.starts_at.isoformat(),
                end=snapshot.slot.ends_at.isoformat(),
                description=description,
                location=snapshot.location,
                status="confirmed",
                busy=True,
                alarms_minutes_before=[60] if mode == "Collection" else None,
            )
        reference = result.get("id") or result.get("event_id")
        return str(reference or f"Waitrose {snapshot.mode.value} calendar event")


class KnowledgeOrderLog:
    """Keep confirmed grocery evidence in an ordinary editable knowledge record."""

    def __init__(
        self,
        store: KnowledgeStore,
        *,
        record_id: str = "grocery-orders",
        folder: str = "life",
    ) -> None:
        self.store = store
        self.record_id = record_id
        self.folder = folder

    def write(self, draft: GroceryDraft, confirmation: OrderConfirmation) -> str:
        snapshot = draft.snapshot
        if snapshot is None:
            raise ValueError("Confirmed grocery order has no basket snapshot.")
        item_text = ", ".join(
            f"{item.quantity} × {item.name}" for item in snapshot.items
        )
        line = (
            f"- {confirmation.confirmed_at.date().isoformat()} — Waitrose "
            f"{snapshot.mode.value}, order `{confirmation.order_number}`; "
            f"{item_text}; £{snapshot.total}."
        )
        try:
            current = self.store.read((self.record_id,))[0]
        except KnowledgeValidationError:
            created = self.store.create(
                title="Grocery orders",
                summary=(
                    "Confirmed grocery choices and fulfilment evidence; prior orders "
                    "are context rather than standing permission to buy again."
                ),
                body=f"## Confirmed orders\n\n{line}",
                folder=self.folder,
            )
            return created.metadata.id
        body = current.body.rstrip()
        if confirmation.order_number in body:
            return current.metadata.id
        updated = self.store.update(
            current.metadata.id,
            summary=(
                "Confirmed grocery choices and fulfilment evidence; prior orders are "
                "context rather than standing permission to buy again."
            ),
            body=f"{body}\n{line}" if body else f"## Confirmed orders\n\n{line}",
        )
        return updated.metadata.id


class OrderDownstream:
    """Attempt each independent write and retain failures for explicit repair."""

    def __init__(
        self,
        store: GroceryStore,
        *,
        calendar: CalendarOrderWriter | None = None,
        knowledge: KnowledgeOrderWriter | None = None,
    ) -> None:
        self.store = store
        self.calendar = calendar
        self.knowledge = knowledge

    async def repair(self, draft_id: str) -> DownstreamRecord:
        draft = self.store.get(draft_id)
        confirmation = self.store.confirmation(draft_id)
        state = self.store.downstream(draft_id)
        if confirmation is None or state is None:
            raise ValueError(
                "Only a definitively confirmed order has repairable writes."
            )

        if state.calendar_status != DownstreamStatus.SUCCEEDED:
            if self.calendar is None:
                state = self.store.set_downstream(
                    draft_id,
                    "calendar",
                    DownstreamStatus.SKIPPED,
                    error="Calendar integration is disabled.",
                )
            else:
                try:
                    reference = await asyncio.to_thread(
                        self.calendar.write, draft, confirmation
                    )
                except Exception as error:
                    state = self.store.set_downstream(
                        draft_id,
                        "calendar",
                        DownstreamStatus.FAILED,
                        error=str(error),
                    )
                else:
                    state = self.store.set_downstream(
                        draft_id,
                        "calendar",
                        DownstreamStatus.SUCCEEDED,
                        reference=reference,
                    )

        if state.knowledge_status != DownstreamStatus.SUCCEEDED:
            if self.knowledge is None:
                state = self.store.set_downstream(
                    draft_id,
                    "knowledge",
                    DownstreamStatus.SKIPPED,
                    error="Knowledge integration is disabled.",
                )
            else:
                try:
                    reference = await asyncio.to_thread(
                        self.knowledge.write, draft, confirmation
                    )
                except Exception as error:
                    state = self.store.set_downstream(
                        draft_id,
                        "knowledge",
                        DownstreamStatus.FAILED,
                        error=str(error),
                    )
                else:
                    state = self.store.set_downstream(
                        draft_id,
                        "knowledge",
                        DownstreamStatus.SUCCEEDED,
                        reference=reference,
                    )

        if state.handoff_status != DownstreamStatus.SUCCEEDED:
            try:
                snapshot = draft.snapshot
                assert snapshot is not None
                reference = self.store.enqueue_handoff(
                    draft_id,
                    {
                        "kind": "grocery_order_confirmed",
                        "draft_id": draft_id,
                        "retailer": snapshot.retailer,
                        "mode": snapshot.mode.value,
                        "slot": snapshot.slot.model_dump(mode="json"),
                        "items": [
                            {
                                "name": item.name,
                                "quantity": item.quantity,
                                "substitution": any(
                                    value.requested_item_id == item.requested_item_id
                                    for value in snapshot.substitutions
                                ),
                            }
                            for item in snapshot.items
                        ],
                        "total": str(snapshot.total),
                        "order_number": confirmation.order_number,
                        "calendar_status": state.calendar_status.value,
                        "knowledge_status": state.knowledge_status.value,
                        "instruction": (
                            "Blend this into the active Telegram conversation as a "
                            "natural completion update; do not start a "
                            "contextless turn."
                        ),
                    },
                )
            except Exception as error:
                state = self.store.set_downstream(
                    draft_id,
                    "handoff",
                    DownstreamStatus.FAILED,
                    error=str(error),
                )
            else:
                state = self.store.set_downstream(
                    draft_id,
                    "handoff",
                    DownstreamStatus.SUCCEEDED,
                    reference=reference,
                )
        return state


__all__ = [
    "CalendarOrderWriter",
    "ICloudOrderCalendar",
    "KnowledgeOrderLog",
    "KnowledgeOrderWriter",
    "OrderDownstream",
]
