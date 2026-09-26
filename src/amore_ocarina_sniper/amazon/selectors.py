"""Centralized Amazon.ca selectors with verification status.

Verification legend (see README "Live-verified selectors"):
  VERIFIED   – observed live on www.amazon.ca desktop: product page 2026-09-24/25 (signed out
               and signed in) against ASIN B0HJ6F8L6V and an in-stock third-party listing
               (B0HJPFB24P); cart page and checkout review page 2026-09-25 from the
               operator's signed-in session (outerHTML pastes, one third-party item).
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

# ---------------------------------------------------------------------------- cart path
# Pre-orders on this listing expose only "Pre-order now" (the add-to-cart slot), so the cart
# path is the primary path, not a fallback. Everything below marked VERIFIED was read from the
# operator's signed-in cart page (one item) on 2026-09-25 21:04: rows are
# div.sc-list-item[data-asin][data-quantity][data-isselected] inside ul[data-name="Active
# Items"]; the quantity control is an atomic stepper (<fieldset data-action="a-stepper"
# data-steppervalue>) whose decrement <button> is labelled "Decrease quantity by one, …" at
# quantity ≥ 2 but "Delete …" (trash) at quantity 1 — hence one decrement per pass, never a
# burst; each row also carries a *gift* checkbox, so the checkout checkbox must be scoped.
CART_URL = "https://www.amazon.ca/gp/cart/view.html"
NAV_CART_COUNT = "#nav-cart-count"  # VERIFIED (total units in cart)
CART_TOTAL_ITEM_COUNT = "#sc-active-cart[data-cart-total-item-count]"  # VERIFIED (attr)

# After clicking add-to-cart, any of these being *visible* means the add registered.
# Rehearsal 2026-09-25 21:42 (signed in, bot's own profile): the desktop button carries class
# `attach-dss-atc`, adds via AJAX and opens `#attach-desktop-sideSheet` (role=dialog,
# aria-modal) showing a warranty upsell (`#attach-warranty-pane`, "Add to your order",
# `#attachSiAddCoverage` / `#attachSiNoCoverage`) behind `#attach-popover-lgtbox`. No
# "Proceed to checkout" control was offered there, so the cart page follows.
ADD_TO_CART_CONFIRMATION = (
    "#attach-desktop-sideSheet[aria-modal='true']",  # VERIFIED
    "#attach-warranty-pane",  # VERIFIED (display:block once shown)
    "#attach-popover-lgtbox.attach-dss-backdrop",  # VERIFIED
    "#attach-sidesheet-checkout-button",
    "#attach-accessory-cart-button",
    "#NATC_SMART_WAGON_CONF_MSG_SUCCESS",
    "#huc-v2-order-row-confirm-text",
    "#sw-atc-confirmation",
    "#attachDisplayAddBaseAlert",
    "#sc-active-cart",
)
# URL path fragments of the add-to-cart request the button issues (form target
# `/cart/add-to-cart/ref=...` VERIFIED from the button's formaction; the AJAX endpoint the
# side sheet uses is logged by the rehearsal and added here once observed).
ADD_TO_CART_RESPONSE_MARKERS = ("/cart/add-to-cart", "/gp/add-to-cart", "/gp/aws/cart/add")
# Proceed-to-checkout offered directly on the confirmation surface (skips the cart page).
CONFIRMATION_PROCEED = (
    "#attach-sidesheet-checkout-button",
    "#hlb-ptc-btn-native",
    "#sw-ptc-form input[type='submit']",
    "input[name='proceedToRetailCheckout']",
)  # ASSUMED

CART_PAGE = {
    "active": "#sc-active-cart",  # VERIFIED
    "saved": "#sc-saved-cart",  # VERIFIED
    "buy_box": "#sc-buy-box",  # VERIFIED (data-quantity = units in active cart)
    "rows": (
        "#sc-active-cart .sc-list-item[data-asin]",  # VERIFIED
        "#sc-active-cart [data-asin][data-itemid]",
        "#sc-active-cart [data-asin]",
    ),
    "row_quantity_attr": "data-quantity",  # VERIFIED
    "row_selected_attr": "data-isselected",  # VERIFIED ("1" when ticked for checkout)
    "row_quantity_stepper": "fieldset[data-action='a-stepper'][data-steppervalue]",  # VERIFIED
    "row_quantity_label": "[aria-label^='Quantity is'], legend",  # VERIFIED (legend text)
    "row_quantity_select": "select[name='quantity']",  # ASSUMED (legacy rows only)
    "row_quantity_decrement": (
        # Only ever clicked when the row quantity is >= 2 (at 1 the same button deletes).
        "button[data-action='a-stepper-decrement'][aria-label*='Decrease quantity' i]",  # VERIFIED
        "[data-a-selector='decrement'][aria-label*='Decrease quantity' i]",
        "[aria-label*='Decrease quantity' i]",
    ),
    "row_checkbox": (
        ".sc-list-item-checkbox input[type='checkbox']",  # VERIFIED
        "input[type='checkbox'][aria-label*='for checkout' i]",  # VERIFIED wording
    ),
    "row_save_for_later": (
        "input[data-action='save-for-later']",  # VERIFIED (name=submit.save-for-later.<itemid>)
        "input[name^='submit.save-for-later']",
        "input[value='Save for later']",
        "input[aria-label^='Save for later' i]",
    ),
    "row_delete": (
        "input[data-action='delete-active']",  # VERIFIED (name=submit.delete-active.<itemid>)
        "input[name^='submit.delete-active']",
        "input[data-action='delete']",
        "input[value='Delete']",
    ),
    "proceed": (
        # VERIFIED 2026-09-25 21:16 (operator paste): <input name="proceedToRetailCheckout"
        #   data-feature-id="proceed-to-checkout-action" class="a-button-input" type="submit"
        #   value="Proceed to checkout" aria-labelledby="sc-buy-box-ptc-button-announce">
        "input[name='proceedToRetailCheckout']",  # VERIFIED
        "input[data-feature-id='proceed-to-checkout-action']",  # VERIFIED
        "input[aria-labelledby='sc-buy-box-ptc-button-announce']",  # VERIFIED
        "#sc-buy-box-ptc-button input",
        "#sc-buy-box-ptc-button",
    ),
}

# Review page ("SPC", /checkout/p/<purchase-id>/spc). VERIFIED entries were read from the
# operator's signed-in review page on 2026-09-25 21:19 (one third-party item, saved Visa, no
# CVV prompt). The legacy candidates that follow them are kept for A/B layouts.
CHECKOUT_PAGE: dict[str, Field] = {
    "item_title": Field(
        "item_title",
        (
            ".lineitem-title-text",  # VERIFIED
            "[id^='checkout-item-block-item-primary-title-']",  # VERIFIED
            "[data-testid='item-title']",
            ".product-title",
            "#spc-orders .a-truncate-full",
            ".item-row .a-text-bold",
        ),
        "VERIFIED",
    ),
    # Live: <span data-testid="Item_asin_0_0_0" class="aok-hidden">B0GJZ8WJD9</span> — the ASIN
    # is *text*, there is no data-asin attribute anywhere on the page.
    "item_asin": Field(
        "item_asin",
        ("[data-testid^='Item_asin_']",),  # VERIFIED
        "VERIFIED",
    ),
    "item_asin_attr": Field(
        "item_asin_attr",
        ("[data-asin]", "[data-item-asin]"),
        "ASSUMED",
        attr="data-asin",
        note="legacy fallback only; consulted when item_asin is empty",
    ),
    "line_items": Field(
        "line_items",
        (
            ".lineitem-container",  # VERIFIED (one per line item)
            "[data-testid^='Item_asin_']",  # VERIFIED
            "[data-testid='line-item']",
            ".a-box.spc-item",
            "#spc-orders .item-row",
            "[data-asin][data-quantity]",
        ),
        "VERIFIED",
        note="count of matches = line item count",
    ),
    # Live: atomic stepper <fieldset name="checkout-quantity-stepper" data-steppervalue="1">
    # with the value rendered as text in [data-a-selector='inner-value'] and a live region.
    "quantity": Field(
        "quantity",
        (
            "fieldset[name='checkout-quantity-stepper'] [data-a-selector='inner-value']",  # VERIFIED
            "fieldset[name='checkout-quantity-stepper'] .a-stepper-value-live",  # VERIFIED
            "[id^='lineItemQuantity_'] [data-a-selector='inner-value']",  # VERIFIED
            "[data-testid='item-quantity']",
            ".quantity-display",
            "select[name='quantity']",
            ".a-dropdown-prompt",
        ),
        "VERIFIED",
    ),
    "item_price": Field(
        "item_price",
        (
            ".lineitem-price-text",  # VERIFIED
            "[data-testid='item-price']",
            ".a-color-price.a-text-bold",
            "#spc-orders .a-color-price",
        ),
        "VERIFIED",
    ),
    # Live: the "Order Total:" row is the only bold <li> in #subtotals-marketplace-table and the
    # amount sits in [data-shimmer-target='ordertotals-amount']; the same amount is repeated
    # next to the bottom Place-order button in .grand-total-cell.
    "total": Field(
        "total",
        (
            # VERIFIED (both):
            "#subtotals-marketplace-table .a-text-bold [data-shimmer-target='ordertotals-amount']",
            ".grand-total-cell [data-shimmer-target='ordertotals-amount']",
            "[data-testid='order-total'] .a-color-base",
            ".grand-total-price",
            "td.a-text-right.grand-total-price",
            "#order-summary-total",
        ),
        "VERIFIED",
    ),
    "seller": Field(
        "seller",
        (
            ".lineitem-seller-section",  # VERIFIED ("Sold by <name>")
            "[data-testid='sold-by']",
            ".lineitem-soldby",
            ".a-size-small.a-color-secondary:has-text('Sold by')",
        ),
        "VERIFIED",
    ),
    "fulfiller": Field(
        "fulfiller",
        (
            ".product-description-column .a-size-small:has-text('Ships from')",  # VERIFIED
            "[data-testid='ships-from']",
            ".lineitem-shipsfrom",
            ".a-size-small:has-text('Ships from')",
        ),
        "VERIFIED",
    ),
    # Rehearsal 2026-09-25 21:40: a generic ":has-text('Condition')" candidate matched the footer
    # sentence "...privacy notice and Conditions of use" and would have refused the order.
    # Only explicit condition elements are read; the value is also sanity-checked in
    # parse_checkout and otherwise inherited from the product page.
    "condition": Field(
        "condition",
        ("[data-testid='item-condition']", ".lineitem-condition", ".lineitem-container .item-condition"),
        "ASSUMED",
        note="live page renders no condition row; 'new' is inherited from the product page",
    ),
    "address": Field(
        "address",
        (
            "#deliver-to-address-text",  # VERIFIED ("1447, Sycamore Garden, Milton, Ontario, L9E1P8, Canada")
            "#checkout-deliveryAddressPanel",  # VERIFIED (name + address)
            "[data-testid='Address_selectedAddress']",
            "#shipToAddressBlock",
            ".displayAddressDiv",
            "#address-book-entry-0",
            "[data-testid='delivery-address']",
        ),
        "VERIFIED",
    ),
    "payment": Field(
        "payment",
        (
            "#selected-payment-methods-list-container",  # VERIFIED ("Paying with Visa 4105")
            "#payment-option-text-default",  # VERIFIED
            "[id^='selected-payment-method-']",  # VERIFIED
            "#checkout-payment-option-panel",  # VERIFIED (fallback: whole panel text)
            "[data-testid='payment-method']",
            "#payment-information",
            ".payment-method-details",
            ".pmts-instrument-display-detail",
        ),
        "VERIFIED",
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
    # Live: six <input id="placeOrder" name="placeYourOrder1"> — the enabled top/bottom
    # buttons plus four *disabled* blocker/spinner copies that Amazon swaps in while a
    # selection is being updated. Never target a disabled one.
    "place_order": Field(
        "place_order",
        (
            "input[name='placeYourOrder1']:not([disabled])",  # VERIFIED
            "#submitOrderButtonId input[name='placeYourOrder1']:not([disabled])",  # VERIFIED
            "#bottomSubmitOrderButtonId input[name='placeYourOrder1']:not([disabled])",  # VERIFIED
            "[data-testid='SPC_selectPlaceOrder']:not([disabled])",  # VERIFIED
            "#placeYourOrder input",
            "#turbo-checkout-pyo-button",
            "[data-testid='placeYourOrderButton']",
        ),
        "VERIFIED",
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
    # A real block: Amazon names automated access / API support. Stops the bot.
    "access_denied_text": (
        "access denied",
        "to discuss automated access",
        "api-services-support@amazon.com",
    ),
    # Amazon's own error pages under load (the "dog" page, CloudFront 503). Transient: the
    # attempt is retried, the watcher backs off mildly, the bot stays ARMED.
    "server_error_text": (
        "sorry! something went wrong",
        "sorry, something went wrong",
        "request could not be satisfied",
        "service unavailable",
    ),
    "payment_challenge_text": (
        "verify your card",
        "revise payment",
        "payment method was declined",
        "there was a problem with your payment",
    ),
    "signed_out_nav_text": ("hello, sign in",),
}  # captcha/login/signed-out markers VERIFIED as text observed on amazon.ca; others ASSUMED

# VERIFIED 2026-09-25 21:53 (operator's signed-in page): the URL redirects to the current
# Your Orders page, which lists "Order #" cards (`.yohtmlc-order-id`) whose ids all match the
# pattern; the same 3-7-7 shape appears as the purchase id in review-page URLs.
ORDER_HISTORY_URL = "https://www.amazon.ca/gp/css/order-history"
ORDER_ID_PATTERN = r"\b\d{3}-\d{7}-\d{7}\b"


def verification_report() -> list[tuple[str, str, str]]:
    """(page, field, status) rows for `doctor` and the README table."""
    rows = [("product", f.name, f.status) for f in PRODUCT_PAGE.values()]
    rows += [("checkout", f.name, f.status) for f in CHECKOUT_PAGE.values()]
    return rows
