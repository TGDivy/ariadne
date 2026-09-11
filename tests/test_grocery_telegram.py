from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from grocery_fixtures import (
    NOW,
    basket_item,
    requested,
    slot,
    snapshot,
    substitution,
)

from ariadne.grocery.models import (
    ApprovalStatus,
    FulfilmentMode,
    GroceryDraft,
    GroceryState,
    OmittedItem,
)
from ariadne.grocery.service import GroceryService
from ariadne.grocery.store import GroceryStore
from ariadne.grocery.telegram import (
    CALLBACK_PREFIX,
    SETTLED_LABELS,
    GroceryApprovalController,
    parse_approval_callback,
    review_markdown,
)

OWNER = 7


class FakeQuery:
    def __init__(
        self, data: str, *, user_id: int = OWNER, message_id: int = 41
    ) -> None:
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(
            message_id=message_id, chat=SimpleNamespace(id=OWNER)
        )
        self.answers: list[str] = []

    async def answer(self, text: str | None = None, **_: object) -> bool:
        self.answers.append(str(text))
        return True


class FakeRichBot:
    """Enough of the Bot API to observe what the review card would send."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    async def do_api_request(
        self,
        method: str,
        api_kwargs: dict[str, Any],
        return_type: object | None = None,
    ) -> Any:
        del return_type
        self.requests.append({"method": method, **api_kwargs})
        return SimpleNamespace(message_id=41, date=NOW)


class RecordingAdapter:
    def __init__(self) -> None:
        self.released = 0

    async def release(self, draft: GroceryDraft) -> None:
        self.released += 1


def service_with_review(tmp_path: Path, **kwargs: object) -> GroceryService:
    store = GroceryStore(tmp_path / "grocery.sqlite3")
    store.initialize()
    service = GroceryService(store, cast(Any, RecordingAdapter()))
    draft = store.create_draft((requested(),), mode=FulfilmentMode.DELIVERY)
    draft = store.save(
        draft.model_copy(update={"state": GroceryState.BUILD}),
        expected_revision=draft.revision,
        reason="build",
    )
    draft = store.save(
        draft.model_copy(
            update={
                "state": GroceryState.RESOLVE,
                "basket": (basket_item(),),
                "selected_slot": slot(),
            }
        ),
        expected_revision=draft.revision,
        reason="resolve",
    )
    store.save(
        draft.model_copy(
            update={"state": GroceryState.REVIEW, "snapshot": snapshot(**kwargs)}  # type: ignore[arg-type]
        ),
        expected_revision=draft.revision,
        reason="review",
    )
    return service


def only_draft(service: GroceryService) -> str:
    return service.store.list(limit=1)[0].draft_id


def test_callback_data_is_parsed_conservatively() -> None:
    assert parse_approval_callback(f"{CALLBACK_PREFIX}abc:approve") == (
        "abc",
        "approve",
    )
    assert parse_approval_callback(f"{CALLBACK_PREFIX}abc:cancel") == ("abc", "cancel")
    assert parse_approval_callback(f"{CALLBACK_PREFIX}abc:submit") is None
    assert parse_approval_callback(f"{CALLBACK_PREFIX}:approve") is None
    assert parse_approval_callback("settings:web:live") is None
    assert parse_approval_callback("grocery:abc") is None


def test_the_review_shows_every_material_detail_of_the_exact_order() -> None:
    text = review_markdown(
        snapshot(
            (basket_item(quantity=2, unit_price="1.50"),),
            fees="3.50",
            substitutions=(substitution(),),
            omitted=(
                OmittedItem(
                    requested_item_id="milk",
                    requested="semi skimmed milk",
                    reason="Out of stock",
                ),
            ),
        )
    )

    assert "**Waitrose · Delivery**" in text
    assert "12 Example Street" in text
    assert "2 × Porridge Oats (1kg) — £3.00" in text
    assert "**Substitutions**" in text
    assert "porridge oats → Jumbo Oats (iris)" in text
    assert "**Unavailable / omitted**" in text
    assert "semi skimmed milk: Out of stock" in text
    assert "Subtotal £3.00 · fees £3.50" in text
    assert "**Estimated total £6.50**" in text
    assert "weighted-item tolerance" in text


def test_a_collection_review_names_its_own_logistics() -> None:
    text = review_markdown(
        snapshot(
            mode=FulfilmentMode.COLLECTION,
            fulfilment=slot(
                "slot.collect", mode=FulfilmentMode.COLLECTION, location="Waitrose Shop"
            ),
        )
    )

    assert "**Waitrose · Collection**" in text
    assert "Waitrose Shop" in text


async def test_the_owner_approving_moves_the_draft_and_settles_the_card(
    tmp_path: Path,
) -> None:
    service = service_with_review(tmp_path)
    draft_id = only_draft(service)
    approval = service.store.create_approval(
        draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
    )
    service.store.attach_approval_message(approval.approval_id, 41)
    controller = GroceryApprovalController(service, OWNER)
    bot = FakeRichBot()
    query = FakeQuery(f"{CALLBACK_PREFIX}{approval.approval_id}:approve")

    await controller.callback(
        cast(Any, SimpleNamespace(callback_query=query)),
        cast(Any, SimpleNamespace(bot=bot)),
    )

    assert query.answers == ["Approved."]
    assert service.store.get(draft_id).state == GroceryState.APPROVED
    settled = service.store.approval(approval.approval_id)
    assert settled is not None and settled.status == ApprovalStatus.APPROVED
    assert bot.requests and bot.requests[0]["method"] == "editMessageText"


async def test_a_non_owner_cannot_approve_a_grocery_order(tmp_path: Path) -> None:
    service = service_with_review(tmp_path)
    draft_id = only_draft(service)
    approval = service.store.create_approval(
        draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
    )
    service.store.attach_approval_message(approval.approval_id, 41)
    controller = GroceryApprovalController(service, OWNER)
    query = FakeQuery(
        f"{CALLBACK_PREFIX}{approval.approval_id}:approve", user_id=OWNER + 1
    )

    await controller.callback(
        cast(Any, SimpleNamespace(callback_query=query)),
        cast(Any, SimpleNamespace(bot=FakeRichBot())),
    )

    assert query.answers == ["Only the configured owner can approve checkout."]
    assert service.store.get(draft_id).state == GroceryState.REVIEW


async def test_a_stale_card_is_reported_without_changing_anything(
    tmp_path: Path,
) -> None:
    service = service_with_review(tmp_path)
    draft_id = only_draft(service)
    approval = service.store.create_approval(
        draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
    )
    service.store.attach_approval_message(approval.approval_id, 41)
    controller = GroceryApprovalController(service, OWNER)
    query = FakeQuery(f"{CALLBACK_PREFIX}{approval.approval_id}:approve", message_id=99)

    await controller.callback(
        cast(Any, SimpleNamespace(callback_query=query)),
        cast(Any, SimpleNamespace(bot=FakeRichBot())),
    )

    assert query.answers == ["That grocery approval is stale."]
    assert service.store.get(draft_id).state == GroceryState.REVIEW


async def test_a_double_tap_records_one_decision(tmp_path: Path) -> None:
    service = service_with_review(tmp_path)
    draft_id = only_draft(service)
    approval = service.store.create_approval(
        draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
    )
    service.store.attach_approval_message(approval.approval_id, 41)
    controller = GroceryApprovalController(service, OWNER)
    context = cast(Any, SimpleNamespace(bot=FakeRichBot()))

    first = FakeQuery(f"{CALLBACK_PREFIX}{approval.approval_id}:approve")
    second = FakeQuery(f"{CALLBACK_PREFIX}{approval.approval_id}:cancel")
    await controller.callback(cast(Any, SimpleNamespace(callback_query=first)), context)
    await controller.callback(
        cast(Any, SimpleNamespace(callback_query=second)), context
    )

    assert first.answers == ["Approved."]
    assert second.answers == ["That choice was already recorded."]
    assert service.store.get(draft_id).state == GroceryState.APPROVED


async def test_cancelling_releases_the_browser_profile(tmp_path: Path) -> None:
    service = service_with_review(tmp_path)
    draft_id = only_draft(service)
    approval = service.store.create_approval(
        draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
    )
    service.store.attach_approval_message(approval.approval_id, 41)
    controller = GroceryApprovalController(service, OWNER)
    query = FakeQuery(f"{CALLBACK_PREFIX}{approval.approval_id}:cancel")

    await controller.callback(
        cast(Any, SimpleNamespace(callback_query=query)),
        cast(Any, SimpleNamespace(bot=FakeRichBot())),
    )

    assert query.answers == ["Cancelled."]
    assert service.store.get(draft_id).state == GroceryState.CANCELLED
    assert cast(RecordingAdapter, service.adapter).released == 1


async def test_a_changed_basket_makes_the_card_inert(tmp_path: Path) -> None:
    service = service_with_review(tmp_path)
    draft_id = only_draft(service)
    approval = service.store.create_approval(
        draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
    )
    service.store.attach_approval_message(approval.approval_id, 41)
    draft = service.store.get(draft_id)
    service.store.save(
        draft.model_copy(update={"snapshot": snapshot((basket_item(quantity=3),))}),
        expected_revision=draft.revision,
        reason="Owner asked for more",
    )
    controller = GroceryApprovalController(service, OWNER)
    query = FakeQuery(f"{CALLBACK_PREFIX}{approval.approval_id}:approve")

    await controller.callback(
        cast(Any, SimpleNamespace(callback_query=query)),
        cast(Any, SimpleNamespace(bot=FakeRichBot())),
    )

    assert query.answers == ["The basket changed or this approval expired."]
    assert service.store.get(draft_id).state == GroceryState.REVIEW


async def test_unparseable_callback_data_is_answered_and_ignored(
    tmp_path: Path,
) -> None:
    service = service_with_review(tmp_path)
    controller = GroceryApprovalController(service, OWNER)
    query = FakeQuery("grocery:nonsense")

    await controller.callback(
        cast(Any, SimpleNamespace(callback_query=query)),
        cast(Any, SimpleNamespace(bot=FakeRichBot())),
    )

    assert query.answers == ["That grocery approval is no longer available."]


def test_every_settled_state_has_a_label() -> None:
    assert set(SETTLED_LABELS) == set(ApprovalStatus) - {ApprovalStatus.PENDING}
