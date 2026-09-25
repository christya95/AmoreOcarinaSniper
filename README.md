# AmoreOcarinaSniper

A Chromium (Manifest V3) extension watches **one** open Discord channel for **one** configured
stock alert and forwards a minimal trigger to a Python process on `127.0.0.1`. The Python
process verifies the configured Amazon.ca listing in an already-signed-in Playwright browser
and, **only when explicitly armed and every policy check passes**, submits a single order.

Target: ASIN `B0HJ6F8L6V` — Nintendo Switch 2 – The Legend of Zelda – 40th Anniversary Edition
(https://www.amazon.ca/dp/B0HJ6F8L6V). The CAD 709.99 figure in the example alert is reference
data only; the spending limit is whatever you put in `config.toml`.

**Status:** implemented and tested against local fixtures and one live signed-out product-page
probe. Not yet exercised against a signed-in Amazon checkout or a live Discord session — see
[What is verified and what is not](#what-is-verified-and-what-is-not) before arming anything.

---

## How it works

```
Discord tab (content script)            Service worker                Python (always on)
────────────────────────────            ──────────────                ──────────────────
MutationObserver on the rendered   →   dedupe (chrome.storage)   →   POST 127.0.0.1:48620/v1/trigger
message list of the configured         bounded retries, same         Bearer pairing secret, Origin/Host,
channel; baseline history; match       event id; never sees          schema, size, freshness, durable
text+embeds; freshness from            page content                  dedupe → ack
<time datetime>; ≤1 trigger/message
                                                                     coordinator (asyncio, one worker)
                                                                     ARMED? → verify offer → Buy Now →
                                                                     verify review page → kill switch →
                                                                     persist intent → Place order →
                                                                     confirm order id → PURCHASED (disabled)
```

Design rules that are enforced in code, not just documented:

- The bridge **cannot** arm, disarm, reset, or change policy. Control commands write SQLite
  directly (`ocarina arm|disarm|kill|reset`).
- The runner starts **dry-run** and **DISARMED**. `--live` is required for any submission, and a
  restart always requires an explicit `ocarina arm` again.
- Every policy field is read from what the browser actually shows immediately before the click.
  Missing, ambiguous, or unreadable fields fail closed (`policy.py`).
- Submission intent is persisted **before** the click. Anything ambiguous afterwards ends in
  `UNKNOWN`, which disables further purchases until `ocarina reset --confirm`. A confirmed
  purchase does the same. This prevents double clicks; it does not make browser automation
  exactly-once.
- Notification links are never opened; Python always navigates to `[target].url`.
- No Discord API, bot token, WebSocket interception, OCR, or CAPTCHA/MFA bypass anywhere.

## Requirements

- Windows 10/11 (Linux/macOS also work), Python 3.12+, Chrome/Edge/Brave (Chromium ≥ 116).
- Playwright Chromium: `python -m playwright install chromium`.

## Install

```powershell
git clone https://github.com/christya95/AmoreOcarinaSniper.git
cd AmoreOcarinaSniper
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
python -m playwright install chromium
copy config.example.toml config.toml
```

Edit `config.toml`. The `[policy]` block ships with `max_item_price = "0.00"` and empty
address/payment fragments, so **`ocarina arm` refuses until you set them deliberately**:

| Key | Meaning |
| --- | --- |
| `policy.max_item_price` | CAD, decimal string. Item price on the product page and review page must be ≤ this. |
| `policy.max_total` | CAD. Order total (tax, shipping, fees) on the review page must be ≤ this. |
| `policy.allowed_sellers` | e.g. `["amazon.ca"]`. Third-party sellers are rejected unless listed. |
| `policy.fulfillment` | `"amazon"` (Ships from Amazon) or `"any"`. If no *Ships from* row is rendered, Amazon-as-seller is accepted; other sellers fail closed. |
| `policy.allow_preorder` | Default `false`. Accept listings whose availability announces a release date ("Pre-order now"). All other checks still apply. |
| `policy.approved_address_contains` | Fragment that must appear in the shipping address block (e.g. street number + name). |
| `policy.approved_payment_contains` | Fragment that must appear in the payment block (e.g. `ending in 4242`). |
| `policy.default_arm_minutes` / `max_arm_minutes` | Armed-session bounds. |
| `target.discord_channel_id` | Optional: bridge rejects triggers from any other channel id. |
| `bridge.message_freshness_s` | Rendered Discord timestamp must be at most this old (default 90 s). |
| `checkout.strategy` | `"buy_now"` (default) or `"cart"`. See caveats below. |

## Commands

```powershell
ocarina pair                 # generate the pairing secret (secrets/bridge_secret) and print extension values
ocarina setup                # open the dedicated browser profile; sign in to Amazon.ca yourself (MFA included)
ocarina doctor               # readiness: config, secret, profile, policy, selector verification table
ocarina doctor --browser     # also open the product page and print the parsed offer + policy verdict
ocarina doctor --browser --checkout-probe   # dry-run into the review page and print every field read (nothing submitted)
ocarina run                  # always-on runner, DRY-RUN (never clicks Place order)
ocarina run --live           # allow submission when armed and all checks pass
ocarina arm --minutes 120    # arm for a bounded time (refused if policy is not purchase-ready)
ocarina disarm
ocarina kill                 # kill switch file + disarm; checked immediately before the final click
ocarina status --transitions
ocarina trigger              # synthetic trigger through the real bridge (accepted → verify/dry-run)
ocarina trigger --stale-seconds 600         # demonstrates freshness rejection (HTTP 410)
ocarina reset --confirm      # leave PURCHASED/UNKNOWN/NEEDS_ATTENTION, re-enable, remove kill switch
ocarina reconcile            # read-only look at order history to help resolve UNKNOWN
```

`ocarina` is installed as a console script; `python -m amore_ocarina_sniper …` is equivalent.
Use `--config path\to\config.toml` or `OCARINA_CONFIG` to point at a different file.

## Extension install (Chromium)

1. `ocarina pair` → note **Bridge port**, **Pairing secret**, **Target id**.
2. `chrome://extensions` → enable *Developer mode* → *Load unpacked* → select the `extension/` folder.
3. Open the extension's **Options**: paste guild id, channel id (from the Discord URL
   `discord.com/channels/<guild>/<channel>`), bridge port, pairing secret, target id. Save,
   then *Test bridge* (the runner must be up).
4. Optionally set `allowed_extension_origin = "chrome-extension://<id>"` in `config.toml`
   using the id shown on `chrome://extensions`, and restart the runner.
5. Open the Discord channel in a tab and leave it there. The popup shows: bridge
   connected/disconnected, Python state, correct/wrong channel, monitoring/paused, last match.

The extension only sends `event_id`, `message_id`, `channel_id`, `guild_id`, `matched_target`,
`message_ts_ms`, `sent_at_ms`, `detected_at_offset_ms`, `attempt`, `source`. No chat text.

### Discord DOM check

Live-verified 2026-09-24 on the target channel (Chromium/Comet), against a real alert from the
configured app: `ol[data-list-id="chat-messages"]`, `li#chat-messages-<channel>-<message>`,
`#message-content-<id>`, `#message-accessories-<id>`, `time[datetime]`, embed title/description,
and `span#message-username-<id>` containing `span.username_*[data-text]` plus a `span.botTag*`
badge whose text is `APP`. Selectors are centralized in `extension/content/selectors.js`. If
Discord changes its DOM later, re-run in DevTools on the channel:

```js
document.querySelector('ol[data-list-id="chat-messages"] li[id^="chat-messages-"]')?.id
document.querySelector('li[id^="chat-messages-"] time[datetime]')?.getAttribute('datetime')
```

**Sender user id caveat.** The optional *Sender user id* is read from the avatar image URL
(`/avatars/<id>/…`). Authors with Discord's **default** avatar render `/assets/<hash>.png` and
expose no user id anywhere in the message DOM; the alert app in the target channel is one of
them. With a user id configured, every such message fails closed and is dropped. Leave the field
empty for these authors and rely on *Sender display name* (the target channel is read-only, so
only moderators and their apps can post). The popup's *Last skip* row shows the most recent
fail-closed reason so silent drops are visible.

## Recommended bring-up sequence

1. `ocarina pair`, `ocarina setup` (sign in, confirm a saved address and payment method exist).
2. `ocarina doctor --browser` — confirm `signed_in=True` and that the offer is parsed.
   The listing was **Currently unavailable** on 2026-09-24, so expect `not in stock`.
3. `ocarina run` (dry-run) in one window; `ocarina arm --minutes 60`; `ocarina trigger` in another.
   Expect an attempt that stops at the offer check. Read `ocarina status --transitions`.
4. When the item is in stock and you are signed in: `ocarina doctor --browser --checkout-probe`.
   This opens the review page in dry-run and prints every field. **Do not proceed to `--live`
   until each field is read correctly** (the checkout selectors are assumptions; see below).
5. Only then: `ocarina run --live`, `ocarina arm --minutes N`.

## Windows always-on deployment

- Run the runner in a terminal you keep open, or as a Scheduled Task (*Run only when user is
  logged on*, trigger *At log on*). The browser is headed by default; use `--headless` only
  if you have verified Amazon does not challenge it more often.
- Prevent sleep: `powercfg /change standby-timeout-ac 0` and `powercfg /change monitor-timeout-ac 0`
  (or Settings → Power). Discord tabs in a sleeping machine do not observe anything.
- Keep the Discord tab foregrounded in its own window; disable Chrome's *Memory Saver* for
  `discord.com` (`chrome://settings/performance`). Suspended/discarded tabs stop detecting.
- Restart behaviour: after any restart the runner is **DISARMED** (and `UNKNOWN` if it died
  after intent). Re-run `ocarina status`, resolve, then `ocarina arm` again.
- Challenges (CAPTCHA, MFA, expired login, payment verification) move state to
  `NEEDS_ATTENTION` with a screenshot in `runtime/artifacts/`. Resolve them **in the same
  browser profile** (RDP/VNC/Tailscale into the host). Note: an RDP session that is
  disconnected (not signed out) keeps the desktop alive; locking the console can throttle
  Chrome timers and delay detection.
- Never expose port 48620 or any browser debugging port beyond loopback. The bridge refuses
  to bind anything but `127.0.0.1`.

## Latency

Measured points (see `runtime/logs/telemetry.jsonl`): rendered Discord timestamp, extension
detection, bridge receipt, offer verified, checkout prepared, intent persisted, submitted,
confirmed. Local spans use `perf_counter_ns`; cross-process figures are wall-clock deltas and
are reported separately. Synthetic measurement on this machine (fresh headless profile,
signed out, live product page): trigger accepted → product page evaluated ≈ 3 s. No end-to-end
purchase latency has been measured; none is promised.

`checkout.block_heavy_assets` (images/fonts/media) is off by default. Benchmark with
`doctor --browser` before enabling; it must not break checkout.

## Tests

```powershell
python -m pytest -q          # 173 tests, ~80 s (headless Chromium for fixture-driven tests)
ruff check src tests
```

Coverage highlights (all on local fixtures; no real orders are ever placed):

- Matching: text/embeds, Unicode/full-width/trademark variants, unrelated products, delayed
  embeds; JS/Python parity vectors.
- Extension content script: baseline history, duplicate mutations, stale messages, missing
  timestamps, virtualized re-mounts, list replacement, channel navigation, DOM channel
  mismatch, sender-id gate, pause, extension reload.
- Service worker: auth header + schema, durable dedupe, terminal 4xx, bounded retries with the
  same event id, worker restart, gating.
- Bridge: auth, origin/host, size, JSON, stale/skew, target/channel pin, duplicate retries.
- Amazon adapter: in-stock/unavailable/third-party/variant/USD/wrong-ASIN offers; CAPTCHA,
  sign-in and signed-out challenges; review-page parsing; two line items, wrong address,
  wrong payment, unreadable total, over total, third-party seller, quantity 2; dry-run refusal;
  live confirm, ambiguous, payment challenge.
- Coordinator: dry-run never submits; purchase disables until reset; crash before/after intent;
  ambiguous submission → UNKNOWN with no retry; kill switch; disarm mid-attempt; concurrency.
- Store/lock: CAS transitions, restart recovery, two-process lock.

## What is verified and what is not

**Live-verified (www.amazon.ca desktop, signed out, 2026-09-24):**
`input#ASIN`, `#productTitle`, `#availability` (both "In Stock" and "Currently unavailable"),
`#corePrice_feature_div .a-price .a-offscreen`, `#sellerProfileTriggerId` (+ `seller=` in href),
`#merchantInfoFeature_feature_div` / `#fulfillerInfoFeature_feature_div` ("Sold by" / "Ships
from Amazon"), `#buy-now-button` (`submit.buy-now`, form `addToCart`), `#add-to-cart-button`,
`#quantity`, `#twister`, `#nav-link-accountList` ("Hello, sign in"). The target listing
exists with the expected title and was **unavailable** (no price, no buy box).

**Assumed (fail-closed, must be probed with `doctor --browser --checkout-probe` once signed in
and in stock):** every checkout review-page selector in `amazon/selectors.py`
(`CHECKOUT_PAGE`), the turbo-checkout iframe, the confirmation page markers and order-id
pattern, order-history layout, MFA selectors. `ocarina doctor` prints the table.

**Live-verified end to end (2026-09-25 03:52, real alert, `--live`, armed):** Discord detection
103 ms after the message timestamp → bridge +7 ms → product page read in 2.35 s. `#productTitle`,
`input#ASIN`, price `$709.99`, seller `Amazon.ca`, availability *"This item will be released on
October 29, 2026. Pre-order now."* all parsed. No *Ships from* row was rendered for the
Amazon-sold pre-order. The attempt was refused (pre-order, `allow_preorder = false`); nothing was
submitted. The pre-order window had closed again by 04:07.

**Live-verified (discord.com, target channel, 2026-09-24):** message list, message/content/
accessories ids, `time[datetime]`, embed text, username header + `data-text` + `APP` badge. The
sender *user id* is confirmed **absent** from the DOM for the alert app (default avatar).

**Not verified at all:** Amazon-as-seller display
text (`Amazon.ca` vs `Amazon`; both are accepted via aliases), whether Buy Now on this listing
shows all policy fields (if not, switch `checkout.strategy = "cart"` — with the caveat that any
unrelated cart items make the attempt fail closed by design; the tool never edits your cart).

## Account and setup dependencies (blockers to unattended operation)

- An Amazon.ca account signed in inside `profiles/amazon` with a saved address and payment
  method. The tool never types credentials or card data and stores none of them.
- A Discord tab that stays loaded on the exact channel; Discord UI changes can break selectors
  silently — the popup shows *list not found* when that happens.
- The source Discord server prohibits bots except those run by moderators. This design uses
  no bot and no Discord API, but **moderator acceptance of local automated monitoring is
  unresolved**. Nothing here conceals the extension's existence or behaviour; ask before use.
- Amazon may challenge automation-flagged sessions (headless especially). Challenges pause the
  system; they are never bypassed.
- Checkout selectors are unverified until you run the checkout probe on a live, in-stock,
  signed-in session.

## Repository layout

```
extension/                 unpacked MV3 extension (manifest, shared/matcher.js, content/, background.js, popup, options)
src/amore_ocarina_sniper/  config, policy, models, store, lock, killswitch, telemetry, bridge, coordinator, app, cli
src/amore_ocarina_sniper/amazon/  selectors.py (verification status), extract.py (one-round-trip DOM extractor + parsers), adapter.py
tests/                     pytest suite; fixtures/discord/channel.html and fixtures/amazon/*.html
config.example.toml        secret-free example configuration
```

Never commit `config.toml`, `secrets/`, `profiles/`, `runtime/`, or anything under `artifacts/`.
