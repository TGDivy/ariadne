"""Waitrose recipe scenarios against a local fixture shop and real Chromium.

Nothing here touches waitrose.com. The fixture serves the same *shapes* the
adapter reads — semantic button labels, a trolley summary, and a checkout
outcome — so selector-level drift on the real site stays a manual check.
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
from grocery_fixtures import NOW, requested

from ariadne.browser.client import BrowserClient
from ariadne.browser.daemon import BrowserDaemon
from ariadne.config import BrowserConfig
from ariadne.grocery.errors import (
    GroceryAdapterError,
    GroceryApprovalError,
    GroceryStateError,
    GroceryTakeoverRequired,
)
from ariadne.grocery.models import FulfilmentMode, GroceryState, OrderConfirmation
from ariadne.grocery.service import GroceryService
from ariadne.grocery.store import GroceryStore
from ariadne.grocery.waitrose import WaitroseAdapter

REQUIRE_BROWSER_ENVIRONMENT = "ARIADNE_REQUIRE_BROWSER_TESTS"
OWNER = 7


async def _launch_chromium_once() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        await browser.close()


@pytest.fixture(scope="module", autouse=True)
def chromium_build() -> None:
    """Skip these scenarios when Chromium is absent, as the browser tests do."""
    try:
        asyncio.run(_launch_chromium_once())
    except Exception as error:
        if os.environ.get(REQUIRE_BROWSER_ENVIRONMENT, "") in {"1", "true"}:
            raise
        pytest.skip(
            "Playwright's Chromium build is unavailable "
            f"({type(error).__name__}). Run "
            "`uv run playwright install --with-deps chromium`, or set "
            f"{REQUIRE_BROWSER_ENVIRONMENT}=1 to make its absence a failure.",
            allow_module_level=True,
        )


@dataclass
class Product:
    key: str
    name: str
    size: str
    price: Decimal
    favourite: bool = False
    ordered: int = 0
    variable: bool = False
    available: bool = True


@dataclass
class Shop:
    """Mutable synthetic shop state shared by one test and its HTTP handler."""

    products: dict[str, Product] = field(default_factory=dict)
    basket: dict[str, int] = field(default_factory=dict)
    fee: Decimal = Decimal("0.00")
    minimum_spend: Decimal | None = None
    challenge: bool = False
    login_expired: bool = False
    outcome: str = "confirmed"
    submissions: int = 0

    def reset(self) -> None:
        self.products = {
            "oats": Product("oats", "Porridge Oats", "1kg", Decimal("1.50")),
            "duchy": Product(
                "duchy",
                "Duchy Organic Porridge Oats",
                "1kg",
                Decimal("2.80"),
                favourite=True,
                ordered=3,
            ),
            "bananas": Product(
                "bananas", "Bananas Loose", "1kg", Decimal("1.10"), variable=True
            ),
            "sourdough": Product(
                "sourdough", "Sourdough Loaf", "400g", Decimal("2.20"), available=False
            ),
        }
        self.basket = {}
        self.fee = Decimal("0.00")
        self.minimum_spend = None
        self.challenge = False
        self.login_expired = False
        self.outcome = "confirmed"
        self.submissions = 0

    def subtotal(self) -> Decimal:
        return sum(
            (self.products[key].price * count for key, count in self.basket.items()),
            Decimal("0.00"),
        ).quantize(Decimal("0.01"))

    def total(self) -> Decimal:
        return (self.subtotal() + self.fee).quantize(Decimal("0.01"))


SHOP = Shop()


def _slot_label(mode: str, hours: int, fee: str, location: str, note: str = "") -> str:
    start = NOW + timedelta(hours=hours)
    end = start + timedelta(hours=1)
    parts = [
        f"Choose {mode} slot",
        start.isoformat(),
        end.isoformat(),
        f"£{fee}",
        location,
    ]
    if note:
        parts.append(note)
    return " | ".join(parts)


def _button(label: str, target: str) -> str:
    safe = escape(label, quote=True)
    return (
        f'<button aria-label="{safe}" '
        f"onclick=\"location.href='{target}'\">{escape(label)}</button>"
    )


class ShopHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        if SHOP.challenge:
            self._html(
                "<title>Security check</title>"
                "<main>Please complete this security check to continue.</main>"
            )
            return
        if SHOP.login_expired:
            self._html(
                "<title>Sign in</title><main>"
                "<label>Password <input id='password' type='password'></label>"
                "</main>"
            )
            return
        if parts.path == "/act":
            self._act(query)
            return
        if parts.path == "/ecom/shop/search":
            self._search(query.get("searchTerm", [""])[0])
            return
        if parts.path == "/ecom/checkout/fulfilment":
            self._fulfilment()
            return
        if parts.path == "/ecom/shop/trolley":
            self._trolley()
            return
        if parts.path == "/ecom/checkout/review":
            self._review()
            return
        if parts.path == "/ecom/checkout/placed":
            self._placed()
            return
        self._html("<title>Fixture shop</title><main>Nothing here.</main>")

    def _act(self, query: dict[str, list[str]]) -> None:
        key = query.get("add", [""])[0] or query.get("increase", [""])[0]
        if key in SHOP.products:
            SHOP.basket[key] = SHOP.basket.get(key, 0) + 1
        self._search(query.get("term", [""])[0])

    def _search(self, term: str) -> None:
        needle = term.casefold()
        rows: list[str] = []
        for product in SHOP.products.values():
            if not product.available or needle not in product.name.casefold():
                continue
            label = f"Add {product.name} | {product.size} | £{product.price} to trolley"
            if product.favourite:
                label += " | favourite"
            if product.ordered:
                label += f" | ordered {product.ordered} times"
            if product.variable:
                label += " | variable weight"
            rows.append(_button(label, f"/act?add={product.key}&term={term}"))
            if SHOP.basket.get(product.key):
                rows.append(
                    _button(
                        f"Increase quantity of {product.name}",
                        f"/act?increase={product.key}&term={term}",
                    )
                )
        body = "".join(rows) or "<p>No matching products.</p>"
        self._html(f"<title>Search: {escape(term)}</title><main>{body}</main>")

    def _fulfilment(self) -> None:
        buttons = "".join(
            (
                _button(
                    _slot_label(
                        "delivery", 24, "0.00", "12 Example Street", "convenient"
                    ),
                    "/ecom/checkout/fulfilment",
                ),
                _button(
                    _slot_label("delivery", 48, "3.50", "12 Example Street"),
                    "/ecom/checkout/fulfilment",
                ),
                _button(
                    _slot_label("collection", 30, "0.00", "Fixture Shop"),
                    "/ecom/checkout/fulfilment",
                ),
            )
        )
        self._html(f"<title>Fulfilment</title><main>{buttons}</main>")

    def _trolley(self) -> None:
        lines = []
        for key, count in SHOP.basket.items():
            product = SHOP.products[key]
            line = (product.price * count).quantize(Decimal("0.01"))
            lines.append(
                f"<p>Product: {escape(product.name)} | Quantity: {count} | "
                f"Unit: £{product.price} | Line: £{line} | "
                f"Variable: {'yes' if product.variable else 'no'}</p>"
            )
        summary = [
            f"<p>Your basket has {len(SHOP.basket)} items</p>",
            f"<p>Subtotal: £{SHOP.subtotal()}</p>",
        ]
        if SHOP.fee:
            summary.append(f"<p>Delivery fee: £{SHOP.fee}</p>")
        if SHOP.minimum_spend is not None:
            summary.append(f"<p>Minimum spend: £{SHOP.minimum_spend}</p>")
        summary.append(f"<p>Total: £{SHOP.total()}</p>")
        self._html(
            "<title>Trolley</title><main>"
            + "".join(lines)
            + "".join(summary)
            + "</main>"
        )

    def _review(self) -> None:
        self._html(
            "<title>Review</title><main>"
            f"<p>Total: £{SHOP.total()}</p>"
            + _button("Place order", "/ecom/checkout/placed")
            + "</main>"
        )

    def _placed(self) -> None:
        SHOP.submissions += 1
        if SHOP.outcome == "rejected":
            body = "<p>Your payment was declined and the order was not placed.</p>"
        elif SHOP.outcome == "uncertain":
            body = "<p>We are still processing your request. Please wait.</p>"
        else:
            body = (
                "<p>Order confirmed. Thanks for your order.</p>"
                "<p>Order number: WR-FIXTURE-9001</p>"
            )
        self._html(f"<title>Order</title><main>{body}</main>")

    def _html(self, value: str) -> None:
        body = value.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: object) -> None:
        pass


@pytest.fixture(scope="module")
def shop_site() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), ShopHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.fixture(autouse=True)
def clean_shop() -> Iterator[None]:
    SHOP.reset()
    yield
    SHOP.reset()


def browser_config(tmp_path: Path) -> BrowserConfig:
    return BrowserConfig(
        enabled=True,
        state=tmp_path / "state" / "browser.sqlite3",
        profiles=tmp_path / "profiles",
        artifacts=tmp_path / "artifacts",
        socket=tmp_path / "run" / "browser.sock",
        headless=True,
        lease_seconds=120,
        action_timeout_seconds=10,
        observation_max_chars=8_000,
        observation_max_elements=60,
        journal_max_entries=200,
        takeover_url=cast(Any, "https://browser.home.private"),
    )


async def wait_for_socket(path: Path) -> None:
    for _ in range(400):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("browser daemon did not create its socket")


@dataclass
class Harness:
    service: GroceryService
    store: GroceryStore
    client: BrowserClient


@asynccontextmanager
async def harness(
    tmp_path: Path, site: str, **kwargs: object
) -> AsyncIterator[Harness]:
    """Run one disposable browser daemon and grocery service for a scenario."""
    settings = browser_config(tmp_path)
    daemon = BrowserDaemon(settings)
    running = asyncio.create_task(daemon.run())
    try:
        await wait_for_socket(settings.socket)
        client = BrowserClient(settings.socket)
        await client.call("profiles.create", name="grocery-disposable")
        store = GroceryStore(tmp_path / "grocery.sqlite3")
        store.initialize()
        adapter = WaitroseAdapter(
            store, client, profile="grocery-disposable", base_url=site
        )
        yield Harness(
            store=store,
            client=client,
            service=GroceryService(
                store,
                adapter,
                **kwargs,  # type: ignore[arg-type]
            ),
        )
    finally:
        with suppress(Exception):
            await BrowserClient(settings.socket).call("shutdown")
        try:
            await asyncio.wait_for(running, timeout=30)
        except (TimeoutError, Exception):
            running.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await running


async def build_basket(
    service: GroceryService,
    *,
    mode: FulfilmentMode = FulfilmentMode.DELIVERY,
    slot_hours: int = 24,
) -> str:
    draft = service.start((requested(),), mode=mode)
    candidates = await service.search(draft.draft_id, "oats")
    exact = next(item for item in candidates if item.name == "Porridge Oats")
    await service.add_product(draft.draft_id, "oats", exact.product_id)
    listed = await service.list_slots(draft.draft_id)
    chosen = next(
        value
        for value in listed.slots
        if value.starts_at == NOW + timedelta(hours=slot_hours)
    )
    await service.choose_slot(draft.draft_id, chosen.slot_id)
    return draft.draft_id


async def test_search_ranks_favourites_and_reads_variable_weight(
    tmp_path: Path, shop_site: str
) -> None:
    async with harness(tmp_path, shop_site) as values:
        draft = values.service.start((requested(),), mode=FulfilmentMode.DELIVERY)

        candidates = await values.service.search(draft.draft_id, "oats")

        names = [item.name for item in candidates]
        assert names == ["Porridge Oats", "Duchy Organic Porridge Oats"]
        duchy = candidates[1]
        assert duchy.favourite is True
        assert duchy.prior_order_count == 3
        assert "Waitrose favourite" in duchy.evidence
        assert "Seen in 3 prior order(s)" in duchy.evidence
        assert duchy.unit_price == Decimal("2.80")
        assert duchy.size == "1kg"
        assert candidates[0].variable_weight is False


async def test_variable_weight_pricing_is_read_from_the_listing(
    tmp_path: Path, shop_site: str
) -> None:
    async with harness(tmp_path, shop_site) as values:
        draft = values.service.start(
            (requested("bananas", "Bananas"),), mode=FulfilmentMode.DELIVERY
        )

        loose = await values.service.search(draft.draft_id, "bananas")

        assert [item.name for item in loose] == ["Bananas Loose"]
        assert loose[0].variable_weight is True
        assert loose[0].unit_price == Decimal("1.10")


async def test_one_profile_serves_one_mutating_order_at_a_time(
    tmp_path: Path, shop_site: str
) -> None:
    async with harness(tmp_path, shop_site) as values:
        first = values.service.start((requested(),), mode=FulfilmentMode.DELIVERY)
        second = values.service.start(
            (requested("bananas", "Bananas"),), mode=FulfilmentMode.DELIVERY
        )
        await values.service.search(first.draft_id, "oats")

        with pytest.raises(GroceryAdapterError, match="busy with another task"):
            await values.service.search(second.draft_id, "bananas")

        await values.service.cancel(first.draft_id, "Making room for the other order")
        released = await values.service.search(second.draft_id, "bananas")
        assert [item.name for item in released] == ["Bananas Loose"]


async def test_an_unavailable_product_is_omitted_rather_than_guessed(
    tmp_path: Path, shop_site: str
) -> None:
    async with harness(tmp_path, shop_site) as values:
        draft = values.service.start(
            (requested("bread", "Sourdough Loaf"),), mode=FulfilmentMode.DELIVERY
        )

        with pytest.raises(GroceryAdapterError, match="did not expose"):
            await values.service.search(draft.draft_id, "bread")

        omitted = values.service.omit(
            draft.draft_id, "bread", "Sourdough is out of stock today"
        )
        assert omitted.state == GroceryState.RESOLVE
        assert omitted.omitted[0].reason == "Sourdough is out of stock today"


async def test_delivery_and_collection_slots_are_read_separately(
    tmp_path: Path, shop_site: str
) -> None:
    async with harness(tmp_path, shop_site) as values:
        draft = values.service.start((requested(),), mode=FulfilmentMode.DELIVERY)
        candidates = await values.service.search(draft.draft_id, "oats")
        await values.service.add_product(
            draft.draft_id, "oats", candidates[0].product_id
        )

        delivery = await values.service.list_slots(draft.draft_id)
        assert {value.mode for value in delivery.slots} == {FulfilmentMode.DELIVERY}
        assert sorted(str(value.fee) for value in delivery.slots) == ["0.00", "3.50"]
        assert delivery.slots[0].convenient is True

        collection = await values.service.list_slots(
            draft.draft_id, FulfilmentMode.COLLECTION
        )
        assert [value.location for value in collection.slots] == ["Fixture Shop"]
        chosen = await values.service.choose_slot(
            draft.draft_id, collection.slots[0].slot_id
        )
        assert chosen.mode == FulfilmentMode.COLLECTION


async def test_the_reviewed_basket_reconciles_fees_and_the_live_total(
    tmp_path: Path, shop_site: str
) -> None:
    async with harness(tmp_path, shop_site) as values:
        SHOP.fee = Decimal("3.50")
        draft_id = await build_basket(values.service, slot_hours=48)

        reviewed = await values.service.prepare_review(draft_id)

        assert reviewed.state == GroceryState.REVIEW
        snapshot = reviewed.snapshot
        assert snapshot is not None
        assert snapshot.subtotal == Decimal("1.50")
        assert snapshot.fees == Decimal("3.50")
        assert snapshot.total == Decimal("5.00")
        assert [item.name for item in snapshot.items] == ["Porridge Oats"]


async def test_a_minimum_spend_stops_the_order_before_review(
    tmp_path: Path, shop_site: str
) -> None:
    async with harness(tmp_path, shop_site) as values:
        SHOP.minimum_spend = Decimal("40.00")
        draft_id = await build_basket(values.service)

        with pytest.raises(GroceryStateError, match="minimum spend"):
            await values.service.prepare_review(draft_id)


async def test_a_basket_changed_outside_the_draft_is_refused(
    tmp_path: Path, shop_site: str
) -> None:
    async with harness(tmp_path, shop_site) as values:
        draft_id = await build_basket(values.service)
        # Something else put a product in the live trolley.
        SHOP.basket["duchy"] = 1

        with pytest.raises(GroceryAdapterError, match="unexpected number of products"):
            await values.service.prepare_review(draft_id)


async def test_an_expired_login_pauses_for_private_takeover(
    tmp_path: Path, shop_site: str
) -> None:
    async with harness(tmp_path, shop_site) as values:
        draft = values.service.start((requested(),), mode=FulfilmentMode.DELIVERY)
        SHOP.login_expired = True

        with pytest.raises(GroceryTakeoverRequired, match="private login"):
            await values.service.search(draft.draft_id, "oats")


async def test_a_site_challenge_pauses_instead_of_being_worked_around(
    tmp_path: Path, shop_site: str
) -> None:
    async with harness(tmp_path, shop_site) as values:
        draft = values.service.start((requested(),), mode=FulfilmentMode.DELIVERY)
        SHOP.challenge = True

        with pytest.raises(GroceryTakeoverRequired, match="CAPTCHA"):
            await values.service.search(draft.draft_id, "oats")

        SHOP.challenge = False
        paused = await values.service.request_takeover(
            draft.draft_id, "Waitrose showed a security check"
        )
        assert paused["takeover_url"] == "https://browser.home.private/"
        assert values.store.get(draft.draft_id).state == GroceryState.NEEDS_TAKEOVER


async def test_an_approved_order_checks_out_exactly_once(
    tmp_path: Path, shop_site: str
) -> None:
    async with harness(tmp_path, shop_site, checkout_enabled=True) as values:
        draft_id = await build_basket(values.service)
        await values.service.prepare_review(draft_id)
        approval = values.store.create_approval(
            draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
        )
        values.store.attach_approval_message(approval.approval_id, 41)
        values.store.decide_approval(
            approval.approval_id,
            user_id=OWNER,
            chat_id=OWNER,
            message_id=41,
            decision="approve",
        )

        result = await values.service.checkout(draft_id)

        assert result["status"] == "confirmed"
        order = cast(dict[str, Any], result["order"])
        assert order["order_number"] == "WR-FIXTURE-9001"
        assert SHOP.submissions == 1
        assert values.store.get(draft_id).state == GroceryState.CONFIRMED
        stored = values.store.confirmation(draft_id)
        assert isinstance(stored, OrderConfirmation)


async def test_a_declined_payment_is_a_definitive_rejection(
    tmp_path: Path, shop_site: str
) -> None:
    async with harness(tmp_path, shop_site, checkout_enabled=True) as values:
        SHOP.outcome = "rejected"
        draft_id = await build_basket(values.service)
        await values.service.prepare_review(draft_id)
        approval = values.store.create_approval(
            draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
        )
        values.store.attach_approval_message(approval.approval_id, 41)
        values.store.decide_approval(
            approval.approval_id,
            user_id=OWNER,
            chat_id=OWNER,
            message_id=41,
            decision="approve",
        )

        result = await values.service.checkout(draft_id)

        assert result["status"] == "rejected"
        assert SHOP.submissions == 1
        assert values.store.get(draft_id).state == GroceryState.REJECTED


async def test_a_response_without_an_order_number_stays_uncertain(
    tmp_path: Path, shop_site: str
) -> None:
    async with harness(tmp_path, shop_site, checkout_enabled=True) as values:
        SHOP.outcome = "uncertain"
        draft_id = await build_basket(values.service)
        await values.service.prepare_review(draft_id)
        approval = values.store.create_approval(
            draft_id, owner_user_id=OWNER, chat_id=OWNER, ttl_seconds=900
        )
        values.store.attach_approval_message(approval.approval_id, 41)
        values.store.decide_approval(
            approval.approval_id,
            user_id=OWNER,
            chat_id=OWNER,
            message_id=41,
            decision="approve",
        )

        result = await values.service.checkout(draft_id)

        assert result["status"] == "uncertain"
        assert SHOP.submissions == 1
        assert values.store.get(draft_id).state == GroceryState.UNCERTAIN

        # A second attempt must not reach the shop again.
        with pytest.raises(GroceryApprovalError):
            await values.service.checkout(draft_id)
        assert SHOP.submissions == 1
