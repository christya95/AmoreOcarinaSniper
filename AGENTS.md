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
  `telemetry`, `bridge`, `coordinator`, `app`, `cli`; `amazon/` holds `selectors.py`,
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
- [ ] Amazon.ca product page while **signed in** (`ocarina doctor --browser` shows `signed_in=True`).
- [!] Discord live DOM: `li#chat-messages-*`, `#message-content-*`, `#message-accessories-*`,
  `time#message-timestamp-*[datetime]` — needs a Discord session on the target channel.
- [!] Checkout review page (`CHECKOUT_PAGE` selectors, turbo-checkout iframe, confirmation
  markers, order-id pattern) via `ocarina doctor --browser --checkout-probe` — needs signed-in
  session **and** the item in stock.
- [ ] Amazon-as-seller display text (`Amazon.ca` vs `Amazon`) — confirm which alias appears.
- [ ] Does Buy Now on this listing expose every policy field? If not, switch default
  `checkout.strategy` to `"cart"` and document the unrelated-cart-items caveat.

### M3 — Always-on Windows deployment `[ ]`
- [ ] `ocarina pair` / `ocarina setup` completed on the host; Amazon signed in with saved
  address + payment.
- [ ] Extension loaded unpacked, options saved, *Test bridge* green, popup shows *monitoring*.
- [ ] `allowed_extension_origin` pinned in `config.toml`.
- [ ] Sleep/Memory Saver disabled; runner started via Scheduled Task or persistent terminal.
- [ ] Remote access (RDP/VNC/Tailscale) into the same browser profile confirmed for challenges.
- [ ] Dry-run soak: `ocarina run` + `ocarina arm` + `ocarina trigger` stops at the offer check;
  `ocarina status --transitions` is readable.

### M4 — Armed operation `[ ]`
Gate: every M2 checkout item is verified and M3 is complete.

- [ ] `ocarina run --live` with a short `arm --minutes` window on a real in-stock event.
- [ ] Confirm the `PURCHASED` transition disables further purchases; `ocarina reconcile`
  matches order history.
- [ ] Record real end-to-end latency points from `runtime/logs/telemetry.jsonl` and replace the
  synthetic ≈3 s figure in `README.md`.

### M5 — Hardening / nice-to-have `[ ]`
- [ ] Benchmark `checkout.block_heavy_assets = true` with `doctor --browser`; keep only if it
  helps and does not break checkout.
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

### Next up
- [ ] Run `ocarina doctor --browser` on the deployment host once signed in; paste the
  selector table result into M2.
- [ ] Run the Discord DOM check snippets from `README.md` in DevTools on the target channel;
  fix `extension/content/selectors.js` if either returns `undefined`.
- [ ] Decide `checkout.strategy` default after the first `--checkout-probe`.

### In progress
- (none)

### Done
- [x] `75aebe3` README: commands, extension install, Windows deployment, verification and
  blocker report; relax test line-length lint.
- [x] `e372196` Python purchase coordinator, localhost trigger bridge, MV3 Discord watcher
  extension, fixture-based test suite.
- [x] `a202ee8` Project brief and implementation prompt.

## Decision log

- 2026-09-24 — Buy Now is the default checkout strategy; `cart` is the fallback. Revisit after
  the first live checkout probe.
- 2026-09-24 — `policy.max_item_price` ships as `"0.00"` so `ocarina arm` refuses until the
  operator sets a limit deliberately. The alert's CAD 709.99 is never used as a limit.
- 2026-09-24 — Headed browser by default; `--headless` only if Amazon is shown not to
  challenge it more often.
