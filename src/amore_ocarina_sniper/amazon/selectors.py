"""Centralized Amazon.ca selectors with verification status.

Verification legend (see README "Live-verified selectors"):
  VERIFIED   – observed live on www.amazon.ca desktop (signed out) on 2026-09-24 against
               ASIN B0HJ6F8L6V (unavailable state) and an in-stock third-party listing
               (B0HJPFB24P) for buy-box structure.
  ASSUMED    – based on long-standing Amazon markup that could not be observed in this
               environment (requires an authenticated session / in-stock target).
               Every ASSUMED field is read fail-closed: if none of the candidates yields
               a value, the attempt stops before submission.

Each field lists candidate selectors in priority order. The extractor picks the first
candidate that exists and yields non-empty text/value.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Field:
    name: str
    candidates: tuple[str, ...]
    status: str  # VERIFIED | ASSUMED
    attr: str | None = None  # read attribute instead of text
    note: str = ""


PRODUCT_URL_HOST = "www.amazon.ca"

PRODUCT_PAGE: dict[str, Field] = {
    "asin": Field("asin", ("input#ASIN",), "VERIFIED", attr="value"),
    "title": Field("title", ("#productTitle",), "VERIFIED"),
    "price": Field(
        "price",
        (
            "#corePrice_feature_div .a-price .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
            "#apex_desktop .a-price .a-offscreen",
            "#price_inside_buybox",
        ),
        "VERIFIED",
        note="first candidate observed as '$109.99' on in-stock listing",
    ),
    "availability": Field("availability", ("#availability", "#outOfStock"), "VERIFIED"),
    "seller_link": Field(
        "seller_link",
        ("#sellerProfileTriggerId",),
        "VERIFIED",
        attr="href",
        note="third-party seller; href contains seller=<id>",
    ),
    "seller": Field(
        "seller",
        (
            "#sellerProfileTriggerId",
            "#merchantInfoFeature_feature_div .offer-display-feature-text-message",
            "#merchantInfoFeature_feature_div",
            "#merchant-info",
        ),
        "VERIFIED",
        note="Amazon-as-seller text ('Amazon.ca') not observed live; treated via aliases",
    ),
    "fulfiller": Field(
        "fulfiller",
        (
            "#fulfillerInfoFeature_feature_div .offer-display-feature-text-message",
            "#fulfillerInfoFeature_feature_div",
        ),
        "VERIFIED",
        note="observed 'Ships from Amazon'",
    ),
    "buy_now": Field("buy_now", ("#buy-now-button", "input[name='submit.buy-now']"), "VERIFIED"),
    "add_to_cart": Field(
        "add_to_cart", ("#add-to-cart-button", "input[name='submit.add-to-cart']"), "VERIFIED"
    ),
    "quantity": Field("quantity", ("#quantity",), "VERIFIED", attr="value"),
    "twister": Field(
        "twister",
        ("#twister", "#twister_feature_div [id^='variation_']", "#inline-twister-expander-content-size_name"),
        "VERIFIED",
        note="absent on target ASIN; present on variant listings",
    ),
    "used_buybox": Field("used_buybox", ("#usedBuySection", "#usedbuyBox", "#usedAccordionRow"), "ASSUMED"),
    "renewed_caption": Field(
        "renewed_caption",
        ("#renewedSingleOfferCaption_feature_div",),
        "VERIFIED",
        note="present but empty on a new-condition listing",
    ),
    "buybox": Field("buybox", ("#desktop_buybox", "#buybox"), "VERIFIED"),
    "account_nav": Field(
        "account_nav",
        ("#nav-link-accountList",),
        "VERIFIED",
        note="signed-out text observed: 'Hello, sign in'",
    ),
}

# Buy Now on desktop may open a "turbo checkout" iframe instead of navigating.
TURBO_IFRAME = ("#turbo-checkout-iframe", "iframe[name='turbo-checkout-iframe']")  # ASSUMED

CHECKOUT_PAGE: dict[str, Field] = {
    "item_title": Field(
        "item_title",
        (
            "[data-testid='item-title']",
            ".lineitem-title-text",
            ".a-row .a-spacing-small .a-text-bold[data-asin]",
            ".product-title",
            "#spc-orders .a-truncate-full",
            ".item-row .a-text-bold",
        ),
        "ASSUMED",
    ),
    "item_asin": Field(
        "item_asin",
        ("[data-asin]", "input[name*='asin' i]", "[data-item-asin]"),
        "ASSUMED",
        attr="data-asin",
    ),
    "line_items": Field(
        "line_items",
        (
            "[data-testid='line-item']",
            ".lineitem-container",
            ".a-box.spc-item",
            "#spc-orders .item-row",
            "[data-asin][data-quantity]",
        ),
        "ASSUMED",
        note="count of matches = line item count",
    ),
    "quantity": Field(
        "quantity",
        (
            "[data-testid='item-quantity']",
            ".quantity-display",
            "select[name='quantity']",
            ".a-dropdown-prompt",
            "[data-quantity]",
        ),
        "ASSUMED",
    ),
    "item_price": Field(
        "item_price",
        (
            "[data-testid='item-price']",
            ".lineitem-price-text",
            ".a-color-price.a-text-bold",
            "#spc-orders .a-color-price",
        ),
        "ASSUMED",
    ),
    "total": Field(
        "total",
        (
            "[data-testid='order-total'] .a-color-base",
            ".grand-total-price",
            "#subtotals-marketplace-table .grand-total-price",
            "td.a-text-right.grand-total-price",
            "#order-summary-total",
        ),
        "ASSUMED",
    ),
    "seller": Field(
        "seller",
        (
            "[data-testid='sold-by']",
            ".lineitem-soldby",
            "#spc-orders .a-size-small:has-text('Sold by')",
            ".a-size-small.a-color-secondary:has-text('Sold by')",
        ),
        "ASSUMED",
    ),
    "fulfiller": Field(
        "fulfiller",
        (
            "[data-testid='ships-from']",
            ".lineitem-shipsfrom",
            ".a-size-small:has-text('Ships from')",
        ),
        "ASSUMED",
    ),
    "condition": Field(
        "condition",
        ("[data-testid='item-condition']", ".lineitem-condition", ".a-size-small:has-text('Condition')"),
        "ASSUMED",
        note="absent => treated as 'new' only when the product-page condition was new",
    ),
    "address": Field(
        "address",
        (
            "[data-testid='Address_selectedAddress']",
            "#shipToAddressBlock",
            ".displayAddressDiv",
            "#address-book-entry-0",
            ".ship-to-this-address",
            "#checkout-deliver-to-section",
            "[data-testid='delivery-address']",
        ),
        "ASSUMED",
    ),
    "payment": Field(
        "payment",
        (
            "[data-testid='payment-method']",
            "#payment-information",
            ".payment-method-details",
            "#selected-payment-method",
            "#checkout-payment-section",
            ".pmts-instrument-display-detail",
        ),
        "ASSUMED",
    ),
    "payment_input": Field(
        "payment_input",
        (
            "input[name*='cvv' i]",
            "input[id*='cvv' i]",
            "input[name*='securitycode' i]",
            "input[placeholder*='security code' i]",
            "input[aria-label*='security code' i]",
            "input[name*='addCreditCardNumber' i]",
            "input[placeholder*='card number' i]",
        ),
        "ASSUMED",
        note="checked for *visibility* only; a hidden widget input never fails the attempt",
    ),
    "place_order": Field(
        "place_order",
        (
            "input[name='placeYourOrder1']",
            "#submitOrderButtonId input",
            "#bottomSubmitOrderButtonId input",
            "#placeYourOrder input",
            "#turbo-checkout-pyo-button",
            "[data-testid='placeYourOrderButton']",
            "input[aria-labelledby='submitOrderButtonId-announce']",
        ),
        "ASSUMED",
    ),
}

CONFIRMATION = {
    "url_markers": ("/gp/buy/thankyou", "/checkout/thankyou", "/buy/thankyou", "thankyou"),
    "text_markers": (
        "order placed, thanks",
        "thank you, your order has been placed",
        "your order has been placed",
        "order confirmation",
    ),
    "order_id_selectors": (
        ".order-number",
        "[data-testid='order-id']",
        "#order-id",
        ".a-size-medium.a-color-base:has-text('Order #')",
    ),
}  # ASSUMED

CHALLENGES = {
    "captcha_selectors": (
        "form[action*='validateCaptcha']",
        "#captchacharacters",
        "input[name='cvf_captcha_input']",
    ),
    "captcha_text": ("enter the characters you see below", "type the characters you see", "robot check"),
    "login_url_markers": ("/ap/signin", "/ap/register"),
    "login_selectors": ("#ap_email", "#ap_password", "#signInSubmit"),
    "mfa_url_markers": ("/ap/mfa", "/ap/cvf"),
    "mfa_selectors": ("#auth-mfa-otpcode", "#cvf-page-content", "input[name='otpCode']"),
    "access_denied_text": (
        "sorry! something went wrong",
        "request could not be satisfied",
        "access denied",
        "to discuss automated access",
    ),
    "payment_challenge_text": (
        "verify your card",
        "revise payment",
        "payment method was declined",
        "there was a problem with your payment",
    ),
    "signed_out_nav_text": ("hello, sign in",),
}  # captcha/login/signed-out markers VERIFIED as text observed on amazon.ca; others ASSUMED

ORDER_HISTORY_URL = "https://www.amazon.ca/gp/css/order-history"  # ASSUMED
ORDER_ID_PATTERN = r"\b\d{3}-\d{7}-\d{7}\b"


def verification_report() -> list[tuple[str, str, str]]:
    """(page, field, status) rows for `doctor` and the README table."""
    rows = [("product", f.name, f.status) for f in PRODUCT_PAGE.values()]
    rows += [("checkout", f.name, f.status) for f in CHECKOUT_PAGE.values()]
    return rows
