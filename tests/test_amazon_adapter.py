"""Real AmazonAdapter + real Chromium, with www.amazon.ca routed to local fixtures.

No request ever leaves the machine: every https://www.amazon.ca/** request is fulfilled
from tests/fixtures/amazon. Nothing here can place an order.
"""

from __future__ import annotations

import time
from decimal import Decimal
from urllib.parse import urlparse

import pytest

from amore_ocarina_sniper.amazon.adapter import AmazonAdapter, DryRunRefusal
from amore_ocarina_sniper.coordinator import PurchaseCoordinator
from amore_ocarina_sniper.models import ChallengeDetected, ChallengeKind, PurchaseState
from amore_ocarina_sniper.policy import evaluate_checkout, evaluate_offer
from amore_ocarina_sniper.telemetry import Telemetry

from .conftest import FIXTURES
from .test_coordinator import event

pytestmark = pytest.mark.browser

AMZ = FIXTURES / "amazon"


class Routing:
    def __init__(self, product="product_in_stock.html", checkout="checkout_review"):
        self.product = product
        self.checkout = checkout
        self.requests: list[str] = []

    async def handle(self, route):
        url = route.request.url
        self.requests.append(url)
        path = urlparse(url).path
        if path.startswith("/dp/"):
            file = self.product
        elif path.startswith("/gp/buy/spc/handlers/display.html"):
            file = "checkout.html"
        elif path.startswith("/gp/buy/thankyou"):
            file = "thankyou.html"
        elif path.startswith("/gp/css/order-history"):
            file = "thankyou.html"
        else:
            file = "blank.html"
        await route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=(AMZ / file).read_text(encoding="utf-8"),
        )


async def start_adapter(config, routing: Routing, *, dry_run=True) -> AmazonAdapter:
    adapter = AmazonAdapter(config, dry_run=dry_run, headless=True)
    await adapter.start()
    await adapter._context.add_init_script(f"window.__checkoutFixture = {routing.checkout!r};")
    await adapter._context.route("https://www.amazon.ca/**", routing.handle)
    return adapter


async def test_offer_in_stock_parses_verified_fields(config):
    routing = Routing()
    adapter = await start_adapter(config, routing)
    try:
        offer = await adapter.verify_offer()
    finally:
        await adapter.stop()
    assert offer.asin == "B0HJ6F8L6V"
    assert offer.title.startswith("Nintendo Switch")
    assert offer.price == Decimal("709.99") and offer.currency == "CAD"
    assert offer.seller == "Amazon.ca" and offer.fulfiller == "Amazon"
    assert offer.condition == "new" and offer.in_stock
    assert offer.buy_now_available and offer.add_to_cart_available
    assert not offer.has_variant_selector
    assert evaluate_offer(offer, config.policy, config.target).ok
    assert all(urlparse(u).hostname == "www.amazon.ca" for u in routing.requests)


async def test_poll_offer_uses_a_separate_tab(config):
    adapter = await start_adapter(config, Routing())
    try:
        offer = await adapter.poll_offer()
        assert offer.asin == "B0HJ6F8L6V" and offer.in_stock
        assert adapter._watch_page is not None and adapter._watch_page is not adapter.page
        assert adapter.page.url == "about:blank"  # main tab never navigated by a poll
        assert adapter._watch_page.url == config.target.url
        # Polling must not leak state that the checkout path reads from the main tab.
        assert adapter._last_offer_condition is None
        again = await adapter.poll_offer()
        assert again.asin == offer.asin and len(adapter._context.pages) == 2
    finally:
        await adapter.stop()


async def test_offer_unavailable_fails_closed(config):
    adapter = await start_adapter(config, Routing(product="product_unavailable.html"))
    try:
        offer = await adapter.verify_offer()
    finally:
        await adapter.stop()
    assert not offer.in_stock and offer.price is None and not offer.buy_now_available
    decision = evaluate_offer(offer, config.policy, config.target)
    assert not decision.ok
    assert any("not in stock" in r for r in decision.reasons)


async def test_offer_preorder_parses_and_is_policy_gated(config, tmp_path):
    from dataclasses import replace

    from .conftest import make_config

    adapter = await start_adapter(config, Routing(product="product_preorder.html"))
    try:
        offer = await adapter.verify_offer()
    finally:
        await adapter.stop()
    assert offer.is_preorder and not offer.in_stock
    assert offer.price == Decimal("709.99") and offer.currency == "CAD"
    assert offer.seller == "Amazon.ca" and offer.fulfiller is None
    assert offer.condition == "new"  # inferred from the buy box for pre-orders too
    assert offer.buy_now_available and offer.add_to_cart_available
    # Diagnostics retained: which selector produced each value and what was present.
    assert offer.raw["fields"]["seller"]["selector"]
    assert offer.raw["present"]["fulfiller"] is False
    assert offer.raw["hasVisibleButton"]["add_to_cart"] is True

    decision = evaluate_offer(offer, config.policy, config.target)
    assert not decision.ok
    assert [r for r in decision.reasons if "pre-order not allowed" in r]
    assert not [r for r in decision.reasons if "fulfillment" in r or "condition" in r]

    allowed = make_config(tmp_path / "allow", allow_preorder=True)
    assert evaluate_offer(replace(offer), allowed.policy, allowed.target).ok


async def test_offer_third_party_variant_usd_rejected(config):
    adapter = await start_adapter(config, Routing(product="product_third_party.html"))
    try:
        offer = await adapter.verify_offer()
    finally:
        await adapter.stop()
    assert offer.has_variant_selector
    assert offer.seller == "Reseller Depot" and offer.seller_id == "A1RX2MS9L6OBHO"
    assert offer.currency == "USD" and offer.price is None
    reasons = evaluate_offer(offer, config.policy, config.target).reasons
    assert any("variant" in r for r in reasons)
    assert any("seller" in r for r in reasons)
    assert any("currency" in r for r in reasons)


async def test_wrong_asin_rejected(config):
    adapter = await start_adapter(config, Routing(product="product_wrong_asin.html"))
    try:
        offer = await adapter.verify_offer()
    finally:
        await adapter.stop()
    reasons = evaluate_offer(offer, config.policy, config.target).reasons
    assert any("asin mismatch" in r for r in reasons)
    assert any("title" in r for r in reasons)


@pytest.mark.parametrize(
    "fixture,kind",
    [
        ("captcha.html", ChallengeKind.CAPTCHA),
        ("signin.html", ChallengeKind.LOGIN_REQUIRED),
        ("product_signed_out.html", ChallengeKind.LOGIN_REQUIRED),
    ],
)
async def test_challenges_are_raised_not_bypassed(config, fixture, kind):
    adapter = await start_adapter(config, Routing(product=fixture))
    try:
        with pytest.raises(ChallengeDetected) as exc:
            await adapter.verify_offer()
    finally:
        await adapter.stop()
    assert exc.value.kind == kind


async def test_checkout_review_parses_and_dry_run_refuses_submit(config):
    adapter = await start_adapter(config, Routing())
    try:
        offer = await adapter.verify_offer()
        snap = await adapter.prepare_checkout(offer)
        assert snap.asin == "B0HJ6F8L6V" and snap.quantity == 1 and snap.line_item_count == 1
        assert snap.item_price == Decimal("709.99") and snap.total == Decimal("802.29")
        assert snap.seller == "Amazon.ca" and snap.fulfiller == "Amazon" and snap.condition == "new"
        assert "123 Maple Street" in snap.address_text and "ending in 4242" in snap.payment_text
        assert snap.place_order_available
        assert evaluate_checkout(snap, config.policy, config.target).ok
        with pytest.raises(DryRunRefusal):
            await adapter.submit_order()
        await adapter.abandon()
        assert adapter.page.url == config.target.url
    finally:
        await adapter.stop()


@pytest.mark.parametrize(
    "variant,fragment",
    [
        ("two_items", "2 line items"),
        ("wrong_address", "address"),
        ("wrong_payment", "payment"),
        ("unreadable_total", "total unreadable"),
        ("over_total", "exceeds max_total"),
        ("third_party_seller", "seller not allowed"),
        ("qty_two", "quantity 2"),
    ],
)
async def test_checkout_variants_rejected(config, variant, fragment):
    adapter = await start_adapter(config, Routing(checkout=variant))
    try:
        offer = await adapter.verify_offer()
        snap = await adapter.prepare_checkout(offer)
    finally:
        await adapter.stop()
    decision = evaluate_checkout(snap, config.policy, config.target)
    assert not decision.ok
    assert any(fragment in r for r in decision.reasons), decision.reasons


async def test_live_submit_confirms_order_id(config):
    adapter = await start_adapter(config, Routing(), dry_run=False)
    try:
        offer = await adapter.verify_offer()
        await adapter.prepare_checkout(offer)
        await adapter.submit_order()
        order_id = await adapter.confirm_order()
    finally:
        await adapter.stop()
    assert order_id == "702-1234567-7654321"


async def test_live_submit_ambiguous_returns_none(config):
    adapter = await start_adapter(config, Routing(checkout="ambiguous"), dry_run=False)
    try:
        offer = await adapter.verify_offer()
        await adapter.prepare_checkout(offer)
        await adapter.submit_order()
        order_id = await adapter.confirm_order()
    finally:
        await adapter.stop()
    assert order_id is None


async def test_live_submit_payment_challenge_raises(config):
    adapter = await start_adapter(config, Routing(checkout="payment_challenge"), dry_run=False)
    try:
        offer = await adapter.verify_offer()
        await adapter.prepare_checkout(offer)
        await adapter.submit_order()
        with pytest.raises(ChallengeDetected) as exc:
            await adapter.confirm_order()
    finally:
        await adapter.stop()
    assert exc.value.kind == ChallengeKind.PAYMENT_CHALLENGE


async def test_end_to_end_dry_run_then_live_with_real_adapter(config, store):
    """Coordinator + real adapter + fixtures: dry-run never submits; live purchases once."""
    now = int(time.time() * 1000)
    routing = Routing()
    adapter = await start_adapter(config, routing, dry_run=True)
    try:
        coord = PurchaseCoordinator(
            config=config, store=store, adapter=adapter, telemetry=Telemetry(None, False), dry_run=True
        )
        store.arm(now + 600_000, at_ms=now)
        out = await coord.handle_trigger(event(1))
        assert out.dry_run and out.final_state == PurchaseState.ARMED
        assert not any("/gp/buy/thankyou" in u for u in routing.requests)
    finally:
        await adapter.stop()

    adapter = await start_adapter(config, routing, dry_run=False)
    try:
        coord = PurchaseCoordinator(
            config=config, store=store, adapter=adapter, telemetry=Telemetry(None, False), dry_run=False
        )
        out = await coord.handle_trigger(event(2))
        assert out.final_state == PurchaseState.PURCHASED and out.order_id == "702-1234567-7654321"
        assert sum("/gp/buy/thankyou" in u for u in routing.requests) == 1
        out3 = await coord.handle_trigger(event(3))
        assert "disabled" in out3.reason
        assert sum("/gp/buy/thankyou" in u for u in routing.requests) == 1
    finally:
        await adapter.stop()
