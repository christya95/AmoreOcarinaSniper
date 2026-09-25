from dataclasses import replace
from decimal import Decimal

import pytest

from amore_ocarina_sniper.models import CheckoutSnapshot, OfferSnapshot
from amore_ocarina_sniper.policy import evaluate_checkout, evaluate_offer, seller_allowed

from .conftest import TARGET_ASIN, make_config


def good_offer() -> OfferSnapshot:
    return OfferSnapshot(
        asin=TARGET_ASIN,
        title="Nintendo Switch™ 2 – The Legend of Zelda™ – 40th Anniversary Edition",
        price=Decimal("709.99"),
        currency="CAD",
        seller="Amazon.ca",
        seller_id=None,
        fulfiller="Amazon",
        condition="new",
        availability="In Stock",
        in_stock=True,
        has_variant_selector=False,
        buy_now_available=True,
        add_to_cart_available=True,
    )


def good_checkout() -> CheckoutSnapshot:
    return CheckoutSnapshot(
        asin=TARGET_ASIN,
        title="Nintendo Switch 2 – The Legend of Zelda – 40th Anniversary Edition",
        seller="Amazon.ca",
        fulfiller="Amazon",
        condition="new",
        quantity=1,
        currency="CAD",
        item_price=Decimal("709.99"),
        total=Decimal("802.29"),
        address_text="Josua Example 123 Maple Street Milton, ON",
        payment_text="Visa ending in 4242",
        line_item_count=1,
        place_order_available=True,
    )


@pytest.fixture
def cfg(tmp_path):
    return make_config(tmp_path)


def test_good_offer_and_checkout_pass(cfg):
    assert evaluate_offer(good_offer(), cfg.policy, cfg.target).ok
    assert evaluate_checkout(good_checkout(), cfg.policy, cfg.target).ok


@pytest.mark.parametrize(
    "change,fragment",
    [
        ({"asin": "B0DIFFERENT"}, "asin mismatch"),
        ({"asin": None}, "asin unreadable"),
        ({"title": "Nintendo Switch 2 – Mario Kart World Bundle"}, "title"),
        ({"has_variant_selector": True}, "variant"),
        ({"in_stock": False, "availability": "Currently unavailable."}, "not in stock"),
        ({"price": None}, "price unreadable"),
        ({"price": Decimal("750.01")}, "exceeds max_item_price"),
        ({"currency": "USD"}, "currency"),
        ({"currency": None}, "currency"),
        ({"seller": "Reseller Depot"}, "seller not allowed"),
        ({"seller": None}, "seller not allowed"),
        ({"fulfiller": "Reseller Depot"}, "fulfillment"),
        ({"condition": "used"}, "condition"),
        ({"condition": None}, "condition"),
        ({"buy_now_available": False, "add_to_cart_available": False}, "no purchase control"),
    ],
)
def test_offer_rejections(cfg, change, fragment):
    decision = evaluate_offer(replace(good_offer(), **change), cfg.policy, cfg.target)
    assert not decision.ok
    assert any(fragment in r for r in decision.reasons), decision.reasons


def test_offer_price_at_limit_allowed(cfg):
    assert evaluate_offer(replace(good_offer(), price=Decimal("750.00")), cfg.policy, cfg.target).ok


def test_fulfillment_any_accepts_third_party_shipper(tmp_path):
    cfg = make_config(tmp_path, fulfillment="any")
    assert evaluate_offer(replace(good_offer(), fulfiller="Reseller Depot"), cfg.policy, cfg.target).ok


@pytest.mark.parametrize(
    "change,fragment",
    [
        ({"asin": "B0EXTRA0001"}, "asin mismatch"),
        ({"asin": None}, "asin unreadable"),
        ({"line_item_count": 2}, "2 line items"),
        ({"line_item_count": None}, "line item count unreadable"),
        ({"quantity": 2}, "quantity 2"),
        ({"quantity": None}, "quantity unreadable"),
        ({"currency": "USD"}, "currency"),
        ({"item_price": Decimal("760.00")}, "exceeds"),
        ({"item_price": None}, "item price unreadable"),
        ({"total": Decimal("860.01")}, "exceeds max_total"),
        ({"total": None}, "total unreadable"),
        ({"total": Decimal("100.00")}, "lower than item price"),
        ({"seller": "Reseller Depot"}, "seller not allowed"),
        ({"fulfiller": None}, "fulfillment"),
        ({"condition": "used"}, "condition"),
        ({"address_text": "9 Other Road"}, "address"),
        ({"address_text": None}, "address"),
        ({"payment_text": "Mastercard ending in 9999"}, "payment"),
        ({"payment_text": None}, "payment"),
        ({"place_order_available": False}, "place order"),
    ],
)
def test_checkout_rejections(cfg, change, fragment):
    decision = evaluate_checkout(replace(good_checkout(), **change), cfg.policy, cfg.target)
    assert not decision.ok
    assert any(fragment in r for r in decision.reasons), decision.reasons


def test_unconfigured_address_or_payment_fails_closed(tmp_path):
    cfg = make_config(tmp_path, approved_address_contains="", approved_payment_contains="")
    decision = evaluate_checkout(good_checkout(), cfg.policy, cfg.target)
    assert not decision.ok
    assert any("approved_address_contains" in r for r in decision.reasons)
    assert any("approved_payment_contains" in r for r in decision.reasons)


def test_seller_aliases():
    assert seller_allowed("Amazon", ("amazon.ca",))
    assert seller_allowed("Amazon.ca", ("amazon.ca",))
    assert seller_allowed("Sold by Amazon.ca".replace("Sold by ", ""), ("amazon",))
    assert not seller_allowed("Amazon Warehouse", ("amazon.ca",))
    assert not seller_allowed("Amazonian Deals", ("amazon.ca",))
    assert not seller_allowed(None, ("amazon.ca",))
