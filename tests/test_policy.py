from dataclasses import replace
from decimal import Decimal

import pytest

from amore_ocarina_sniper.models import CheckoutSnapshot, OfferSnapshot
from amore_ocarina_sniper.policy import evaluate_checkout, evaluate_offer, payment_matches, seller_allowed

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


def test_missing_ships_from_row_inferred_only_for_amazon_seller(cfg):
    # Live 2026-09-25 03:52: Amazon.ca-sold pre-order rendered no "Ships from" row at all.
    assert evaluate_offer(replace(good_offer(), fulfiller=None, seller="Amazon.ca"), cfg.policy, cfg.target).ok
    assert evaluate_offer(replace(good_offer(), fulfiller=None, seller="Amazon"), cfg.policy, cfg.target).ok
    d = evaluate_offer(replace(good_offer(), fulfiller=None, seller="Reseller Depot"), cfg.policy, cfg.target)
    assert not d.ok and any("fulfillment" in r for r in d.reasons)
    assert evaluate_checkout(replace(good_checkout(), fulfiller=None, seller="Amazon.ca"), cfg.policy, cfg.target).ok
    d = evaluate_checkout(replace(good_checkout(), fulfiller=None, seller="Reseller Depot"), cfg.policy, cfg.target)
    assert not d.ok and any("fulfillment" in r for r in d.reasons)


PREORDER_AVAIL = "This item will be released on October 29, 2026. Pre-order now."


def test_preorder_rejected_by_default_with_explicit_reason(cfg):
    offer = replace(good_offer(), in_stock=False, is_preorder=True, availability=PREORDER_AVAIL)
    d = evaluate_offer(offer, cfg.policy, cfg.target)
    assert not d.ok
    assert any("pre-order not allowed" in r and "allow_preorder" in r for r in d.reasons), d.reasons
    assert not any("not in stock" in r for r in d.reasons)


def test_preorder_accepted_when_allowed(tmp_path):
    cfg = make_config(tmp_path, allow_preorder=True)
    offer = replace(good_offer(), in_stock=False, is_preorder=True, availability=PREORDER_AVAIL)
    assert evaluate_offer(offer, cfg.policy, cfg.target).ok
    # allow_preorder does not weaken any other check.
    assert not evaluate_offer(replace(offer, price=Decimal("750.01")), cfg.policy, cfg.target).ok
    assert not evaluate_offer(replace(offer, seller="Reseller Depot"), cfg.policy, cfg.target).ok
    # A plain out-of-stock listing is still refused.
    oos = replace(good_offer(), in_stock=False, is_preorder=False, availability="Currently unavailable.")
    assert not evaluate_offer(oos, cfg.policy, cfg.target).ok


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
        ({"fulfiller": None, "seller": "Reseller Depot"}, "fulfillment"),  # no row + non-Amazon seller
        ({"condition": "used"}, "condition"),
        ({"address_text": "9 Other Road"}, "address"),
        ({"address_text": None}, "address"),
        ({"payment_text": "Mastercard ending in 9999"}, "payment"),
        ({"payment_text": None}, "payment"),
        ({"payment_text": "Visa ending in 1042, expires 42/42"}, "payment"),  # 4242 not a digit group
        ({"payment_input_required": True}, "payment requires manual input"),
        ({"place_order_available": False}, "place order"),
    ],
)
def test_checkout_rejections(cfg, change, fragment):
    decision = evaluate_checkout(replace(good_checkout(), **change), cfg.policy, cfg.target)
    assert not decision.ok
    assert any(fragment in r for r in decision.reasons), decision.reasons


@pytest.mark.parametrize(
    "text",
    [
        "Visa ending in 4242",
        "Paying with Visa ending in 4242",
        "Visa •••• 4242",
        "Visa ****4242",
        "Visa ...4242",
        "Signature RBC Rewards Visa x4242",
        "VISA\n4242\nChange",
    ],
)
def test_payment_renderings_accepted(text):
    assert payment_matches("ending in 4242", text)


@pytest.mark.parametrize(
    "text",
    ["Visa ending in 9999", "Visa •••• 14242", "Visa 42420", "", None, "Amazon.ca Gift Card balance"],
)
def test_payment_renderings_rejected(text):
    assert not payment_matches("ending in 4242", text)


def test_payment_fragment_without_digits_is_exact_only():
    assert payment_matches("RBC Rewards Visa", "Signature RBC Rewards Visa ending in 4242")
    assert not payment_matches("RBC Rewards Visa", "Visa •••• 4242")


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


# ------------------------------------------------------- challenge classification (extract)
def _raw(body: str, url: str = "https://www.amazon.ca/dp/X") -> dict:
    return {"url": url, "bodyTextLower": body.lower(), "present": {}, "fields": {}}


@pytest.mark.parametrize(
    "body,kind",
    [
        ("Sorry! Something went wrong on our end. Please go back and try again.", "server_error"),
        ("503 ERROR The request could not be satisfied.", "server_error"),
        ("Access Denied You don't have permission to access this page.", "access_denied"),
        ("To discuss automated access to Amazon data please contact api-services-support@amazon.com.",
         "access_denied"),
        ("Enter the characters you see below", "captcha"),
        ("Nintendo Switch 2 - Pre-order now. Sold by Amazon.ca.", None),
    ],
)
def test_amazon_error_pages_are_transient_but_blocks_are_not(body, kind):
    from amore_ocarina_sniper.amazon.extract import detect_challenge_from_raw
    from amore_ocarina_sniper.models import ChallengeKind

    assert detect_challenge_from_raw(_raw(body)) == kind
    if kind:
        assert ChallengeKind(kind).transient == (kind == "server_error")
