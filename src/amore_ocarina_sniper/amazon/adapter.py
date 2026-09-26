"""Playwright-backed Amazon.ca adapter using a normal authenticated browser checkout.

No purchasing API, no private endpoints, no replayed requests. The adapter drives the
same pages a human would, reads what they show, and hands snapshots to the policy layer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Protocol

from ..config import AppConfig
from ..models import ChallengeDetected, ChallengeKind, CheckoutSnapshot, OfferSnapshot
from . import selectors as S
from .extract import (
    CHALLENGE_SPEC,
    CHECKOUT_SPEC,
    EXTRACT_JS,
    PRODUCT_SPEC,
    detect_challenge_from_raw,
    find_order_id,
    parse_checkout,
    parse_offer,
)

log = logging.getLogger("ocarina.amazon")

# Serialises the current DOM with scripts removed, hidden-input values blanked (checkout
# tokens, CSRF) and long digit runs masked (card / order numbers). Same redaction the
# operator's manual capture used; the output is for selector verification only.
_REDACTED_HTML_JS = """() => {
  const c = document.documentElement.cloneNode(true);
  c.querySelectorAll('script,style,link,svg,img,noscript,iframe').forEach(e => e.remove());
  c.querySelectorAll('input[type=hidden]').forEach(e => e.setAttribute('value', '[HIDDEN]'));
  return c.outerHTML.replace(/\\b\\d{12,19}\\b/g, '[NUM]');
}"""

# True when any of the selectors matches a *rendered* element (layout box present).
_ANY_VISIBLE_JS = """(sels) => sels.some((sel) => {
  let els; try { els = document.querySelectorAll(sel); } catch (e) { return false; }
  for (const el of els) {
    if (el.offsetParent !== null || el.getClientRects().length) return true;
  }
  return false;
})"""


class DryRunRefusal(RuntimeError):
    """Defence in depth: the adapter refuses to click Place Order while in dry-run."""


class PurchaseAdapter(Protocol):
    dry_run: bool

    async def ensure_ready(self) -> None: ...
    async def verify_offer(self) -> OfferSnapshot: ...
    async def poll_offer(self) -> OfferSnapshot: ...
    async def prepare_checkout(self, offer: OfferSnapshot) -> CheckoutSnapshot: ...
    async def submit_order(self) -> None: ...
    async def confirm_order(self) -> str | None: ...
    async def abandon(self) -> None: ...
    async def capture_artifact(self, label: str, *, html: bool = False) -> str | None: ...


_HEAVY_TYPES = {"image", "font", "media"}
MAX_CART_TIDY_PASSES = 6  # one decrement per pass -> handles a leftover quantity up to 6


async def _abort_heavy_assets(route) -> None:
    if route.request.resource_type in _HEAVY_TYPES:
        await route.abort()
    else:
        await route.continue_()


class AmazonAdapter:
    def __init__(self, config: AppConfig, *, dry_run: bool = True, headless: bool = False) -> None:
        self.config = config
        self.dry_run = dry_run
        self.headless = headless
        self._pw = None
        self._context = None
        self._page = None
        self._watch_page = None  # second tab used only by the product watcher
        self._checkout_target = None  # Page or Frame holding the checkout UI
        self._last_offer_condition: str | None = None
        self._used_cart_path = False
        self._cart_row_selector = S.CART_PAGE["rows"][-1]
        # Rehearsal only (`ocarina doctor --checkout-probe`): when set, a redacted copy of each
        # page the checkout path passes through is written here so selectors can be verified
        # offline. Never set by the runner; off the critical path.
        self.probe_dir: Path | None = None

    async def _probe_dump(self, name: str) -> None:
        if self.probe_dir is None:
            return
        try:
            html = await self.page.evaluate(_REDACTED_HTML_JS)
            self.probe_dir.mkdir(parents=True, exist_ok=True)
            (self.probe_dir / f"{name}.html").write_text(html, encoding="utf-8")
            log.info("probe: saved %s (%d chars) url=%s", name, len(html), self.page.url)
        except Exception as exc:  # noqa: BLE001
            log.warning("probe: could not save %s: %s", name, exc)

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        from playwright.async_api import async_playwright

        profile = self.config.paths.browser_profile_dir
        profile.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        self._context = await self._pw.chromium.launch_persistent_context(
            str(profile),
            headless=self.headless,
            locale="en-CA",
            timezone_id="America/Toronto",
            viewport={"width": 1366, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        self._context.set_default_timeout(self.config.checkout.element_timeout_ms)
        self._context.set_default_navigation_timeout(self.config.checkout.navigation_timeout_ms)
        if self.config.checkout.block_heavy_assets:
            await self._context.route("**/*", _abort_heavy_assets)
        pages = self._context.pages
        self._page = pages[0] if pages else await self._context.new_page()

    async def stop(self) -> None:
        try:
            if self._context:
                await self._context.close()
        finally:
            if self._pw:
                await self._pw.stop()
            self._context = None
            self._pw = None
            self._page = None
            self._watch_page = None

    @property
    def page(self):
        if self._page is None:
            raise RuntimeError("adapter not started")
        return self._page

    # -------------------------------------------------------------- helpers
    async def _extract(self, target, spec: dict[str, Any]) -> dict[str, Any]:
        return await target.evaluate(EXTRACT_JS, spec)

    async def _raise_if_challenge(self, target) -> None:
        raw = await self._extract(target, CHALLENGE_SPEC)
        kind = detect_challenge_from_raw(raw)
        if kind:
            raise ChallengeDetected(ChallengeKind(kind), raw.get("url", ""))

    async def capture_artifact(self, label: str, *, html: bool = False) -> str | None:
        """Screenshot (and optionally DOM of every frame) for operator review.

        Never on the critical path: called only after an attempt has already been
        rejected or has raised. The HTML dump exists so a selector that failed on a
        live page can be fixed offline; it stays under data_dir/artifacts (gitignored).
        """
        try:
            out_dir = self.config.paths.artifacts_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            stem = out_dir / f"{int(time.time())}-{label}"
            await self.page.screenshot(path=f"{stem}.png", full_page=True)
            if html:
                parts = []
                for frame in self.page.frames:
                    try:
                        content = await frame.content()
                    except Exception as exc:  # noqa: BLE001 - cross-origin/detached frames
                        content = f"<!-- frame content unavailable: {exc!r} -->"
                    parts.append(f"<!-- ===== frame url={frame.url} name={frame.name!r} ===== -->\n{content}")
                await asyncio.to_thread(
                    Path(f"{stem}.html").write_text, "\n\n".join(parts), encoding="utf-8"
                )
            return f"{stem}.png"
        except Exception as exc:  # pragma: no cover
            log.warning("artifact capture failed: %s", exc)
            return None

    # -------------------------------------------------------------- product
    async def ensure_ready(self) -> None:
        """Keep the product tab loaded so a trigger only needs one fresh navigation."""
        if self.page.url != self.config.target.url:
            await self.page.goto(self.config.target.url, wait_until="domcontentloaded")

    async def poll_offer(self) -> OfferSnapshot:
        """Read the product page on a dedicated second tab (same signed-in session).

        Used by the watcher only. Keeping it off the main tab means a Discord trigger can
        start ``verify_offer`` at any moment without racing a poll's navigation.
        """
        if self._watch_page is None or self._watch_page.is_closed():
            if self._context is None:
                raise RuntimeError("adapter not started")
            self._watch_page = await self._context.new_page()
        return await self.verify_offer(page=self._watch_page)

    async def verify_offer(self, page=None) -> OfferSnapshot:
        cfg = self.config
        page = page or self.page
        await page.goto(cfg.target.url, wait_until="domcontentloaded")
        readiness = ", ".join(
            [
                *S.PRODUCT_PAGE["asin"].candidates,
                *S.PRODUCT_PAGE["title"].candidates,
                *S.CHALLENGES["captcha_selectors"],
                *S.CHALLENGES["login_selectors"],
            ]
        )
        try:
            await page.wait_for_selector(readiness, state="attached", timeout=cfg.checkout.element_timeout_ms)
        except Exception as exc:
            await self._raise_if_challenge(page)
            raise ChallengeDetected(ChallengeKind.UNKNOWN_PAGE, "product page did not render") from exc
        await self._raise_if_challenge(page)
        raw = await self._extract(page, PRODUCT_SPEC)
        offer = parse_offer(raw)
        if page is self._page:
            self._last_offer_condition = offer.condition
        return offer

    # ------------------------------------------------------------- checkout
    async def prepare_checkout(self, offer: OfferSnapshot) -> CheckoutSnapshot:
        cfg = self.config
        strategy = cfg.checkout.strategy
        self._used_cart_path = False
        if strategy == "buy_now" and offer.buy_now_available:
            await self._click_first(self.page, S.PRODUCT_PAGE["buy_now"].candidates)
        elif offer.add_to_cart_available:
            # Pre-orders expose only "Pre-order now" (the add-to-cart slot): this is the main
            # path for the target listing, not a fallback.
            self._used_cart_path = True
            units = await self._add_to_cart_confirmed()
            await self._probe_dump("after-add-to-cart")
            if units == 1 and await self._proceed_from_confirmation():
                log.info("cart path: fast checkout from the add-to-cart confirmation (units=1)")
            else:
                log.info("cart path: via cart page (units=%s)", units)
                await self.page.goto(S.CART_URL, wait_until="domcontentloaded")
                await self._probe_dump("cart-page")
                await self._tidy_cart(cfg.target.asin)
                await self._click_first(self.page, S.CART_PAGE["proceed"])
        else:
            raise ChallengeDetected(ChallengeKind.UNKNOWN_PAGE, "no purchase control available")

        target = await self._wait_for_checkout_surface()
        self._checkout_target = target
        await self._raise_if_challenge(target)
        await self._probe_dump("review-page")
        raw = await self._extract(target, CHECKOUT_SPEC)
        return parse_checkout(raw, product_condition=self._last_offer_condition)

    # ------------------------------------------------------------ cart path
    async def _cart_units(self) -> int | None:
        """Units in the cart: the cart page's own attribute when present, else the nav badge."""
        try:
            text = await self.page.evaluate(
                """(spec) => {
                  const cart = document.querySelector(spec.cart);
                  if (cart) return cart.getAttribute('data-cart-total-item-count');
                  const el = document.querySelector(spec.badge); return el ? el.innerText : null;
                }""",
                {"cart": S.CART_TOTAL_ITEM_COUNT, "badge": S.NAV_CART_COUNT},
            )
        except Exception:
            return None
        digits = "".join(ch for ch in (text or "") if ch.isdigit())
        return int(digits) if digits else None

    async def _add_to_cart_confirmed(self) -> int | None:
        """Click add-to-cart and wait until Amazon acknowledges it. Returns cart units.

        Rehearsal 2026-09-25: the desktop button (class attach-dss-atc) adds via AJAX and slides
        in `#attach-desktop-sideSheet` — on this account a *warranty upsell* pane, not an
        "added to cart" message — while the nav badge is progressively loaded and may not be
        readable before the click. Three independent signals are therefore watched: the
        add-to-cart network response, a *visible* confirmation surface, and a badge increase.
        """
        cfg = self.config
        page = self.page
        before = await self._cart_units()
        acknowledged = asyncio.Event()
        seen_paths: list[str] = []

        def on_response(resp) -> None:
            path = resp.url.split("?", 1)[0].lower()
            if self.probe_dir is not None and resp.request.method == "POST":
                seen_paths.append(f"{resp.status} {path}")  # rehearsal: learn the AJAX endpoint
            if resp.request.method == "POST" and any(m in path for m in S.ADD_TO_CART_RESPONSE_MARKERS):
                if resp.status < 400:
                    acknowledged.set()

        page.on("response", on_response)
        t0 = time.monotonic()
        how = "timeout"
        try:
            await self._click_first(page, S.PRODUCT_PAGE["add_to_cart"].candidates)
            deadline = time.monotonic() + cfg.checkout.element_timeout_ms / 1000
            while time.monotonic() < deadline:
                url = page.url.lower()
                if "/cart/" in url or "/huc/" in url:
                    how = "navigation"
                    break
                if acknowledged.is_set():
                    how = "network"
                    break
                try:
                    if await page.evaluate(_ANY_VISIBLE_JS, list(S.ADD_TO_CART_CONFIRMATION)):
                        how = "surface"
                        break
                except Exception:
                    pass  # mid-navigation
                units = await self._cart_units()
                if units is not None and units > (before or 0):
                    how = "badge"
                    break
                await asyncio.sleep(0.05)
        finally:
            page.remove_listener("response", on_response)
        if how == "timeout":
            log.warning("add to cart: no confirmation observed within timeout; checking the cart page")
        else:
            log.info("add to cart acknowledged via %s in %.2fs", how, time.monotonic() - t0)
        if seen_paths:
            log.info("probe: add-to-cart responses: %s", seen_paths)
        await asyncio.sleep(0.15)  # let the badge settle
        return await self._cart_units()

    async def _proceed_from_confirmation(self) -> bool:
        """Fast path: the confirmation surface offers Proceed to checkout. Caller guarantees
        the cart holds exactly one unit, so this checkout can only contain our item."""
        for sel in S.CONFIRMATION_PROCEED:
            loc = self.page.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=self.config.checkout.element_timeout_ms)
                    return True
            except Exception:
                continue
        return False

    async def _read_cart_rows(self) -> list[dict[str, Any]] | None:
        """[{asin, qty, checkbox, checked, index}] for active-cart rows; None if unreadable.

        Also remembers which row selector matched so locators index the same node list."""
        result = await self.page.evaluate(
            """(spec) => {
              let rows = [], matched = null;
              for (const sel of spec.rows) {
                rows = Array.from(document.querySelectorAll(sel));
                if (rows.length) { matched = sel; break; }
              }
              const num = (s) => { const m = (s || '').match(/\\d+/); return m ? parseInt(m[0], 10) : null; };
              return { matched, rows: rows.map((row, i) => {
                let qty = num(row.getAttribute(spec.qtyAttr));
                if (qty === null) {
                  const f = row.querySelector(spec.qtyStepper);
                  if (f) qty = num(f.dataset.steppervalue);
                }
                if (qty === null) {
                  const l = row.querySelector(spec.qtyLabel);
                  if (l) qty = num(l.getAttribute('aria-label') || l.textContent);
                }
                if (qty === null) { const s = row.querySelector(spec.qtySelect); if (s) qty = num(s.value); }
                let cb = null;
                for (const sel of spec.checkbox) { cb = row.querySelector(sel); if (cb) break; }
                let checked = cb ? !!cb.checked : null;
                const sel = row.getAttribute(spec.selectedAttr);
                if (checked === null && sel !== null) checked = sel === '1' || sel === 'true';
                return { asin: (row.dataset.asin || '').toUpperCase(), qty, checkbox: !!cb || sel !== null,
                         checked, index: i };
              }) };
            }""",
            {
                "rows": list(S.CART_PAGE["rows"]),
                "qtyAttr": S.CART_PAGE["row_quantity_attr"],
                "qtyStepper": S.CART_PAGE["row_quantity_stepper"],
                "qtyLabel": S.CART_PAGE["row_quantity_label"],
                "qtySelect": S.CART_PAGE["row_quantity_select"],
                "selectedAttr": S.CART_PAGE["row_selected_attr"],
                "checkbox": list(S.CART_PAGE["row_checkbox"]),
            },
        )
        self._cart_row_selector = result.get("matched") or S.CART_PAGE["rows"][-1]
        return result.get("rows") or None

    def _row_locator(self, index: int):
        return self.page.locator(self._cart_row_selector).nth(index)

    async def _click_in_row(self, row, candidates) -> bool:
        for sel in candidates:
            loc = row.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=2000)
                    return True
            except Exception:
                continue
        return False

    async def _tidy_cart(self, target_asin: str) -> None:
        """Make the active cart exactly [target × 1] before Proceed to checkout.

        Other items are unticked (when the cart has per-item checkout checkboxes), else moved
        to Save for later, else deleted. Target quantity above 1 is stepped down. Bounded by
        element_timeout; the review-page policy check remains the real gate afterwards.
        """
        cfg = self.config
        deadline = time.monotonic() + cfg.checkout.element_timeout_ms / 1000
        passes = 0
        while True:
            rows = await self._read_cart_rows()
            if rows is None:
                units = await self._cart_units()
                if units == 0:
                    raise ChallengeDetected(ChallengeKind.UNKNOWN_PAGE, "cart is empty after add to cart")
                log.warning("cart rows unreadable (units=%s); relying on the review-page check", units)
                return
            target_rows = [r for r in rows if r["asin"] == target_asin]
            if not target_rows:
                if passes == 0:
                    # The add is acknowledged asynchronously; give the cart one short re-read
                    # before failing closed.
                    passes += 1
                    await asyncio.sleep(0.6)
                    await self.page.reload(wait_until="domcontentloaded")
                    continue
                raise ChallengeDetected(
                    ChallengeKind.UNKNOWN_PAGE, "target item not in cart after add to cart"
                )
            others = [r for r in rows if r["asin"] != target_asin]
            has_checkboxes = all(r["checkbox"] for r in rows)
            active_others = [r for r in others if not (has_checkboxes and r["checked"] is False)]
            target = target_rows[0]
            target_ok = (target["qty"] in (1, None)) and (not has_checkboxes or target["checked"])
            if not active_others and target_ok and len(target_rows) == 1:
                if passes:
                    log.info("cart tidied in %d pass(es): only %s x1 remains active", passes, target_asin)
                return
            if time.monotonic() > deadline or passes >= MAX_CART_TIDY_PASSES:
                raise ChallengeDetected(
                    ChallengeKind.UNKNOWN_PAGE,
                    f"cart could not be reduced to the single target item (rows={rows})",
                )
            passes += 1
            log.info("cart needs tidying (pass %d): %s", passes, rows)
            # Work from the bottom so removed rows do not shift the indexes of rows still to do.
            for row in sorted(active_others, key=lambda r: -r["index"]):
                loc = self._row_locator(row["index"])
                if has_checkboxes and row["checked"]:
                    if await self._click_in_row(loc, S.CART_PAGE["row_checkbox"]):
                        continue
                if not await self._click_in_row(loc, S.CART_PAGE["row_save_for_later"]):
                    await self._click_in_row(loc, S.CART_PAGE["row_delete"])
            if has_checkboxes and target["checked"] is False:
                await self._click_in_row(self._row_locator(target["index"]), S.CART_PAGE["row_checkbox"])
            if target["qty"] and target["qty"] > 1:
                loc = self._row_locator(target["index"])
                sel = loc.locator(S.CART_PAGE["row_quantity_select"]).first
                if await sel.count():
                    await sel.select_option("1")
                else:
                    # ONE decrement per pass, then re-read: on the live stepper the same button
                    # turns into "Delete" once the quantity reaches 1, so a burst could remove
                    # the target. The decrement selectors additionally require the
                    # "Decrease quantity" label, so a stale click cannot delete either.
                    await self._click_in_row(loc, S.CART_PAGE["row_quantity_decrement"])
            await asyncio.sleep(0.4)  # optimistic DOM updates settle

    async def _wait_for_checkout_surface(self):
        """Wait (bounded) for either a checkout navigation or the turbo-checkout iframe."""
        cfg = self.config
        deadline = time.monotonic() + cfg.checkout.navigation_timeout_ms / 1000
        iframe_sel = ", ".join(S.TURBO_IFRAME)
        place_sel = ", ".join(S.CHECKOUT_PAGE["place_order"].candidates)
        while time.monotonic() < deadline:
            url = self.page.url.lower()
            if "/buy/" in url or "/checkout" in url:
                try:
                    await self.page.wait_for_selector(
                        place_sel, state="attached", timeout=cfg.checkout.element_timeout_ms
                    )
                except Exception:
                    await self._raise_if_challenge(self.page)
                return self.page
            frame_el = await self.page.query_selector(iframe_sel)
            if frame_el:
                frame = await frame_el.content_frame()
                if frame:
                    try:
                        await frame.wait_for_selector(
                            place_sel, state="attached", timeout=cfg.checkout.element_timeout_ms
                        )
                    except Exception:
                        pass
                    return frame
            await self._raise_if_challenge(self.page)
            await asyncio.sleep(0.05)
        raise ChallengeDetected(ChallengeKind.UNKNOWN_PAGE, "checkout surface did not appear")

    async def _click_first(self, target, candidates) -> None:
        for sel in candidates:
            loc = target.locator(sel).first
            try:
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=self.config.checkout.element_timeout_ms)
                    return
            except Exception:
                continue
        raise ChallengeDetected(ChallengeKind.UNKNOWN_PAGE, f"none clickable: {candidates}")

    # ---------------------------------------------------------------- submit
    async def submit_order(self) -> None:
        if self.dry_run:
            raise DryRunRefusal("dry-run adapter refuses to submit")
        target = self._checkout_target or self.page
        await self._click_first(target, S.CHECKOUT_PAGE["place_order"].candidates)

    async def confirm_order(self) -> str | None:
        """Return an order id when confirmation is observed; None when ambiguous."""
        cfg = self.config
        deadline = time.monotonic() + cfg.checkout.confirmation_timeout_ms / 1000
        while time.monotonic() < deadline:
            url = self.page.url.lower()
            body = ""
            try:
                body = (await self.page.evaluate("document.body ? document.body.innerText : ''")) or ""
            except Exception:
                await asyncio.sleep(0.1)
                continue
            low = body.lower()
            url_hit = any(m in url for m in S.CONFIRMATION["url_markers"])
            text_hit = any(m in low for m in S.CONFIRMATION["text_markers"])
            order_id = find_order_id(body)
            if (url_hit or text_hit) and order_id:
                return order_id
            if url_hit or text_hit:
                # Confirmation page without a readable order id: keep polling briefly.
                await asyncio.sleep(0.2)
                continue
            raw = await self._extract(self.page, CHALLENGE_SPEC)
            kind = detect_challenge_from_raw(raw)
            if kind:
                raise ChallengeDetected(ChallengeKind(kind), "after submission click")
            await asyncio.sleep(0.2)
        return None

    async def abandon(self) -> None:
        """Leave any unsubmitted checkout and return to the product tab. Never touches cart."""
        self._checkout_target = None
        if self._used_cart_path:
            self._used_cart_path = False
            log.warning(
                "abandon: the cart path was used; the item most likely remains in your Amazon cart. "
                "The next attempt's cart step will reduce the quantity to 1, but emptying the cart "
                "by hand is cleaner and faster."
            )
        try:
            await self.page.goto(self.config.target.url, wait_until="domcontentloaded")
        except Exception as exc:  # pragma: no cover
            log.warning("abandon navigation failed: %s", exc)

    # ---------------------------------------------------------------- setup
    async def interactive_login(self, timeout_s: int = 900) -> bool:
        """Open Amazon.ca and wait for the operator to sign in (MFA included). No automation."""
        await self.page.goto("https://www.amazon.ca/", wait_until="domcontentloaded")
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            try:
                raw = await self._extract(self.page, CHALLENGE_SPEC)
                if detect_challenge_from_raw(raw) is None and "amazon.ca" in self.page.url:
                    nav = (raw.get("fields", {}).get("account_nav") or {}).get("value") or ""
                    if nav and "sign in" not in nav.lower():
                        return True
            except Exception:
                pass
            await asyncio.sleep(1.0)
        return False

    async def session_status(self) -> dict[str, Any]:
        await self.page.goto(self.config.target.url, wait_until="domcontentloaded")
        raw = await self._extract(self.page, CHALLENGE_SPEC)
        challenge = detect_challenge_from_raw(raw)
        return {"url": raw.get("url"), "challenge": challenge, "signed_in": challenge is None}

    async def recent_orders_text(self) -> str:
        await self.page.goto(S.ORDER_HISTORY_URL, wait_until="domcontentloaded")
        await self._raise_if_challenge(self.page)
        return (await self.page.evaluate("document.body ? document.body.innerText : ''")) or ""


def profile_exists(config: AppConfig) -> bool:
    p: Path = config.paths.browser_profile_dir
    return p.exists() and any(p.iterdir())
