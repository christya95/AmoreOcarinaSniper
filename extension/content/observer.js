/*
 * Content script: observes the rendered message list of ONE configured channel and
 * forwards at most one candidate per message to the service worker.
 *
 * Runs in the isolated world. Holds no secrets. Never clicks links, never reads
 * history beyond what is rendered, never touches Discord's network layer.
 */
(function () {
  "use strict";

  const SEL = globalThis.OcarinaSelectors;
  const M = globalThis.OcarinaMatcher;
  if (!SEL || !M) return;

  const ROUTE_POLL_MS = 500;
  const TRACKED_LIMIT = 2000;

  const state = {
    config: null, // { guildId, channelId, freshnessMs, requiredPhrases, targetId, senderName, senderUserId, paused }
    route: { guildId: null, channelId: null },
    listEl: null,
    observer: null,
    tracked: new Map(), // messageId -> { baseline, triggered, matched }
    baselineMaxSnowflake: null,
    lastStatus: null,
    lastSkip: null,
    dead: false,
    routeTimer: null,
    bodyObserver: null,
  };

  // ------------------------------------------------------------ messaging
  function send(msg) {
    if (state.dead) return Promise.resolve(null);
    try {
      return chrome.runtime.sendMessage(msg).catch((err) => {
        if (String(err && err.message).includes("Extension context invalidated")) shutdown();
        return null;
      });
    } catch (err) {
      // chrome.runtime is gone after an extension reload/update.
      shutdown();
      return Promise.resolve(null);
    }
  }

  function shutdown() {
    if (state.dead) return;
    state.dead = true;
    detach();
    if (state.routeTimer) clearInterval(state.routeTimer);
    if (state.bodyObserver) state.bodyObserver.disconnect();
  }

  function reportStatus(extra) {
    // Skip reasons are sticky so the popup can show why the last candidate was dropped;
    // a plain reportStatus() 500 ms later must not erase them.
    if (extra && extra.lastSkip) state.lastSkip = `${new Date().toLocaleTimeString()} ${extra.lastSkip}`;
    const onChannel = isOnConfiguredChannel();
    const status = {
      lastSkip: state.lastSkip,
      type: "tab-status",
      route: state.route,
      onChannel,
      attached: !!state.listEl,
      monitoring: !!state.listEl && onChannel && !(state.config && state.config.paused),
      paused: !!(state.config && state.config.paused),
      trackedCount: state.tracked.size,
      configured: !!(state.config && state.config.channelId),
      ...(extra || {}),
    };
    const key = JSON.stringify(status);
    if (key !== state.lastStatus) {
      state.lastStatus = key;
      send(status);
    }
  }

  // ---------------------------------------------------------------- route
  function parseRoute() {
    const m = SEL.routePattern.exec(location.pathname);
    return m ? { guildId: m[1], channelId: m[2] } : { guildId: null, channelId: null };
  }

  function isOnConfiguredChannel() {
    return !!(
      state.config &&
      state.config.channelId &&
      state.route.channelId === state.config.channelId &&
      (!state.config.guildId || state.route.guildId === state.config.guildId)
    );
  }

  function checkRoute() {
    const route = parseRoute();
    if (route.channelId !== state.route.channelId || route.guildId !== state.route.guildId) {
      state.route = route;
      // Channel changed: the list will be replaced; drop attachment and tracking.
      detach();
      state.tracked.clear();
      state.baselineMaxSnowflake = null;
    }
    if (isOnConfiguredChannel()) ensureAttached();
    else detach();
    reportStatus();
  }

  // ----------------------------------------------------------- attachment
  function findList() {
    for (const sel of SEL.messageList) {
      const el = document.querySelector(sel);
      if (el) return el;
    }
    return null;
  }

  function detach() {
    if (state.observer) {
      state.observer.disconnect();
      state.observer = null;
    }
    state.listEl = null;
  }

  function ensureAttached() {
    const el = findList();
    if (!el) {
      if (state.listEl) detach();
      return;
    }
    if (el === state.listEl && state.observer) return;
    detach();
    state.listEl = el;
    baselineExisting(el);
    state.observer = new MutationObserver(onMutations);
    state.observer.observe(el, { childList: true, subtree: true, characterData: true });
  }

  function baselineExisting(listEl) {
    // Everything already rendered when we attach is history: never triggerable.
    const items = listEl.querySelectorAll(SEL.messageItem);
    for (const li of items) {
      const ids = parseMessageIds(li);
      if (!ids) continue;
      track(ids.messageId, { baseline: true, triggered: false, matched: false });
      const snow = M.snowflakeToMs(ids.messageId);
      if (snow && (!state.baselineMaxSnowflake || snow > state.baselineMaxSnowflake)) {
        state.baselineMaxSnowflake = snow;
      }
    }
  }

  function track(messageId, entry) {
    if (state.tracked.size >= TRACKED_LIMIT) {
      // Drop the oldest entries; they are far outside any freshness window anyway.
      const it = state.tracked.keys();
      for (let i = 0; i < 200; i++) {
        const k = it.next();
        if (k.done) break;
        state.tracked.delete(k.value);
      }
    }
    state.tracked.set(messageId, entry);
  }

  // ------------------------------------------------------------ mutations
  function onMutations(records) {
    if (state.dead || !state.config || state.config.paused) return;
    if (!isOnConfiguredChannel()) return;
    if (!state.listEl || !state.listEl.isConnected) {
      // Discord swapped the list (reconnect / navigation). Re-attach and re-baseline.
      detach();
      ensureAttached();
      reportStatus();
      return;
    }
    const affected = new Set();
    for (const rec of records) {
      const target = rec.target && rec.target.nodeType === 1 ? rec.target : rec.target && rec.target.parentElement;
      if (target) {
        const li = target.closest ? target.closest(SEL.messageItem) : null;
        if (li) affected.add(li);
      }
      for (const node of rec.addedNodes) {
        if (node.nodeType !== 1) continue;
        if (node.matches && node.matches(SEL.messageItem)) affected.add(node);
        else if (node.querySelectorAll) {
          for (const li of node.querySelectorAll(SEL.messageItem)) affected.add(li);
        }
      }
    }
    const detectedWallMs = Date.now();
    for (const li of affected) evaluateMessage(li, detectedWallMs);
  }

  // ----------------------------------------------------------- evaluation
  function parseMessageIds(li) {
    const m = SEL.messageIdPattern.exec(li.id || "");
    return m ? { channelId: m[1], messageId: m[2] } : null;
  }

  function collectText(li) {
    const parts = [];
    const content = li.querySelector(SEL.content);
    if (content) parts.push(content.textContent || "");
    const acc = li.querySelector(SEL.accessories);
    if (acc) {
      // Embeds render late; read whatever is present now, re-evaluated on later mutations.
      let sawParts = false;
      for (const sel of SEL.embedTextParts) {
        for (const el of acc.querySelectorAll(sel)) {
          sawParts = true;
          parts.push(el.textContent || "");
        }
      }
      if (!sawParts) parts.push(acc.textContent || "");
    }
    return parts;
  }

  function readTimestampMs(li) {
    const t = li.querySelector(SEL.timestamp);
    if (!t) return null;
    const iso = t.getAttribute("datetime");
    if (!iso) return null;
    const ms = Date.parse(iso);
    return Number.isFinite(ms) ? ms : null;
  }

  function readSenderName(li) {
    const header = li.querySelector(SEL.username);
    if (!header) return null;
    // Live Discord: <span id="message-username-…"><span class="username_…" data-text="Name">Name</span>
    // <span class="botTag…">APP</span></span>. header.textContent would read "NameAPP".
    const inner = header.querySelector(SEL.usernameInner);
    const dataText = (inner || header).getAttribute("data-text");
    if (dataText && dataText.trim()) return dataText.trim();
    const clone = (inner || header).cloneNode(true);
    clone.querySelectorAll(SEL.botTag).forEach((n) => n.remove());
    const text = (clone.textContent || "").trim();
    return text || null;
  }

  function readSender(li) {
    const avatar = li.querySelector(SEL.avatar);
    let userId = null;
    if (avatar) {
      const m = SEL.avatarUserIdPattern.exec(avatar.getAttribute("src") || "");
      if (m) userId = m[1];
    }
    return {
      name: readSenderName(li),
      userId,
      isBot: !!li.querySelector(SEL.botTag),
    };
  }

  function evaluateMessage(li, detectedWallMs) {
    const ids = parseMessageIds(li);
    if (!ids) return;
    if (ids.channelId !== state.config.channelId) return; // channel mismatch: ignore
    if (li.querySelector(SEL.sending)) return;

    let entry = state.tracked.get(ids.messageId);
    if (!entry) {
      entry = { baseline: false, triggered: false, matched: false };
      track(ids.messageId, entry);
    }
    if (entry.baseline || entry.triggered) return;

    // Virtualized DOM reuse / scroll-back: anything at or before the startup high-water
    // mark is history regardless of when it re-renders.
    const snow = M.snowflakeToMs(ids.messageId);
    if (state.baselineMaxSnowflake && snow && snow <= state.baselineMaxSnowflake) {
      entry.baseline = true;
      return;
    }

    const texts = collectText(li);
    if (!M.matches(texts, state.config.requiredPhrases)) return; // may match after embed arrives
    entry.matched = true;

    // Freshness from the rendered timestamp; fail closed when absent.
    const messageTsMs = readTimestampMs(li);
    if (messageTsMs === null) {
      reportStatus({ lastSkip: "no rendered timestamp; failing closed" });
      return;
    }
    const ageMs = detectedWallMs - messageTsMs;
    if (ageMs > state.config.freshnessMs) {
      entry.baseline = true; // stale: never trigger later either
      reportStatus({ lastSkip: `stale message (${Math.round(ageMs / 1000)}s old)` });
      return;
    }
    if (snow && Math.abs(snow - messageTsMs) > 5 * 60 * 1000) {
      reportStatus({ lastSkip: "timestamp/snowflake disagree; failing closed" });
      return;
    }

    // Optional sender identification. A display name alone does not prove identity;
    // require the user id when configured, and fail closed when it is not visible.
    const sender = readSender(li);
    if (state.config.senderUserId) {
      if (!sender.userId) {
        reportStatus({
          lastSkip: "sender user id not visible (default-avatar app or grouped message); "
            + "failing closed — clear 'Sender user id' in Options to rely on display name",
        });
        return;
      }
      if (sender.userId !== state.config.senderUserId) return;
    }
    if (state.config.senderName) {
      const want = M.normalizeText(state.config.senderName);
      if (!sender.name || M.normalizeText(sender.name) !== want) return;
    }

    entry.triggered = true; // at most one trigger per message
    const joined = texts.join(" ");
    send({
      type: "candidate",
      candidate: {
        event_id: `${ids.channelId}-${ids.messageId}`,
        message_id: ids.messageId,
        channel_id: ids.channelId,
        guild_id: state.route.guildId || state.config.guildId || "0",
        matched_target: state.config.targetId,
        message_ts_ms: messageTsMs,
        detected_wall_ms: detectedWallMs,
        // Telemetry only; never used for purchasing decisions.
        alert_price_text: M.extractAlertPriceText(joined),
      },
    });
    reportStatus({ lastMatch: { messageId: ids.messageId, at: detectedWallMs } });
  }

  // ------------------------------------------------------------- config
  function applyConfig(cfg) {
    const prevChannel = state.config && state.config.channelId;
    state.config = cfg || null;
    if (state.config && state.config.channelId !== prevChannel) {
      detach();
      state.tracked.clear();
      state.baselineMaxSnowflake = null;
    }
    checkRoute();
  }

  async function init() {
    const cfg = await send({ type: "get-config" });
    applyConfig(cfg && cfg.config);
    state.routeTimer = setInterval(() => {
      if (state.dead) return;
      checkRoute();
    }, ROUTE_POLL_MS);
    // Detect list replacement quickly (reconnects, channel switches) without polling hard.
    state.bodyObserver = new MutationObserver(() => {
      if (state.dead || !isOnConfiguredChannel()) return;
      if (!state.listEl || !state.listEl.isConnected) {
        detach();
        ensureAttached();
        reportStatus();
      }
    });
    state.bodyObserver.observe(document.body, { childList: true, subtree: true });
    checkRoute();
  }

  try {
    chrome.runtime.onMessage.addListener((msg) => {
      if (!msg || state.dead) return;
      if (msg.type === "config-updated") applyConfig(msg.config);
      if (msg.type === "request-status") reportStatus();
    });
  } catch (e) {
    /* no runtime: nothing to do */
  }

  // Exposed for the local fixture harness only (tests/test_extension_*.py).
  globalThis.__ocarinaTest = { state, checkRoute, ensureAttached, applyConfig, evaluateMessage };

  init();
})();
