# AGENTS.md — AmoreOcarinaSniper

Working notes for humans and coding agents. The **Milestones** and **Task board** sections are
living documents: update them in the same commit as the work they describe.

## Start here (state of play — keep this block current)

- **Production is running.** A LIVE runner (`ocarina run --live`) is normally up in a separate
  pwsh window titled *ocarina LIVE runner*, ARMED, with the product watcher polling every 15 s.
  It holds the Playwright profile lock: anything that opens `profiles/amazon` (`doctor --browser`,
  `--checkout-probe`, `setup`) requires stopping it first and re-arming after. Use the runbook
  below; do not improvise the restart.
- **Target listing**: Amazon.ca `B0HJ6F8L6V`, pre-order only (no Buy Now ever); the cart path is
  the primary checkout path. Price 709.99 CAD, seller `Amazon.ca`. Policy allows pre-orders.
- **What is live-verified vs assumed** is the table in `README.md` → *What is verified and what
  is not*. Everything on product page, add-to-cart side sheet, cart page, review page and order
  history is VERIFIED against the operator's real signed-in pages. Still ASSUMED: thank-you page
  markers and order-id location (only observable on a real order), side-sheet *Proceed* button,
  MFA selectors. Do not "fix" a VERIFIED selector from memory of how Amazon usually looks.
- **Every step up to the click has been rehearsed end-to-end in the bot's own profile**
  (`ocarina doctor --browser --checkout-probe --probe-asin <stand-in>`, dry-run): 3.0 s from
  add-to-cart to a fully parsed review page. Re-run this after any adapter/selector change.
- **Test suite**: 238 tests, ~95 s; `-m "not browser"` runs in ~8 s. Both must be green plus
  `ruff check src tests` before a commit. Fixtures in `tests/fixtures/amazon/` mirror the live
  markup; if a live page differs from a fixture, the fixture is what needs to change.
- Detailed history is in *Task board* and *Decision log* below; the file-scoped rules in
  `.cursor/rules/` carry the per-module traps.

## Regression traps (deliberate decisions that look like bugs — do not "fix")

| Looks wrong | Why it is right | Where |
|---|---|---|
| `"sorry! something went wrong"` is *not* an access-denied marker | Amazon's overload page under a rush; parking the bot on it loses the drop. It is `server_error` → transient. Only *Access Denied* / *automated access* stops the bot. | `selectors.CHALLENGES`, `ChallengeKind.transient` |
| Pre-intent failures are retried (≤ 2) | Hard rule 5 forbids retries **after** the intent only. Retries re-run full policy each pass. | `coordinator._run_attempt` |
| Watcher keeps polling during its cooldown | Cooldown is bound to the *refused offer's signature*; a changed offer must trigger at once. | `watch.tick` |
| Watcher back-off on timeouts is capped at 2× | Exponential back-off is for CAPTCHA/login only; timeouts happen exactly when polling matters. | `watch._delay` |
| `verify_offer(reuse_within_s=10)` skips a navigation | The watcher promoted its freshly loaded tab / `abandon()` just reloaded the page; older pages reload anyway. | `adapter.verify_offer`, `promote_watch_page` |
| Evidence capture runs *before* `abandon()` | `abandon()` navigates away; a screenshot after it shows the product page, not the failure. | `coordinator._run_attempt` |
| Place-order wait uses `state="visible"` and raises `unknown_page` | The live page has 4 **disabled** blocker copies of the button; a snapshot without an enabled one can only be refused, which would start the watcher cooldown mid-drop. | `adapter._wait_for_checkout_surface` |
| No `.a-size-small:has-text('Condition')` selector | It matched the footer *Conditions of use* → `condition != new` → would have refused the real order. `_looks_like_condition` guards the field. | `selectors.CHECKOUT_PAGE`, `extract.parse_checkout` |
| Add-to-cart confirmation watches three signals | The side sheet on this account is a **warranty upsell** with no "added" text; the nav badge loads progressively; the POST to `/cart/add-to-cart` is the reliable one. | `adapter._add_to_cart_confirmed` |
| Cart stepper decrement is one click per pass | At qty 1 the same button becomes **Delete**. | `adapter._tidy_cart` |
| ASIN on the review page is read as *text* | `span[data-testid^='Item_asin_']`; there is no `data-asin` attribute anywhere on the live page. | `selectors.CHECKOUT_PAGE["item_asin"]` |
| `fulfiller_ok` accepts an empty fulfiller for Amazon-as-seller | The Amazon-sold pre-order renders no *Ships from* row at all. | `policy.fulfiller_ok` |
| `payment_matches` accepts a bare last-4 | Live text is *Paying with Visa 4105*, not *ending in 4105*. | `policy.payment_matches` |
| `checkout.block_heavy_assets = false` | Benchmarked: no gain (verify stops at `domcontentloaded`). | README M5 |
| `max_item_price` ships as `"0.00"` in the example | `arm` must refuse until the operator sets a limit deliberately. | `config.example.toml` |
| Sender user id is empty for the alert author | The alert app uses a default avatar → no user id in the DOM; a configured id fails closed. | extension options |

## Runbook (exact commands, PowerShell, repo root)

```powershell
# Stop the LIVE runner (kills the pwsh/ocarina/python processes of `ocarina run`)
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'ocarina' -and $_.CommandLine -match '\brun\b' -and $_.Name -in @('pwsh.exe','ocarina.exe','python.exe') } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep 3; Remove-Item runtime\ocarina.lock -ErrorAction SilentlyContinue

# Start it again LIVE in its own window, then re-arm (restarts always come up DISARMED)
Start-Process pwsh -ArgumentList '-NoExit','-Command','$host.UI.RawUI.WindowTitle = "ocarina LIVE runner"; Set-Location D:\Users\josua\workspace\AmoreOcarinaSniper; ocarina run --live'
Start-Sleep 12; ocarina arm --minutes 4320; ocarina status

# Health: last lines of the runner log / telemetry, and challenge count (should stay 0)
Get-Content runtime\logs\runner.log -Tail 5
Select-String -Path runtime\logs\telemetry.jsonl -Pattern 'watch_challenge|transient|watch_error' | Measure-Object

# Rehearsal in the bot's profile (runner must be stopped; ~3 min; dry-run; writes redacted dumps
# to runtime/artifacts/probe-<ts>/). Stand-in ASIN must have add-to-cart and be cheap.
ocarina doctor --browser --checkout-probe --probe-asin B0GJZ8WJD9

# Emergency stop that survives everything: creates the kill-switch file the coordinator checks
ocarina kill
```

PowerShell has no heredoc; use the file tools for edits, never `cat <<EOF`. Temp scripts go to
`$env:TEMP`, never into the repo. `config.toml` is git-ignored and holds the operator's real
address/payment fragments — never print it whole in a chat; grep the key you need.

## Gathering evidence from live pages (how every selector got verified)

Verification order of preference — cheapest and safest first:

1. **Artifacts the bot already wrote.** Any refusal with an offer present, any transient failure
   on its last pass and any challenge leaves `runtime/artifacts/<ts>-<label>.png` + `.html`
   (all frames) and a `telemetry.jsonl` event carrying the full `offer` / `snapshot` dict incl.
   `raw` (which candidate matched each field, what was present, counts). Read these before
   asking the operator for anything.
   ```powershell
   Select-String -Path runtime\logs\telemetry.jsonl -Pattern '"checkout_checked"|"offer_checked"' | Select-Object -Last 3
   Get-ChildItem runtime\artifacts | Sort-Object LastWriteTime | Select-Object -Last 10
   ```
2. **Rehearsal in the bot's own profile** (dry-run, never submits; runner stopped; ~3 min):
   `ocarina doctor --browser --checkout-probe --probe-asin <ASIN>` forces the cart strategy,
   substitutes the stand-in ASIN/URL into a copy of the config, runs the real
   add-to-cart → cart → review path, prints *offer read in Xs*, *review page reached in Xs* and
   the `matched selectors` dict, and writes redacted dumps `after-add-to-cart.html`,
   `cart-page.html`, `review-page.html` to `runtime/artifacts/probe-<ts>/`. Policy is reported,
   not bypassed (it refuses on ASIN/title/seller for the stand-in — expected). The stand-in
   stays in the operator's cart afterwards: tell them to remove it. Pick something cheap with
   *Add to Cart* (GameSir controller `B0GJZ8WJD9` was used); a stand-in with *Buy Now* is fine
   because the probe forces `strategy = "cart"`.
3. **Operator capture of a page the bot cannot reach** (a live drop review page, order history,
   a challenge). Ask them to paste this in the DevTools console on the page and save the
   result to `Downloads/<name>.html`; it is the same redaction the adapter uses (scripts, styles,
   images, iframes removed; hidden-input values → `[HIDDEN]`; 12–19 digit runs → `[NUM]`):
   ```js
   (() => { const c = document.documentElement.cloneNode(true);
     c.querySelectorAll('script,style,link,svg,img,noscript,iframe').forEach(e => e.remove());
     c.querySelectorAll('input[type=hidden]').forEach(e => e.setAttribute('value', '[HIDDEN]'));
     const html = c.outerHTML.replace(/\b\d{12,19}\b/g, '[NUM]');
     const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([html], {type: 'text/html'}));
     a.download = 'page.html'; a.click(); })()
   ```
   Copy the file into `runtime/artifacts/` (git-ignored) before analysing. Pasting a single
   element's `outerHTML` (DevTools → *Copy outerHTML*) is enough for one control.
4. **Never** ask for or store: cookies, request headers, HAR files, Postman/Fiddler captures,
   `anti-csrftoken-a2z` values, card/CVV fields, purchase ids in logs. Request replay was
   evaluated and rejected (Decision log, 2026-09-25 22:2x) — do not reopen it.

Analysing a dump offline — use the adapter's own extractor against the file, so `:not([disabled])`,
`^=`, `:has-text()` behave exactly as in production (script lives in `$env:TEMP`, not the repo;
no BeautifulSoup — plain `html.parser` is fine for ad-hoc attribute walks):

```python
# %TEMP%\probe.py  <dump.html>  -> which candidate matched each review-page field
import asyncio, json, sys
from pathlib import Path
from playwright.async_api import async_playwright
from amore_ocarina_sniper.amazon.extract import CHECKOUT_SPEC, EXTRACT_JS  # or PRODUCT_SPEC / CHALLENGE_SPEC

async def main(path):
    async with async_playwright() as pw:
        b = await pw.chromium.launch(); p = await b.new_page()
        await p.set_content(Path(path).read_text(encoding="utf-8"))
        raw = await p.evaluate(EXTRACT_JS, CHECKOUT_SPEC)
        for name, f in raw["fields"].items():
            print(f"{name:18} {f['selector'] or '-':60} {str(f['value'])[:60]!r}")
        print("present:", json.dumps(raw["present"]), "counts:", json.dumps(raw["counts"]))
        await b.close()
asyncio.run(main(sys.argv[1]))
```

After verifying: (a) reorder the Field's candidates so the verified one is first and mark it
`# VERIFIED <date>`; (b) rebuild the matching fixture in `tests/fixtures/amazon/` to mirror the
live markup (keep the `window.__checkoutFixture` / `window.__cartFixture` variant switches the
tests set via `Routing`); (c) make the test assert the *verified* candidate matched
(`snapshot.raw["fields"][name]["selector"]`); (d) update the README verification table;
(e) rerun the rehearsal if the adapter path changed.

## What this project is

A Chromium MV3 extension watches one open Discord channel for one configured stock alert and
POSTs a minimal trigger to a Python process on `127.0.0.1:48620`. Python verifies the Amazon.ca
listing (ASIN `B0HJ6F8L6V`) in a signed-in Playwright browser and, only when explicitly armed
and every policy check passes, submits a single order. See `README.md` for operator docs and
`CURSOR_PROMPT.md` for the original spec.

Layout:

- `extension/` — unpacked MV3 extension (`content/observer.js`, `content/selectors.js`,
  `shared/matcher.js`, `background.js`, popup, options).
- `src/amore_ocarina_sniper/` — `config`, `policy`, `models`, `store`, `lock`, `killswitch`,
  `telemetry`, `bridge`, `coordinator`, `watch`, `notify`, `app`, `cli`; `amazon/` holds `selectors.py`,
  `extract.py`, `adapter.py`.
- `tests/` — pytest suite; fixtures in `tests/fixtures/{discord,amazon}/`.
- `config.example.toml` — the only config that is committed.
- `.cursor/rules/*.mdc` — file-scoped agent rules (adapter, state machine, extension, tests).

## Hard rules (do not relax without an explicit user decision)

1. **No real orders in development or tests.** Tests run against local fixtures only.
2. **Fail closed.** Missing, ambiguous, or unreadable policy fields reject the attempt.
3. **The trigger path cannot arm, disarm, reset, or change policy.** Control is CLI → SQLite.
4. **Dry-run and DISARMED by default.** `--live` and `ocarina arm` are both required; restarts
   always require re-arming.
5. **Intent is persisted before the final click.** Ambiguity → `UNKNOWN`, never auto-retry
   *after* the intent. Before it, timing failures (`unknown_page`, browser `TimeoutError`) may
   be retried at most `MAX_TRANSIENT_RETRIES` times, each pass re-running full policy.
6. **No Discord API, bot token, WebSocket interception, OCR, or CAPTCHA/MFA bypass.**
7. **Never open notification links.** Python navigates to `[target].url` only.
8. **Never commit** `config.toml`, `secrets/`, `profiles/`, `runtime/`, `artifacts/`.
9. **Never log** card numbers, CVVs, passwords, cookies, or checkout tokens.
10. **Selectors live in one place each** (`extension/content/selectors.js`,
    `src/amore_ocarina_sniper/amazon/selectors.py`) and carry a verified/assumed status.
11. The bridge binds `127.0.0.1` only; no wildcard CORS; no exposed debugging ports.
12. **No aggressive polling.** The product watcher (`watch.py`) is the only self-initiated
    Amazon traffic: one page load per `watch.interval_s` (≥ 10 s, enforced at config load),
    only while ARMED, never while an attempt is in flight, exponential back-off on any
    challenge. It triggers the *same* coordinator path as a Discord alert and cannot bypass
    policy, arming, or the single-purchase rule.

## Commands

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"          # once
python -m playwright install chromium      # once
python -m pytest -q                        # full suite (~95 s, headless Chromium for fixture tests)
python -m pytest -q -m "not browser"       # fast subset
ruff check src tests
ocarina doctor                             # readiness + selector verification table
```

Operator commands (`pair`, `setup`, `run`, `arm`, `disarm`, `kill`, `status`, `trigger`,
`reset`, `reconcile`) are documented in `README.md` → *Commands*.

## Conventions

- Python 3.12+, asyncio, Playwright async API, `Decimal` for money, aiohttp for the bridge.
- Ruff: line length 110, rules `E F I B UP ASYNC` (tests ignore `E501`).
- New state-machine transitions go through `store.py` CAS helpers; add a test in
  `tests/test_coordinator.py` / `tests/test_store.py`.
- Any selector change must update its status in the selector module **and** the
  *What is verified and what is not* section of `README.md`.
- Matching logic changes must keep JS/Python parity vectors green
  (`tests/test_matching.py`, `tests/test_extension_matcher.py`).
- Keep diagnostics (screenshots, telemetry flushes, notifications) off the critical path
  between trigger receipt and submission.
- Prefer small, surgical edits; do not rewrite modules to make a change.

## Definition of done for a task

- Tests added or updated and `python -m pytest -q` passes.
- `ruff check src tests` clean.
- `README.md` updated if commands, config keys, or verification status changed.
- Task board below updated; commit message describes the user-visible effect.

---

## Milestones

Status legend: `[x]` done · `[~]` in progress · `[ ]` not started · `[!]` blocked on external input

### M1 — Core pipeline on fixtures `[x]`
Extension → bridge → coordinator → Amazon adapter, state machine, durable store, CLI, telemetry,
165 fixture-driven tests. Commits `e372196`, `75aebe3`.

### M2 — Live read-only verification `[~]`
Confirm assumptions against real pages without submitting anything.

- [x] Amazon.ca product page selectors, signed out (2026-09-24; listing was *Currently unavailable*).
- [x] Amazon.ca product page while **signed in** (2026-09-24: `signed_in=True`, `challenge=None`,
  title parsed; listing still *Currently unavailable* so price/seller/buy box not yet observed).
- [x] Discord live DOM (2026-09-24, Comet, DevTools on a real Lbabinz alert): list,
  `li#chat-messages-*`, `#message-content-*`, `#message-accessories-*`, `time[datetime]`,
  embed title/description, username header (`data-text` + `APP` botTag) all verified.
  Finding: the alert app uses a **default avatar** → no user id in the DOM → *Sender user id*
  must stay empty for this author (fails closed otherwise). Fixed name matching to strip the
  badge; added sticky *Last skip* row to the popup.
- [x] Checkout review page (2026-09-25 21:19, operator's redacted HTML of a live signed-in
  `/checkout/p/<id>/spc` with a third-party item): every `CHECKOUT_PAGE` field verified; six
  assumed fields (ASIN, quantity, total, seller, address, payment) would have read empty →
  fixed. Still assumed: confirmation markers, order-id pattern, turbo iframe (irrelevant on
  the cart path).
- [x] Amazon-as-seller display text: `Amazon.ca` (live 2026-09-25 03:52). No *Ships from* row
  on the Amazon-sold pre-order; fulfiller now inferred from seller in that case.
- [x] Buy Now is never shown on this listing (operator, 2026-09-25); the cart path is primary
  and was rehearsed end-to-end in the bot's profile (3.0 s to the review page). Cart tidy
  handles unrelated items / leftover quantity.

### M3 — Always-on Windows deployment `[ ]`
- [x] `ocarina pair` / `ocarina setup` completed on the host (2026-09-24); Amazon signed in.
  Saved address + payment still to be confirmed via the checkout probe.
- [x] Extension loaded unpacked in Comet (Chromium), options saved incl. sender user id
  (2026-09-24). Popup: bridge *connected*, Python *ARMED · dry-run*, *correct channel*,
  *monitoring*. (Tab had to be reloaded after Load unpacked for the content script to inject.)
- [x] `allowed_extension_origin` pinned in `config.toml`; `target.discord_channel_id` pinned.
- [x] Host: AC sleep = Never, display = Never (already set); Comet Memory Saver on with
  `discord.com` in *Always keep these sites active* (2026-09-24).
- [ ] Runner started via Scheduled Task (currently a persistent terminal; dies with the session).
- [ ] Remote access (RDP/VNC/Tailscale) into the same browser profile confirmed for challenges.
- [x] Dry-run soak (2026-09-24): `ocarina run` + `arm --minutes 30` + `trigger` → accepted,
  `ARMED -> VERIFYING -> ARMED` in 2 s, rejected on *not in stock*. Policy config passes `arm`.

### M4 — Armed operation `[~]`
Gate: every M2 checkout item is verified and M3 is complete. **Operator chose to go live early**
(2026-09-24 23:15) accepting that unverified checkout selectors most likely fail closed (missed
drop) rather than mis-purchase; the run doubles as the checkout probe. See Decision log.

- [~] `ocarina run --live` in a standalone pwsh window, armed 720 min until 2026-09-25 11:15.
  Console log mirrored to `runtime/logs/runner.log`; `telemetry.verbose = true` for this run.
  On any rejection with stock present, full-page screenshot + all-frame HTML land in
  `runtime/artifacts/` and the full snapshot dict in `telemetry.jsonl`.
- [ ] Confirm the `PURCHASED` transition disables further purchases; `ocarina reconcile`
  matches order history.
- [ ] Record real end-to-end latency points from `runtime/logs/telemetry.jsonl` and replace the
  synthetic ≈3 s figure in `README.md`.

### M5 — Hardening / nice-to-have `[ ]`
- [x] Benchmark `checkout.block_heavy_assets` (2026-09-25, headless, throwaway signed-out
 profile, 4×4 alternating runs): median 1.28–1.36 s off vs 1.32–1.33 s on — no gain, because
 `verify_offer` already stops at `domcontentloaded`. Leave it `false`.
- [ ] Popup: surface *list not found* / selector drift more loudly.
- [ ] Optional operator notification (off the critical path) on `NEEDS_ATTENTION` / `UNKNOWN`.
- [ ] CI: run `ruff` + non-browser tests on push.

## Open blockers (external)

- **Moderator acceptance** of local automated monitoring on the source Discord server is
  unresolved. Do not arm until this is settled; never conceal the extension's behaviour.
- **Amazon session**: a signed-in `profiles/amazon` with saved address and payment is required
  for M2 checkout verification and everything after.
- **Stock**: checkout-probe verification cannot happen until the listing is in stock.

## Task board

Add new items at the top of *Next up*. Move to *Done* with the commit hash.

### First live event — 2026-09-25 03:52:15 (event `755171032393973760-1552950852480667680`)
- Detection 103 ms after message ts; bridge +7 ms; offer read in 2.35 s. Price `709.99`,
  seller `Amazon.ca`, correct ASIN/title. Availability: *"This item will be released on
  October 29, 2026. Pre-order now."* No *Ships from* row. Refused: pre-order (policy) +
  fulfillment unreadable + condition unreadable (both consequences). Window closed by 04:07.
  Operator tried manually at 03:55 and it was already gone: **window < 3 min.** Bot decision
  at 03:52:18 (+3 s) would have been inside it; only the pre-order policy blocked it.
- Fixes shipped (runner restarted 04:17, armed until 16:17): `is_preorder` detection,
  `policy.allow_preorder` (default false, **operator decision pending**), fulfiller inferred
  from Amazon-as-seller when no row is rendered, condition inferred for pre-orders, full
  extractor output retained in `OfferSnapshot.raw` and emitted in telemetry, screenshot+HTML
  captured on any rejection where an offer was present. 173 tests.

### Outage — 2026-09-25 07:41 → 10:15
- Windows Update (`MoUsoCoreWorker.exe`) initiated a restart at 07:41; PC back 10:03, second
  update restart 10:05. No Kernel-Power 41, so not a hard thermal cut. Runner down ~2.5 h;
  no alert reached the bot in that window (Discord history to be checked by operator).
- Runner relaunched 10:15 LIVE, armed until 22:15. Amazon session survived (profile on disk).
- Follow-ups: operator to pause Windows Update + set active hours; Scheduled Task at logon
  (M3) is now a priority; auto sign-in after restart so the task fires.

### Speed review — 2026-09-25 evening
- Our side of the critical path is a few hundred ms; the rest is Amazon serving pages.
  `block_heavy_assets` benchmarked: no gain (M5). Checkout loop already polls at 50 ms.
- **Operator approved a second trigger source** (2026-09-25): `watch.py` polls the product
  page on a second tab every 30 s while ARMED and feeds `coordinator.handle_trigger` a
  synthetic `watch-<ms>` event when `evaluate_offer` would pass. Config `[watch]`
  (`enabled`, `interval_s` ≥ 10, `retrigger_cooldown_s`). 185 tests.
- Enabled in the host `config.toml`; runner restarted LIVE with the watcher on.

### Edge-case pass — 2026-09-25 evening
- Operator asked for pre-emptive fixes (2+ cart items, manual buying via HotStock in parallel,
  correct card/address, instant pre-order). Shipped: `policy.payment_matches` accepts masked
  card renderings (`•••• 4105`) for a fragment ending in 4 digits; `CheckoutSnapshot.
  payment_input_required` (visible CVV / card input → refused pre-intent instead of UNKNOWN
  post-click); checkout `raw` retains fields/present/counts/buttons like the offer does;
  adapter warns when the cart path was abandoned (item stays in cart); `notify.py` (ntfy push
  on PURCHASED / REFUSED-with-stock / NEEDS_ATTENTION / UNKNOWN / first watch challenge /
  runner start) + `ocarina notify-test`; README *Edge cases* table with the operator checklist.
  209 tests.
- Operator checklist (README → Edge cases): empty cart, Amazon default address = 1447 Sycamore,
  default payment = Visa 4105, one small recent purchase with that card, install ntfy app and
  subscribe to the topic in `config.toml`, "robot first" rule for HotStock alerts.

### Cart path made primary — 2026-09-25 21:00
- Operator: the listing will **never show Buy Now** (pre-order only → "Pre-order now" is the
  add-to-cart slot). Adapter now: click → wait for add confirmation (badge/side sheet/URL) →
  badge == 1 and side-sheet *Proceed to checkout* visible → straight to checkout; otherwise
  cart page → `_tidy_cart` (untick / Save for later / Delete non-target rows, step target qty
  to 1, ≤ 4 passes, bounded) → Proceed. Target missing from cart → fail closed. Rows unreadable
  → proceed, review-page gate decides.
- 21:04 operator pasted `#sc-active-cart` outerHTML from the signed-in cart (one item): rows,
  `data-quantity`/`data-isselected`, checkout checkbox (`.sc-list-item-checkbox`), gift checkbox
  (must be avoided), atomic stepper (`fieldset[data-action=a-stepper][data-steppervalue]`; the
  decrement `button` is **Delete** at qty 1), `input[data-action=save-for-later|delete-active]`
  all VERIFIED; fixture rebuilt to mirror it; decrement is now one click per pass (≤ 6 passes).
  21:16 operator pasted the cart *Proceed to checkout* input → VERIFIED (`name`, `data-feature-id`,
  `aria-labelledby` all match the first candidates).

### Review page verified — 2026-09-25 21:19
- Operator ran a redacting DevTools snippet on the live review page (GameSir item, saved Visa)
  → `runtime/artifacts/review-page.html` (git-ignored). Findings: ASIN is *text* in
  `span[data-testid^=Item_asin_]` (no `data-asin` anywhere); quantity is an atomic stepper
  (`fieldset[name=checkout-quantity-stepper]`, value in `[data-a-selector=inner-value]`);
  total in `[data-shimmer-target=ordertotals-amount]` inside the only bold `<li>`; seller
  `.lineitem-seller-section`; address `#deliver-to-address-text` ("1447, Sycamore Garden, …"
  — comma survives `normalize_text`, config fragment still matches); payment
  `#selected-payment-methods-list-container` ("Paying with Visa 4105" — matched via the
  last-4 fallback); six `input[name=placeYourOrder1]`, four of them **disabled** blockers →
  candidates now `:not([disabled])`; no condition row; no CVV prompt.
- **Six of eleven assumed fields would have read empty → the order would have been refused.**
  Selectors fixed, `item_asin_attr` legacy fallback added, `checkout.html` fixture rebuilt to
  mirror the live markup (+ `blocked` variant), review test asserts the *verified* selector
  matched for each field.

### Rehearsal in the bot's own profile — 2026-09-25 21:40–21:48
- New `ocarina doctor --browser --checkout-probe --probe-asin <ASIN>`: runs the real
  add-to-cart → cart → review path against a stand-in (GameSir controller `B0GJZ8WJD9`) in
  dry-run with the cart strategy forced; policy reported not bypassed; `adapter.probe_dir`
  writes redacted dumps (`after-add-to-cart`, `cart-page`, `review-page`) to
  `runtime/artifacts/probe-<ts>/`. Runner must be stopped for it (profile lock); ~3 min.
- Run 1 (took Buy Now — stand-in has it): **bug** — legacy `.a-size-small:has-text('Condition')`
  matched the footer "…privacy notice and Conditions of use" → `condition != new` → **would
  have refused the real order**. Candidate removed; `parse_checkout` now sanity-checks the
  condition text (`_looks_like_condition`) and inherits from the product page otherwise.
- Run 2 (cart path): add registered but **no confirmation id existed** → waited out the
  element timeout → review page in 11.4 s. Dump showed the desktop ATC button (`attach-dss-atc`)
  adds via AJAX and opens `#attach-desktop-sideSheet` with a **warranty upsell**
  (`#attach-warranty-pane`, `#attachSiAddCoverage`/`#attachSiNoCoverage`), no Proceed button;
  nav badge is progressively loaded (baseline unreadable). `_add_to_cart_confirmed` now
  watches three signals: POST response to `/cart/add-to-cart`, a *visible* confirmation
  surface (verified side-sheet selectors first), badge > (before or 0). Cart step re-reads once
  before failing closed on "target missing".
- Run 3: acknowledged via surface in 0.30 s; review page in **3.02 s**; every field matched its
  verified selector; policy refused only on the expected ASIN/title/seller. Runner relaunched
  LIVE 21:49, armed until 09:49.
- 21:53 operator's order-history page: `/gp/css/order-history` redirects to *Your Orders*,
  ids match `ORDER_ID_PATTERN` → VERIFIED. Still ASSUMED: confirmation (thank-you) markers
  and order-id location on it (only observable on a real order), side-sheet Proceed button
  (not offered on this account), MFA selectors.
  Fixtures `product_preorder_cart_only.html`, `cart.html` (8 variants); 8 browser tests.

### Rush hardening — 2026-09-25 22:10
- Coordinator `_run_attempt` split into `_verify_and_prepare` (retry loop, `is_transient`) and
  `_commit` (unchanged post-intent path). `AttemptOutcome.transient`. Watcher promote/cooldown/
  back-off changes; adapter `promote_watch_page`, `_loaded_at`, `verify_offer(reuse_within_s)`.
  226 tests. Host `interval_s = 15`. Runner restarted LIVE.

### Edge-case pass 2 — 2026-09-25 22:30 (code review of the critical path)
- **Amazon's overload page was classified as a ban**: "sorry! something went wrong" / "request
  could not be satisfied" mapped to `access_denied` → `NEEDS_ATTENTION` → bot parked for the
  rest of the drop. New `ChallengeKind.SERVER_ERROR` (transient; `ChallengeKind.transient`),
  `CHALLENGES["server_error_text"]`; `access_denied_text` keeps only the real block markers.
- Evidence on the failure path was captured **after** `abandon()` had navigated back to the
  product page (screenshot of the wrong page). Now captured first, with HTML, only on the
  final pass (`challenge-<kind>` / `pre-intent-error`), never before a retry.
- Retry reused nothing: `abandon()` reloads the product page and the retry reloaded it again.
  `abandon()` now records `_loaded_at`; retries pass `reuse_within_s`.
- Review page with only disabled *Place order* blockers / an interstitial returned a snapshot
  the policy could only refuse → benign refusal → 120 s watcher cooldown mid-drop. Now waits for
  a *visible* enabled control, else raises transient `unknown_page` (retried).
- Watcher cooldown is bound to the refused offer's signature (in_stock, preorder, availability,
  price, seller); a different offer triggers immediately. Polling continues during cooldown.
- Pushes: informational *Stock seen — bot retrying* on the first retry when an offer was present;
  *arm window ends in 30 minutes* and *DISARMED: armed session expired* from `_expiry_watch`.
- 238 tests. **Operator decision pending:** `policy.max_arm_minutes` is 720 (12 h); the drop can
  come at any hour before Oct 29 — consider 4320 (3 days) and arming for the full window.

### Next up
- [x] Operator raised `policy.max_arm_minutes` to 4320 (3 days) 2026-09-25 22:49; armed until
  2026-09-28 22:49. Re-arm every ~3 days (expiry pushes at T-30 min and at expiry).
- [ ] After the first `watch_trigger` at 15 s cadence: confirm no `watch_challenge` in
  telemetry; if one appears, set `interval_s` back to 30.
- [ ] Operator: install ntfy on iPhone, subscribe to the `[notify].ntfy_topic` in `config.toml`,
  run `ocarina notify-test`, confirm the push arrives.
- [~] Operator: verify Amazon default address / default payment card. Cart emptied
  (rehearsal stand-in removed 2026-09-25 22:24).
- [ ] After the first `watch_trigger` / `watch_challenge` in `telemetry.jsonl`: confirm Amazon
  tolerates the 30 s cadence on a signed-in session (no CAPTCHA); otherwise raise the interval.
- [ ] Scheduled Task: `ocarina run --live` at logon (runner still comes up DISARMED by design).
- [ ] Operator: pause Windows Update (Settings → Windows Update → Pause), set active hours.
- [x] Operator approved pre-orders 2026-09-25 04:20: `allow_preorder = true`, runner
  restarted LIVE, armed until 16:20. Next alert → full checkout attempt.
- [ ] After the next event: read `runtime/logs/runner.log`, `telemetry.jsonl`
  (`checkout_checked.snapshot`), `runtime/artifacts/*checkout-rejected*`; fix any unread
  review-page field; mark M2 checkout selectors verified/adjusted.
- [x] Operator cleared *Sender user id* in extension Options; *Sender display name* = `Lbabinz`.
  Extension reloaded with `c51a9c7`; popup shows *monitoring* and *Last skip: none*
  (2026-09-24 22:59). Runner left in dry-run, armed until 2026-09-25 10:59.
- [ ] Wait for the next real Lbabinz alert in dry-run; confirm popup *Last match* updates and
  `ocarina status` shows an accepted event (expected to stop at *not in stock* unless restocked).
- [ ] Decide `checkout.strategy` default after the first `--checkout-probe`.

### In progress
- (none)

### Done
- [x] `4315184` Popup pulls tab status on open (fixes *no status yet* after MV3 worker
  suspension). 168 tests.
- [x] `969608b` Evidence capture on rejection; runner always logs to `runtime/logs/runner.log`.
- [x] Discord selectors live-verified; sender-name badge fix; popup *Last skip*; 3 new tests.
- [x] `ocarina doctor --browser` on host: signed in, no challenge, title parsed, unavailable.
- [x] `75aebe3` README: commands, extension install, Windows deployment, verification and
  blocker report; relax test line-length lint.
- [x] `e372196` Python purchase coordinator, localhost trigger bridge, MV3 Discord watcher
  extension, fixture-based test suite.
- [x] `a202ee8` Project brief and implementation prompt.

## Decision log

- 2026-09-25 22:30 — **Amazon error pages are transient, not challenges.** "Sorry! Something
  went wrong" and CloudFront "request could not be satisfied" are Amazon's overload responses,
  the most likely thing to be served during a pre-order rush; parking the bot on them would
  have lost the window without any human check being involved. Only *Access Denied* /
  *automated access* text stops the bot now. Revert: move the strings back to
  `access_denied_text` in `selectors.py`.
- 2026-09-25 22:10 — **Rush hardening**, operator's call after the "what happens when everyone
  clicks" review. (a) Pre-intent transient failures (`unknown_page`, browser `TimeoutError`)
  are retried ≤ 2× within the trigger and otherwise return to `ARMED` (push after a streak of
  3) instead of `NEEDS_ATTENTION`, which was designed for selector drift but under load was
  the most likely way to lose the whole window; real challenges still stop the bot.
  (b) Watcher: no re-trigger cooldown after a transient failure; mild (≤ 2×) back-off on
  timeouts, exponential only on challenges. (c) Watcher promotes its tab before triggering;
  `verify_offer(reuse_within_s=10)` skips the navigation for that one pass (≈ 2 s).
  (d) `watch.interval_s` 30 → **15** in the host config; ~200 polls at 30 s had produced no
  challenge; a CAPTCHA (notified, solved by hand) is the realistic downside, not a ban.
  Revert any of these independently: constants in `coordinator.py`, `watch.py`, config.
- 2026-09-25 — **Self-polling enabled (30 s)**, operator's call after the speed review. The
  spec's "avoid aggressive polling" is honoured by a hard ≥ 10 s floor, jitter, armed-only
  operation, back-off on challenges and a re-trigger cooldown. Rationale: the Discord alert
  is someone else's bot; the 03:52 event showed the whole window is < 3 min, so seconds of
  head start matter more than any local optimisation. Revert with `watch.enabled = false`.
- 2026-09-24 23:15 — **Live before checkout probe**, operator's call. Rationale: item is
  *Currently unavailable*; the first restock is the only chance to observe the real review page,
  and fail-closed policy checks make a wrong purchase far less likely than a missed one. Added
  evidence capture (screenshot + HTML + snapshot dict) on offer/checkout rejection so a miss is
  fixable. `ocarina kill` is the emergency stop. Revert to dry-run (`ocarina run` without
  `--live`) if anything looks off.
- 2026-09-24 — `ocarina run` now always mirrors its log to `data_dir/logs/runner.log`;
  PowerShell `Tee-Object` piping from a `Start-Process` window did not flush for a long-running
  native process.

- 2026-09-24 — Buy Now is the default checkout strategy; `cart` is the fallback. Revisit after
  the first live checkout probe.
- 2026-09-24 — `policy.max_item_price` ships as `"0.00"` so `ocarina arm` refuses until the
  operator sets a limit deliberately. The alert's CAD 709.99 is never used as a limit.
- 2026-09-24 — Headed browser by default; `--headless` only if Amazon is shown not to
  challenge it more often.
