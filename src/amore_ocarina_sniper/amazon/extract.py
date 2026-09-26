"""DOM extraction (one JS round trip per page) and pure parsing into snapshots.

The JS extractor receives the centralized selector table and returns raw strings; the
Python side does all interpretation with fail-closed semantics. Keeping interpretation
in Python makes it unit-testable against fixture HTML and against plain dicts.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..matching import normalize_text
from ..models import CheckoutSnapshot, OfferSnapshot
from ..money import detect_currency, parse_money
from . import selectors as S

# Supports plain CSS plus a trailing Playwright-style :has-text('...') filter.
EXTRACT_JS = r"""
(spec) => {
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim();
  const hasTextRe = /^(.*):has-text\('([^']*)'\)$/;
  const queryAll = (sel) => {
    const m = sel.match(hasTextRe);
    if (!m) { try { return Array.from(document.querySelectorAll(sel)); } catch (e) { return []; } }
    const needle = m[2].toLowerCase();
    let base;
    try { base = Array.from(document.querySelectorAll(m[1])); } catch (e) { return []; }
    return base.filter(el => (el.textContent || "").toLowerCase().includes(needle));
  };
  const visible = (el) => !!(el && (el.offsetParent !== null || el.getClientRects().length));
  const read = (el, attr) => {
    if (!el) return null;
    if (attr === "value") return el.value != null ? String(el.value) : el.getAttribute("value");
    if (attr) return el.getAttribute(attr);
    return norm(el.innerText || el.textContent);
  };
  const out = { url: location.href, title: document.title, fields: {}, counts: {}, present: {} };
  for (const [name, f] of Object.entries(spec.fields)) {
    let value = null, matched = null, count = 0, anyPresent = false;
    for (const sel of f.candidates) {
      const els = queryAll(sel);
      if (!els.length) continue;
      anyPresent = true;
      if (f.count) { count = Math.max(count, els.length); }
      for (const el of els) {
        const v = read(el, f.attr);
        if (v != null && String(v).trim() !== "") { value = String(v); matched = sel; break; }
      }
      if (value != null) break;
    }
    out.fields[name] = { value, selector: matched };
    out.present[name] = anyPresent;
    if (f.count) out.counts[name] = count;
  }
  out.bodyTextLower = norm(document.body ? document.body.innerText : "").toLowerCase().slice(0, 20000);
  out.hasVisibleButton = {};
  for (const [name, sels] of Object.entries(spec.buttons || {})) {
    out.hasVisibleButton[name] = sels.some(sel => queryAll(sel).some(el => visible(el) && !el.disabled));
  }
  return out;
}
"""


def _spec(
    fields: dict[str, S.Field],
    *,
    count_fields: tuple[str, ...] = (),
    buttons: dict[str, tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    return {
        "fields": {
            name: {"candidates": list(f.candidates), "attr": f.attr, "count": name in count_fields}
            for name, f in fields.items()
        },
        "buttons": {k: list(v) for k, v in (buttons or {}).items()},
    }


PRODUCT_SPEC = _spec(
    S.PRODUCT_PAGE,
    buttons={
        "buy_now": S.PRODUCT_PAGE["buy_now"].candidates,
        "add_to_cart": S.PRODUCT_PAGE["add_to_cart"].candidates,
    },
)
CHECKOUT_SPEC = _spec(
    S.CHECKOUT_PAGE,
    count_fields=("line_items",),
    buttons={
        "place_order": S.CHECKOUT_PAGE["place_order"].candidates,
        # Visibility check (not mere DOM presence): Amazon's payment widget keeps hidden
        # inputs around on every page; only a *shown* card input means "human needed".
        "payment_input": S.CHECKOUT_PAGE["payment_input"].candidates,
    },
)


def _val(raw: dict[str, Any], name: str) -> str | None:
    field = raw.get("fields", {}).get(name) or {}
    value = field.get("value")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _seller_id_from_href(href: str | None) -> str | None:
    if not href:
        return None
    try:
        qs = parse_qs(urlparse(href).query)
    except ValueError:
        return None
    values = qs.get("seller")
    return values[0] if values else None


def _clean_prefix(value: str | None, *prefixes: str) -> str | None:
    if not value:
        return None
    v = value.strip()
    low = v.lower()
    for p in prefixes:
        if low.startswith(p):
            v = v[len(p) :].strip(" :")
            break
    return v or None


def parse_offer(raw: dict[str, Any]) -> OfferSnapshot:
    url = raw.get("url") or ""
    host = urlparse(url).hostname or ""
    price_text = _val(raw, "price")
    price = parse_money(price_text)
    currency = detect_currency(price_text)
    if currency == "CAD" and host != S.PRODUCT_URL_HOST:
        currency = None  # "$" only means CAD on amazon.ca
    availability = _val(raw, "availability")
    avail_norm = normalize_text(availability)
    unavailable = any(bad in avail_norm for bad in ("unavailable", "out of stock", "not available"))
    in_stock = bool(avail_norm) and "in stock" in avail_norm and not unavailable
    # Live 2026-09-25: "This item will be released on October 29, 2026. Pre-order now."
    is_preorder = (
        bool(avail_norm)
        and not unavailable
        and any(k in avail_norm for k in ("pre order", "preorder", "will be released"))
    )
    seller = _clean_prefix(_val(raw, "seller"), "sold by")
    fulfiller = _clean_prefix(_val(raw, "fulfiller"), "ships from")
    buybox_text = normalize_text(_val(raw, "buybox"))
    renewed = _val(raw, "renewed_caption")
    used_present = bool(raw.get("present", {}).get("used_buybox"))
    condition: str | None
    if used_present or renewed or any(w in buybox_text for w in ("renewed", "refurbished", " used ")):
        condition = "used"
    elif buybox_text and (in_stock or is_preorder):
        condition = "new"
    else:
        condition = None
    # Keep everything the extractor saw (which selector matched, what was present, which
    # buttons were visible) except the large body text; it is what makes a live miss fixable.
    raw_kept = {
        "url": url,
        "price_text": price_text,
        "quantity": _val(raw, "quantity"),
        "fields": raw.get("fields", {}),
        "present": raw.get("present", {}),
        "hasVisibleButton": raw.get("hasVisibleButton", {}),
    }
    twister_present = bool(raw.get("present", {}).get("twister"))
    return OfferSnapshot(
        asin=(_val(raw, "asin") or "").upper() or None,
        title=_val(raw, "title"),
        price=price,
        currency=currency,
        seller=seller,
        seller_id=_seller_id_from_href(_val(raw, "seller_link")),
        fulfiller=fulfiller,
        condition=condition,
        availability=availability,
        in_stock=in_stock,
        has_variant_selector=twister_present,
        buy_now_available=bool(raw.get("hasVisibleButton", {}).get("buy_now")),
        add_to_cart_available=bool(raw.get("hasVisibleButton", {}).get("add_to_cart")),
        raw=raw_kept,
        is_preorder=is_preorder,
    )


_QTY_RE = re.compile(r"(?:qty|quantity)\s*:?\s*(\d+)", re.I)
_CONDITION_WORDS = ("new", "used", "renewed", "refurbished", "open box", "collectible", "acceptable")


def _looks_like_condition(text: str) -> bool:
    """A real condition value is a short label containing a condition word, not prose."""
    low = normalize_text(text)
    return 0 < len(low) <= 40 and any(w in low for w in _CONDITION_WORDS)


def _parse_quantity(text: str | None) -> int | None:
    if not text:
        return None
    m = _QTY_RE.search(text)
    if m:
        return int(m.group(1))
    if text.strip().isdigit():
        return int(text.strip())
    return None


def parse_checkout(raw: dict[str, Any], *, product_condition: str | None) -> CheckoutSnapshot:
    url = raw.get("url") or ""
    host = urlparse(url).hostname or ""
    item_price_text = _val(raw, "item_price")
    total_text = _val(raw, "total")
    currency = detect_currency(total_text) or detect_currency(item_price_text)
    if currency == "CAD" and host != S.PRODUCT_URL_HOST:
        currency = None
    counts = raw.get("counts", {})
    line_items = counts.get("line_items")
    line_item_count = int(line_items) if isinstance(line_items, int) and line_items > 0 else None
    condition = _clean_prefix(_val(raw, "condition"), "condition")
    if condition is not None and not _looks_like_condition(condition):
        # A candidate matched some unrelated sentence (rehearsal 2026-09-25: the footer's
        # "Conditions of use"). Treat as "no condition row" rather than as a wrong condition.
        condition = None
    if condition is None and product_condition == "new":
        condition = "new"
    # Live review page: the ASIN is the *text* of a hidden data-testid span; legacy layouts
    # carried it as a data-asin attribute instead. Either is accepted, text first.
    asin_text = _val(raw, "item_asin") or _val(raw, "item_asin_attr") or ""
    return CheckoutSnapshot(
        asin=asin_text.upper() or None,
        title=_val(raw, "item_title"),
        seller=_clean_prefix(_val(raw, "seller"), "sold by"),
        fulfiller=_clean_prefix(_val(raw, "fulfiller"), "ships from"),
        condition=condition.lower() if condition else None,
        quantity=_parse_quantity(_val(raw, "quantity")),
        currency=currency,
        item_price=parse_money(item_price_text),
        total=parse_money(total_text),
        address_text=_val(raw, "address"),
        payment_text=_val(raw, "payment"),
        line_item_count=line_item_count,
        place_order_available=bool(raw.get("hasVisibleButton", {}).get("place_order")),
        payment_input_required=bool(raw.get("hasVisibleButton", {}).get("payment_input")),
        # Same retention as parse_offer: which selector matched and what was present is what
        # makes an unverified review-page selector fixable after a live miss.
        raw={
            "url": url,
            "item_price_text": item_price_text,
            "total_text": total_text,
            "fields": raw.get("fields", {}),
            "present": raw.get("present", {}),
            "counts": counts,
            "hasVisibleButton": raw.get("hasVisibleButton", {}),
        },
    )


def detect_challenge_from_raw(raw: dict[str, Any]) -> str | None:
    """Return a ChallengeKind value string if the page is a challenge/interstitial."""
    url = (raw.get("url") or "").lower()
    body = raw.get("bodyTextLower") or ""
    present = raw.get("present", {})
    if any(m in url for m in S.CHALLENGES["login_url_markers"]) or present.get("login"):
        return "login_required"
    if any(m in url for m in S.CHALLENGES["mfa_url_markers"]) or present.get("mfa"):
        return "mfa"
    if present.get("captcha") or any(t in body[:4000] for t in S.CHALLENGES["captcha_text"]):
        return "captcha"
    if any(t in body[:4000] for t in S.CHALLENGES["access_denied_text"]):
        return "access_denied"
    if any(t in body for t in S.CHALLENGES["payment_challenge_text"]):
        return "payment_challenge"
    nav = normalize_text(_val(raw, "account_nav"))
    if nav and any(normalize_text(t) in nav for t in S.CHALLENGES["signed_out_nav_text"]):
        return "login_required"
    return None


CHALLENGE_SPEC = _spec(
    {
        "captcha": S.Field("captcha", S.CHALLENGES["captcha_selectors"], "VERIFIED"),
        "login": S.Field("login", S.CHALLENGES["login_selectors"], "VERIFIED"),
        "mfa": S.Field("mfa", S.CHALLENGES["mfa_selectors"], "ASSUMED"),
        "account_nav": S.PRODUCT_PAGE["account_nav"],
    }
)


def find_order_id(text: str | None) -> str | None:
    if not text:
        return None
    m = re.search(S.ORDER_ID_PATTERN, text)
    return m.group(0) if m else None
