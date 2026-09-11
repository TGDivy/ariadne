"""Conservative Waitrose recipe using fresh browser semantic references."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, cast
from urllib.parse import quote_plus, urljoin, urlsplit

from ..browser.client import BrowserClient, BrowserRemoteError
from .adapter import CheckoutResult, CheckoutTarget
from .errors import GroceryAdapterError, GroceryTakeoverRequired
from .matching import normalized
from .models import (
    BasketItem,
    BrowserAttachment,
    FulfilmentMode,
    FulfilmentSlot,
    GroceryDraft,
    OrderConfirmation,
    OrderSnapshot,
    ProductCandidate,
    RequestedItem,
)
from .store import GroceryStore

_MONEY = re.compile(r"£\s*(\d+(?:\.\d{1,2})?)")
_SIZE = re.compile(
    r"\b(\d+(?:\.\d+)?\s*(?:kg|g|ml|cl|l|pack|pk|each))\b", re.IGNORECASE
)
_CHALLENGE = re.compile(
    r"\b(captcha|security check|verify you are human|unusual traffic)\b",
    re.IGNORECASE,
)
_ORDER_NUMBER = re.compile(
    r"(?:order\s*(?:number|no\.?|reference)|reference)\s*[:#]?\s*"
    r"([A-Z0-9][A-Z0-9-]{3,40})",
    re.IGNORECASE,
)
_REJECTION = re.compile(
    r"\b(payment (?:was )?(?:declined|failed)|order (?:was )?not placed|"
    r"unable to place (?:your )?order)\b",
    re.IGNORECASE,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _money(value: str) -> Decimal:
    try:
        return Decimal(value).quantize(Decimal("0.01"))
    except InvalidOperation as error:
        raise GroceryAdapterError("Waitrose returned an unreadable price.") from error


def _product_id(name: str, size: str | None) -> str:
    material = f"{normalized(name)}|{normalized(size or '')}".encode()
    return "wr." + hashlib.sha256(material).hexdigest()[:20]


def _slot_id(label: str) -> str:
    return "slot." + hashlib.sha256(label.encode()).hexdigest()[:20]


def _elements(observation: dict[str, Any]) -> tuple[dict[str, object], ...]:
    raw = observation.get("elements")
    if not isinstance(raw, list):
        raise GroceryAdapterError("The retailer page had no semantic controls.")
    values: list[dict[str, object]] = []
    for item in raw:
        if isinstance(item, dict):
            values.append(cast(dict[str, object], item))
    return tuple(values)


def _observation(result: dict[str, Any]) -> dict[str, Any]:
    nested = result.get("observation")
    if isinstance(nested, dict):
        return cast(dict[str, Any], nested)
    return result


class BrowserSession:
    """Reattach one durable grocery task to its persistent browser lease."""

    def __init__(
        self,
        store: GroceryStore,
        client: BrowserClient,
        *,
        profile: str,
    ) -> None:
        self.store = store
        self.client = client
        self.profile = profile

    async def attach(self, draft: GroceryDraft) -> tuple[GroceryDraft, dict[str, Any]]:
        """Reattach and refuse to act while the page needs private attention."""
        draft, observation = await self.reattach(draft)
        self._require_safe(observation)
        return draft, observation

    async def reattach(
        self, draft: GroceryDraft
    ) -> tuple[GroceryDraft, dict[str, Any]]:
        """Reattach without the safety gate, for takeover and release only.

        Asking for human takeover, resuming after it, and releasing the profile
        all have to work on exactly the page that needs attention.
        """
        token = draft.browser.lease_token if draft.browser is not None else None
        try:
            result = await self.client.call(
                "session.start",
                profile=self.profile,
                task_id=draft.task_id,
                lease_token=token,
            )
        except BrowserRemoteError as error:
            raise GroceryAdapterError(error.message) from error
        lease = result.get("lease")
        if not isinstance(lease, dict) or not isinstance(lease.get("lease_token"), str):
            raise GroceryAdapterError("Browser did not return a durable task lease.")
        attachment = BrowserAttachment(
            task_id=draft.task_id,
            lease_token=str(lease["lease_token"]),
            profile=self.profile,
        )
        if draft.browser != attachment:
            draft = self.store.attach_browser(draft.draft_id, attachment)
        return draft, _observation(result)

    @staticmethod
    def _require_safe(observation: dict[str, Any]) -> None:
        text = str(observation.get("text", ""))
        attention = observation.get("human_attention_required")
        if attention is not None or _CHALLENGE.search(text):
            raise GroceryTakeoverRequired(
                "Waitrose needs private login, MFA, CAPTCHA, payment, or consent "
                "attention before grocery work can continue."
            )

    async def navigate(
        self, draft: GroceryDraft, url: str
    ) -> tuple[GroceryDraft, dict[str, Any]]:
        draft, _ = await self.attach(draft)
        assert draft.browser is not None
        try:
            result = await self.client.call(
                "page.navigate",
                task_id=draft.task_id,
                lease_token=draft.browser.lease_token,
                url=url,
            )
        except BrowserRemoteError as error:
            raise GroceryAdapterError(error.message) from error
        observation = _observation(result)
        self._require_safe(observation)
        return draft, observation

    async def inspect(self, draft: GroceryDraft) -> tuple[GroceryDraft, dict[str, Any]]:
        draft, _ = await self.attach(draft)
        assert draft.browser is not None
        try:
            result = await self.client.call(
                "page.inspect",
                task_id=draft.task_id,
                lease_token=draft.browser.lease_token,
            )
        except BrowserRemoteError as error:
            raise GroceryAdapterError(error.message) from error
        observation = _observation(result)
        self._require_safe(observation)
        return draft, observation

    async def click(
        self,
        draft: GroceryDraft,
        ref: str,
        *,
        consequential: bool = False,
        operation_id: str | None = None,
        approval_id: str | None = None,
    ) -> tuple[GroceryDraft, dict[str, Any]]:
        draft, _ = await self.attach(draft)
        assert draft.browser is not None
        try:
            result = await self.client.call(
                "page.click",
                task_id=draft.task_id,
                lease_token=draft.browser.lease_token,
                ref=ref,
                action_kind="consequential" if consequential else "reversible",
                operation_id=operation_id,
                approval_id=approval_id,
            )
        except BrowserRemoteError:
            raise
        observation = _observation(result)
        self._require_safe(observation)
        return draft, observation

    async def confirmation(
        self,
        draft: GroceryDraft,
        target: CheckoutTarget,
        *,
        approval_id: str,
        ttl_seconds: int,
    ) -> None:
        draft, _ = await self.attach(draft)
        assert draft.browser is not None
        try:
            await self.client.call(
                "confirmation.record",
                approval_id=approval_id,
                task_id=draft.task_id,
                lease_token=draft.browser.lease_token,
                page_revision=target.page_revision,
                digest=target.state_digest,
                ttl_seconds=ttl_seconds,
            )
        except BrowserRemoteError as error:
            raise GroceryAdapterError(error.message) from error

    async def takeover(self, draft: GroceryDraft, reason: str) -> str | None:
        draft, _ = await self.reattach(draft)
        assert draft.browser is not None
        try:
            result = await self.client.call(
                "takeover.request",
                task_id=draft.task_id,
                lease_token=draft.browser.lease_token,
                reason=reason[:500],
            )
        except BrowserRemoteError as error:
            raise GroceryAdapterError(error.message) from error
        # `takeover.request` answers at the top level; an observation that
        # tripped a sensitive field nests the same value instead.
        direct = result.get("takeover_url")
        if isinstance(direct, str) and direct:
            return direct
        attention = result.get("human_attention_required")
        if isinstance(attention, dict):
            value = attention.get("takeover_url")
            return str(value) if value else None
        return None

    async def resume(self, draft: GroceryDraft) -> None:
        draft, _ = await self.reattach(draft)
        assert draft.browser is not None
        try:
            await self.client.call(
                "takeover.resume",
                task_id=draft.task_id,
                lease_token=draft.browser.lease_token,
            )
        except BrowserRemoteError as error:
            raise GroceryAdapterError(error.message) from error

    async def release(self, draft: GroceryDraft) -> None:
        if draft.browser is None:
            return
        try:
            await self.client.call(
                "session.release",
                task_id=draft.task_id,
                lease_token=draft.browser.lease_token,
            )
        except BrowserRemoteError as error:
            if error.code not in {"lease_invalid", "invalid_browser_state"}:
                raise GroceryAdapterError(error.message) from error


class WaitroseAdapter:
    """Waitrose-first semantic recipe; ambiguous pages stop without improvising."""

    def __init__(
        self,
        store: GroceryStore,
        browser: BrowserClient,
        *,
        profile: str,
        base_url: str = "https://www.waitrose.com",
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Waitrose base URL must be absolute HTTP(S).")
        if parsed.scheme != "https" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("Waitrose base URL must use HTTPS except for fixtures.")
        self.store = store
        self.session = BrowserSession(store, browser, profile=profile)
        self.base_url = base_url.rstrip("/") + "/"
        self._clock = clock

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))

    async def search(
        self, draft: GroceryDraft, request: RequestedItem
    ) -> tuple[ProductCandidate, ...]:
        url = self._url(f"ecom/shop/search?searchTerm={quote_plus(request.query)}")
        _, observation = await self.session.navigate(draft, url)
        candidates, _ = self._parse_candidates(observation, request.quantity)
        if not candidates:
            raise GroceryAdapterError(
                "Waitrose search did not expose unambiguous products with prices. "
                "The retailer recipe may need review."
            )
        return candidates

    async def add_product(
        self,
        draft: GroceryDraft,
        request: RequestedItem,
        candidate: ProductCandidate,
    ) -> BasketItem:
        url = self._url(f"ecom/shop/search?searchTerm={quote_plus(request.query)}")
        draft, observation = await self.session.navigate(draft, url)
        _, refs = self._parse_candidates(observation, request.quantity)
        ref = refs.get(candidate.product_id)
        if ref is None:
            raise GroceryAdapterError(
                "The selected Waitrose product changed or disappeared; search again."
            )
        for index in range(request.quantity):
            draft, observation = await self.session.click(draft, ref)
            if index + 1 == request.quantity:
                break
            ref = self._quantity_control(observation, candidate.name, increase=True)
        return BasketItem(
            requested_item_id=request.item_id,
            product_id=candidate.product_id,
            name=candidate.name,
            size=candidate.size,
            quantity=request.quantity,
            unit_price=candidate.unit_price,
            line_total=(candidate.unit_price * request.quantity).quantize(
                Decimal("0.01")
            ),
            variable_weight=candidate.variable_weight,
            evidence=candidate.evidence,
        )

    def _parse_candidates(
        self, observation: dict[str, Any], quantity: int
    ) -> tuple[tuple[ProductCandidate, ...], dict[str, str]]:
        body = str(observation.get("text", ""))
        candidates: dict[str, ProductCandidate] = {}
        refs: dict[str, str] = {}
        for element in _elements(observation):
            name = str(element.get("name", "")).strip()
            role = str(element.get("role", ""))
            if role != "button" or not re.match(r"^add\b", name, re.IGNORECASE):
                continue
            parsed = self._candidate(name, body, quantity)
            if parsed is None:
                continue
            reference = element.get("ref")
            if not isinstance(reference, str):
                continue
            if parsed.product_id in refs and refs[parsed.product_id] != reference:
                raise GroceryAdapterError(
                    "Waitrose exposed duplicate product controls ambiguously."
                )
            candidates[parsed.product_id] = parsed
            refs[parsed.product_id] = reference
        return tuple(candidates.values()), refs

    @staticmethod
    def _candidate(label: str, body: str, quantity: int) -> ProductCandidate | None:
        content = re.sub(
            r"^add(?:\s+to\s+(?:trolley|basket))?\s+", "", label, flags=re.I
        )
        content = re.sub(r"\s+to\s+(?:trolley|basket)\s*$", "", content, flags=re.I)
        parts = tuple(part.strip() for part in content.split("|") if part.strip())
        if not parts:
            return None
        name = parts[0]
        prices = _MONEY.findall(label)
        context = ""
        if not prices:
            index = body.casefold().find(name.casefold())
            context = body[index : index + 400] if index >= 0 else ""
            prices = _MONEY.findall(context)
        if not prices:
            return None
        unit_price = _money(prices[0])
        size_match = _SIZE.search(label) or _SIZE.search(context)
        size = size_match.group(1) if size_match else None
        folded = label.casefold() + " " + context.casefold()
        favourite = "favourite" in folded or "favorite" in folded
        previous = re.search(r"(?:bought|ordered)\s+(\d+)", folded)
        prior_count = int(previous.group(1)) if previous else 0
        evidence = []
        if favourite:
            evidence.append("Waitrose favourite")
        if prior_count:
            evidence.append(f"Seen in {prior_count} prior order(s)")
        variable = any(
            marker in folded
            for marker in ("variable weight", "estimated weight", "/kg")
        )
        product_id = _product_id(name, size)
        return ProductCandidate(
            product_id=product_id,
            name=name,
            size=size,
            unit_price=unit_price,
            estimated_line_total=(unit_price * quantity).quantize(Decimal("0.01")),
            variable_weight=variable,
            favourite=favourite,
            prior_order_count=prior_count,
            evidence=tuple(evidence),
        )

    @staticmethod
    def _quantity_control(
        observation: dict[str, Any], product_name: str, *, increase: bool
    ) -> str:
        verb = "increase" if increase else "decrease"
        matches = [
            str(element["ref"])
            for element in _elements(observation)
            if str(element.get("role", "")) == "button"
            and verb in str(element.get("name", "")).casefold()
            and normalized(product_name) in normalized(str(element.get("name", "")))
            and isinstance(element.get("ref"), str)
        ]
        if len(matches) != 1:
            raise GroceryAdapterError(
                "Waitrose quantity control was missing or ambiguous after adding."
            )
        return matches[0]

    async def list_slots(
        self, draft: GroceryDraft, mode: FulfilmentMode
    ) -> tuple[FulfilmentSlot, ...]:
        draft, observation = await self.session.navigate(
            draft, self._url("ecom/checkout/fulfilment")
        )
        mode_refs = [
            str(element["ref"])
            for element in _elements(observation)
            if str(element.get("role", "")) in {"button", "tab"}
            and mode.value in str(element.get("name", "")).casefold()
            and isinstance(element.get("ref"), str)
        ]
        if len(mode_refs) == 1:
            _, observation = await self.session.click(draft, mode_refs[0])
        slots, _ = self._parse_slots(observation, mode)
        if not slots:
            raise GroceryAdapterError(
                "Waitrose did not expose bounded fulfilment slots in a recognised "
                "format; review the retailer recipe."
            )
        return slots

    async def choose_slot(self, draft: GroceryDraft, slot: FulfilmentSlot) -> None:
        current = await self.list_slots(draft, slot.mode)
        if slot.slot_id not in {value.slot_id for value in current}:
            raise GroceryAdapterError("The selected fulfilment slot disappeared.")
        draft, observation = await self.session.inspect(draft)
        _, refs = self._parse_slots(observation, slot.mode)
        ref = refs.get(slot.slot_id)
        if ref is None:
            raise GroceryAdapterError(
                "The selected fulfilment slot is no longer fresh."
            )
        await self.session.click(draft, ref)

    def _parse_slots(
        self,
        observation: dict[str, Any],
        mode: FulfilmentMode,
    ) -> tuple[tuple[FulfilmentSlot, ...], dict[str, str]]:
        slots: list[FulfilmentSlot] = []
        refs: dict[str, str] = {}
        prefix = f"choose {mode.value} slot"
        for element in _elements(observation):
            label = str(element.get("name", "")).strip()
            if prefix not in label.casefold():
                continue
            parts = tuple(part.strip() for part in label.split("|") if part.strip())
            if len(parts) < 5:
                continue
            try:
                starts = datetime.fromisoformat(parts[1].replace("Z", "+00:00"))
                ends = datetime.fromisoformat(parts[2].replace("Z", "+00:00"))
            except ValueError:
                continue
            price = _MONEY.search(parts[3])
            reference = element.get("ref")
            if price is None or not isinstance(reference, str):
                continue
            identifier = _slot_id(label)
            slot = FulfilmentSlot(
                slot_id=identifier,
                mode=mode,
                starts_at=starts,
                ends_at=ends,
                fee=_money(price.group(1)),
                location=parts[4],
                convenient=(
                    None
                    if len(parts) < 6
                    else parts[5].casefold() in {"convenient", "preferred"}
                ),
            )
            slots.append(slot)
            refs[identifier] = reference
        return tuple(slots), refs

    async def read_basket(self, draft: GroceryDraft) -> OrderSnapshot:
        slot = draft.selected_slot
        if slot is None or draft.mode is None:
            raise GroceryAdapterError(
                "Choose delivery or collection and a live slot before basket review."
            )
        draft, observation = await self.session.navigate(
            draft, self._url("ecom/shop/trolley")
        )
        body = str(observation.get("text", ""))
        live_items = tuple(self._read_item(body, item) for item in draft.basket)
        displayed_count = re.search(r"basket\s+has\s+(\d+)\s+items?", body, re.I)
        if displayed_count is not None and int(displayed_count.group(1)) != len(
            live_items
        ):
            raise GroceryAdapterError(
                "The live Waitrose basket contains an unexpected number of products."
            )
        subtotal = self._labelled_money(body, "subtotal")
        total = self._labelled_money(body, "total", last=True)
        fees = self._optional_labelled_money(
            body, ("delivery fee", "collection fee", "fees")
        )
        minimum = self._optional_labelled_money(body, ("minimum spend",))
        calculated = sum((item.line_total for item in live_items), Decimal("0.00"))
        if abs(calculated - subtotal) > Decimal("0.02"):
            raise GroceryAdapterError(
                "Waitrose basket lines do not reconcile with its displayed subtotal; "
                "an item may have changed or been added outside this draft."
            )
        if abs(subtotal + fees - total) > Decimal("0.02"):
            raise GroceryAdapterError(
                "Waitrose's displayed subtotal, fees, and total do not reconcile."
            )
        return OrderSnapshot(
            items=live_items,
            substitutions=draft.substitutions,
            omitted=draft.omitted,
            mode=draft.mode,
            slot=slot,
            location=slot.location,
            subtotal=subtotal,
            fees=fees,
            total=total,
            minimum_spend=minimum,
            captured_at=self._clock(),
        )

    @staticmethod
    def _read_item(body: str, expected: BasketItem) -> BasketItem:
        escaped = re.escape(expected.name)
        structured = re.search(
            rf"Product:\s*{escaped}\s*\|\s*Quantity:\s*(\d+)\s*\|\s*"
            rf"Unit:\s*£\s*(\d+(?:\.\d{{1,2}})?)\s*\|\s*"
            rf"Line:\s*£\s*(\d+(?:\.\d{{1,2}})?)"
            rf"(?:\s*\|\s*Variable:\s*(yes|no))?",
            body,
            re.IGNORECASE,
        )
        if structured is None:
            raise GroceryAdapterError(
                f"Could not re-read {expected.name!r} and its exact live "
                "quantity and price."
            )
        quantity = int(structured.group(1))
        unit = _money(structured.group(2))
        line = _money(structured.group(3))
        variable = (
            expected.variable_weight
            if structured.group(4) is None
            else structured.group(4).casefold() == "yes"
        )
        return expected.model_copy(
            update={
                "quantity": quantity,
                "unit_price": unit,
                "line_total": line,
                "variable_weight": variable,
            }
        )

    @staticmethod
    def _labelled_money(body: str, label: str, *, last: bool = False) -> Decimal:
        matches = re.findall(
            rf"{re.escape(label)}\s*:?\s*£\s*(\d+(?:\.\d{{1,2}})?)",
            body,
            re.IGNORECASE,
        )
        if not matches:
            raise GroceryAdapterError(f"Waitrose did not show a readable {label}.")
        return _money(matches[-1] if last else matches[0])

    @staticmethod
    def _optional_labelled_money(body: str, labels: tuple[str, ...]) -> Decimal:
        for label in labels:
            matches = re.findall(
                rf"{re.escape(label)}\s*:?\s*£\s*(\d+(?:\.\d{{1,2}})?)",
                body,
                re.IGNORECASE,
            )
            if matches:
                return _money(matches[-1])
        return Decimal("0.00")

    async def prepare_checkout(self, draft: GroceryDraft) -> CheckoutTarget:
        _, observation = await self.session.navigate(
            draft, self._url("ecom/checkout/review")
        )
        matches = [
            str(element["ref"])
            for element in _elements(observation)
            if str(element.get("role", "")) == "button"
            and re.search(
                r"\b(place order|confirm and pay|complete order)\b",
                str(element.get("name", "")),
                re.IGNORECASE,
            )
            and isinstance(element.get("ref"), str)
            and bool(element.get("enabled", False))
            and not bool(element.get("sensitive", False))
        ]
        if len(matches) != 1:
            raise GroceryAdapterError(
                "Waitrose's final checkout control was missing or ambiguous. Stop "
                "before submission and review the retailer recipe."
            )
        revision = observation.get("page_revision")
        digest = observation.get("state_digest")
        if not isinstance(revision, int) or not isinstance(digest, str):
            raise GroceryAdapterError("Browser checkout state could not be bound.")
        return CheckoutTarget(matches[0], revision, digest)

    async def submit_checkout(
        self,
        draft: GroceryDraft,
        target: CheckoutTarget,
        *,
        operation_id: str,
        approval_id: str,
    ) -> CheckoutResult:
        try:
            _, observation = await self.session.click(
                draft,
                target.ref,
                consequential=True,
                operation_id=operation_id,
                approval_id=approval_id,
            )
        except BrowserRemoteError:
            raise
        body = str(observation.get("text", ""))
        order = _ORDER_NUMBER.search(body)
        if order is not None and re.search(
            r"\b(order confirmed|thanks for your order|order has been placed)\b",
            body,
            re.IGNORECASE,
        ):
            confirmation = OrderConfirmation(
                order_number=order.group(1),
                confirmed_at=self._clock(),
                evidence="Waitrose confirmation page showed a confirmed order number.",
            )
            return CheckoutResult("confirmed", confirmation=confirmation)
        rejected = _REJECTION.search(body)
        if rejected is not None:
            return CheckoutResult("rejected", reason=rejected.group(1))
        return CheckoutResult(
            "uncertain",
            reason=(
                "Waitrose returned no definitive order number or definitive rejection "
                "after submission."
            ),
        )

    async def request_takeover(self, draft: GroceryDraft, reason: str) -> str | None:
        return await self.session.takeover(draft, reason)

    async def resume(self, draft: GroceryDraft) -> None:
        await self.session.resume(draft)

    async def release(self, draft: GroceryDraft) -> None:
        await self.session.release(draft)


__all__ = ["BrowserSession", "WaitroseAdapter"]
