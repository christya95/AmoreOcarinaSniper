"""Purchase policy evaluation. Every check fails closed on missing/ambiguous data."""

from __future__ import annotations

from .config import PurchasePolicy, TargetConfig
from .matching import normalize_text
from .models import CheckoutSnapshot, OfferSnapshot, PolicyDecision

AMAZON_SELLER_ALIASES = frozenset({"amazon", "amazon.ca", "amazon ca", "amazon.com.ca, inc."})
AMAZON_FULFILLER_ALIASES = frozenset({"amazon", "amazon.ca", "amazon ca"})


def _norm_seller(value: str | None) -> str:
    return normalize_text(value).replace(" ", "").replace(".", "") if value else ""


def seller_allowed(seller: str | None, allowed: tuple[str, ...]) -> bool:
    if not seller:
        return False
    s = _norm_seller(seller)
    for candidate in allowed:
        c = _norm_seller(candidate)
        if not c:
            continue
        if s == c:
            return True
        # "amazon.ca" configured should accept "Amazon" / "Amazon.ca" display variants.
        if c in {"amazon", "amazonca"} and s in {"amazon", "amazonca", "amazoncomcainc"}:
            return True
    return False


def fulfiller_ok(fulfiller: str | None, requirement: str) -> bool:
    if requirement == "any":
        return True
    if not fulfiller:
        return False
    f = normalize_text(fulfiller)
    f = f.removeprefix("ships from").strip()
    return f in AMAZON_FULFILLER_ALIASES or f.replace(" ", "") in {"amazon", "amazonca"}


def title_matches_target(title: str | None, target: TargetConfig) -> bool:
    if not title:
        return False
    t = normalize_text(title)
    return all(normalize_text(fragment) in t for fragment in target.title_must_contain)


def evaluate_offer(offer: OfferSnapshot, policy: PurchasePolicy, target: TargetConfig) -> PolicyDecision:
    """Gate between VERIFYING and CHECKOUT_READY. Uses only what the product page shows."""
    reasons: list[str] = []
    if not offer.asin:
        reasons.append("asin unreadable")
    elif offer.asin.upper() != target.asin:
        reasons.append(f"asin mismatch: page={offer.asin} expected={target.asin}")
    if not title_matches_target(offer.title, target):
        reasons.append("title does not match configured target")
    if offer.has_variant_selector:
        reasons.append("variant selector present; variant ambiguity fails closed")
    if not offer.in_stock:
        reasons.append(f"not in stock: {offer.availability or 'availability unreadable'}")
    if offer.price is None:
        reasons.append("price unreadable")
    elif offer.price > policy.max_item_price:
        reasons.append(f"price {offer.price} exceeds max_item_price {policy.max_item_price}")
    if offer.currency != policy.currency:
        reasons.append(f"currency {offer.currency or 'unreadable'} != {policy.currency}")
    if not seller_allowed(offer.seller, policy.allowed_sellers):
        reasons.append(f"seller not allowed: {offer.seller or 'unreadable'}")
    if not fulfiller_ok(offer.fulfiller, policy.fulfillment):
        reasons.append(f"fulfillment not acceptable: {offer.fulfiller or 'unreadable'}")
    if (offer.condition or "").strip().lower() != policy.condition:
        reasons.append(f"condition {offer.condition or 'unreadable'} != {policy.condition}")
    if not (offer.buy_now_available or offer.add_to_cart_available):
        reasons.append("no purchase control available")
    return PolicyDecision(ok=not reasons, reasons=reasons)


def evaluate_checkout(snap: CheckoutSnapshot, policy: PurchasePolicy, target: TargetConfig) -> PolicyDecision:
    """Final gate immediately before submission. Everything must be readable and exact."""
    reasons: list[str] = []
    if not snap.asin:
        reasons.append("checkout asin unreadable")
    elif snap.asin.upper() != target.asin:
        reasons.append(f"checkout asin mismatch: {snap.asin}")
    if not title_matches_target(snap.title, target):
        reasons.append("checkout item title does not match target")
    if snap.line_item_count is None:
        reasons.append("line item count unreadable")
    elif snap.line_item_count != 1:
        reasons.append(f"checkout contains {snap.line_item_count} line items; expected exactly 1")
    if snap.quantity is None:
        reasons.append("quantity unreadable")
    elif snap.quantity != policy.quantity:
        reasons.append(f"quantity {snap.quantity} != {policy.quantity}")
    if snap.currency != policy.currency:
        reasons.append(f"checkout currency {snap.currency or 'unreadable'} != {policy.currency}")
    if snap.item_price is None:
        reasons.append("checkout item price unreadable")
    elif snap.item_price > policy.max_item_price:
        reasons.append(f"checkout item price {snap.item_price} exceeds {policy.max_item_price}")
    if snap.total is None:
        reasons.append("order total unreadable")
    elif snap.total > policy.max_total:
        reasons.append(f"order total {snap.total} exceeds max_total {policy.max_total}")
    if snap.item_price is not None and snap.total is not None and snap.total < snap.item_price:
        reasons.append("order total lower than item price; page state ambiguous")
    if not seller_allowed(snap.seller, policy.allowed_sellers):
        reasons.append(f"checkout seller not allowed: {snap.seller or 'unreadable'}")
    if not fulfiller_ok(snap.fulfiller, policy.fulfillment):
        reasons.append(f"checkout fulfillment not acceptable: {snap.fulfiller or 'unreadable'}")
    if (snap.condition or "").strip().lower() != policy.condition:
        reasons.append(f"checkout condition {snap.condition or 'unreadable'} != {policy.condition}")
    addr_needle = normalize_text(policy.approved_address_contains)
    if not addr_needle:
        reasons.append("approved_address_contains not configured")
    elif not snap.address_text or addr_needle not in normalize_text(snap.address_text):
        reasons.append("shipping address does not match approved address")
    pay_needle = normalize_text(policy.approved_payment_contains)
    if not pay_needle:
        reasons.append("approved_payment_contains not configured")
    elif not snap.payment_text or pay_needle not in normalize_text(snap.payment_text):
        reasons.append("payment method does not match approved payment")
    if not snap.place_order_available:
        reasons.append("place order control not available")
    return PolicyDecision(ok=not reasons, reasons=reasons)
