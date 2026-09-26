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
        if strategy == "buy_now" and offer.buy_now_available:
            await self._click_first(self.page, S.PRODUCT_PAGE["buy_now"].candidates)
        elif offer.add_to_cart_available:
            await self._click_first(self.page, S.PRODUCT_PAGE["add_to_cart"].candidates)
            await self.page.goto("https://www.amazon.ca/gp/cart/view.html", wait_until="domcontentloaded")
            await self._click_first(
                self.page,
                ("input[name='proceedToRetailCheckout']", "#sc-buy-box-ptc-button input"),
            )
        else:
            raise ChallengeDetected(ChallengeKind.UNKNOWN_PAGE, "no purchase control available")

        target = await self._wait_for_checkout_surface()
        self._checkout_target = target
        await self._raise_if_challenge(target)
        raw = await self._extract(target, CHECKOUT_SPEC)
        return parse_checkout(raw, product_condition=self._last_offer_condition)

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
