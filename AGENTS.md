# AGENTS.md — AmoreOcarinaSniper

Working notes for humans and coding agents. The **Milestones** and **Task board** sections are
living documents: update them in the same commit as the work they describe.

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
  `telemetry`, `bridge`, `coordinator`, `watch`, `app`, `cli`; `amazon/` holds `selectors.py`,
  `extract.py`, `adapter.py`.
- `tests/` — pytest suite; fixtures in `tests/fixtures/{discord,amazon}/`.
- `config.example.toml` — the only config that is committed.

## Hard rules (do not relax without an explicit user decision)

1. **No real orders in development or tests.** Tests run against local fixtures only.
2. **Fail closed.** Missing, ambiguous, or unreadable policy fields reject the attempt.
3. **The trigger path cannot arm, disarm, reset, or change policy.** Control is CLI → SQLite.
4. **Dry-run and DISARMED by default.** `--live` and `ocarina arm` are both required; restarts
   always require re-arming.
5. **Intent is persisted before the final click.** Ambiguity → `UNKNOWN`, never auto-retry.
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
python -m pytest -q                        # full suite (~75 s, headless Chromium for fixture tests)
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
- [!] Checkout review page (`CHECKOUT_PAGE` selectors, turbo-checkout iframe, confirmation
  markers, order-id pattern) via `ocarina doctor --browser --checkout-probe` — needs signed-in
  session **and** the item in stock.
- [x] Amazon-as-seller display text: `Amazon.ca` (live 2026-09-25 03:52). No *Ships from* row
  on the Amazon-sold pre-order; fulfiller now inferred from seller in that case.
- [ ] Does Buy Now on this listing expose every policy field? If not, switch default
  `checkout.strategy` to `"cart"` and document the unrelated-cart-items caveat.

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

### Next up
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
