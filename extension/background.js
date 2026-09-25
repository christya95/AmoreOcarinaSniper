/*
 * Service worker: holds the pairing secret, deduplicates candidates durably, and
 * delivers triggers to the local Python bridge with bounded retries.
 *
 * MV3 workers are suspended at will, so nothing here relies on in-memory globals
 * surviving between events: all state lives in chrome.storage.
 */
importScripts("shared/matcher.js");

const BRIDGE_HOST = "127.0.0.1";
const SCHEMA = 1;
const MAX_ATTEMPTS = 5;
const RETRY_BACKOFF_MS = [1000, 2000, 4000, 8000];
const SENT_EVENTS_LIMIT = 300;
const HEALTH_ALARM = "ocarina-health";
const SWEEP_ALARM = "ocarina-retry-sweep";

const DEFAULT_CONFIG = {
  guildId: "",
  channelId: "",
  bridgePort: 48620,
  pairingSecret: "",
  targetId: "switch2-zelda-40th",
  freshnessSec: 90,
  senderName: "",
  senderUserId: "",
  requiredPhrases: globalThis.OcarinaMatcher.DEFAULT_REQUIRED_PHRASES,
  paused: false,
};

// ------------------------------------------------------------------ storage
async function getConfig() {
  const { config } = await chrome.storage.local.get("config");
  return { ...DEFAULT_CONFIG, ...(config || {}) };
}

function contentConfig(cfg) {
  // What the content script (page-adjacent) is allowed to know: never the secret/port.
  return {
    guildId: cfg.guildId,
    channelId: cfg.channelId,
    freshnessMs: Math.max(5, Number(cfg.freshnessSec) || 90) * 1000,
    requiredPhrases: cfg.requiredPhrases,
    targetId: cfg.targetId,
    senderName: cfg.senderName,
    senderUserId: cfg.senderUserId,
    paused: !!cfg.paused,
  };
}

async function getSentEvents() {
  const { sentEvents } = await chrome.storage.local.get("sentEvents");
  return sentEvents || {};
}

async function saveSentEvents(map) {
  const keys = Object.keys(map);
  if (keys.length > SENT_EVENTS_LIMIT) {
    keys
      .sort((a, b) => (map[a].firstSeen || 0) - (map[b].firstSeen || 0))
      .slice(0, keys.length - SENT_EVENTS_LIMIT)
      .forEach((k) => delete map[k]);
  }
  await chrome.storage.local.set({ sentEvents: map });
}

async function setStatus(patch) {
  const { bridgeStatus } = await chrome.storage.local.get("bridgeStatus");
  await chrome.storage.local.set({ bridgeStatus: { ...(bridgeStatus || {}), ...patch } });
}

// ------------------------------------------------------------------- bridge
function bridgeUrl(cfg, path) {
  return `http://${BRIDGE_HOST}:${Number(cfg.bridgePort) || 48620}${path}`;
}

async function bridgeFetch(cfg, path, init) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 4000);
  try {
    return await fetch(bridgeUrl(cfg, path), {
      ...init,
      signal: controller.signal,
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${cfg.pairingSecret}`,
        ...((init && init.headers) || {}),
      },
    });
  } finally {
    clearTimeout(timer);
  }
}

async function checkBridge() {
  const cfg = await getConfig();
  if (!cfg.pairingSecret) {
    await setStatus({ connected: false, error: "no pairing secret configured", checkedAt: Date.now() });
    return;
  }
  try {
    const res = await bridgeFetch(cfg, "/v1/status", { method: "GET" });
    if (res.ok) {
      const body = await res.json();
      await setStatus({ connected: true, error: null, python: body, checkedAt: Date.now() });
    } else {
      await setStatus({ connected: false, error: `HTTP ${res.status}`, python: null, checkedAt: Date.now() });
    }
  } catch (err) {
    await setStatus({ connected: false, error: String(err && err.message || err), python: null, checkedAt: Date.now() });
  }
}

// ---------------------------------------------------------------- delivery
function buildPayload(cfg, record, attempt) {
  return {
    schema: SCHEMA,
    event_id: record.event_id,
    message_id: record.message_id,
    channel_id: record.channel_id,
    guild_id: record.guild_id,
    matched_target: cfg.targetId,
    message_ts_ms: record.message_ts_ms,
    sent_at_ms: Date.now(),
    // Same host wall clock on both sides; monotonic clocks are never mixed across contexts.
    detected_at_offset_ms: Math.max(0, Date.now() - record.detected_wall_ms),
    attempt,
    source: "extension",
  };
}

async function deliver(eventId) {
  const cfg = await getConfig();
  const events = await getSentEvents();
  const rec = events[eventId];
  if (!rec || rec.status === "acked" || rec.status === "terminal") return rec && rec.status;
  if (!cfg.pairingSecret) {
    rec.status = "terminal";
    rec.lastError = "no pairing secret configured";
    await saveSentEvents(events);
    return rec.status;
  }
  const attempt = (rec.attempts || 0) + 1;
  rec.attempts = attempt;
  rec.status = "sending";
  await saveSentEvents(events);

  let outcome;
  try {
    const res = await bridgeFetch(cfg, "/v1/trigger", {
      method: "POST",
      body: JSON.stringify(buildPayload(cfg, rec, attempt)),
    });
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; }
    if (res.ok && body && body.ack) {
      outcome = { status: "acked", detail: body.disposition || "accepted" };
    } else if (res.status >= 400 && res.status < 500) {
      outcome = { status: "terminal", detail: `HTTP ${res.status} ${(body && body.code) || ""} ${(body && body.detail) || ""}`.trim() };
    } else {
      outcome = { status: "retry", detail: `HTTP ${res.status}` };
    }
  } catch (err) {
    outcome = { status: "retry", detail: String(err && err.message || err) };
  }

  const fresh = await getSentEvents();
  const cur = fresh[eventId] || rec;
  cur.lastError = outcome.status === "acked" ? null : outcome.detail;
  cur.lastAttemptAt = Date.now();
  if (outcome.status === "retry" && attempt < MAX_ATTEMPTS) {
    cur.status = "pending";
    const delay = RETRY_BACKOFF_MS[Math.min(attempt - 1, RETRY_BACKOFF_MS.length - 1)];
    cur.nextRetryAt = Date.now() + delay;
    fresh[eventId] = cur;
    await saveSentEvents(fresh);
    // In-worker timer for fast retries; the sweep alarm is the safety net if we get suspended.
    setTimeout(() => deliver(eventId), delay);
    return cur.status;
  }
  cur.status = outcome.status === "acked" ? "acked" : "terminal";
  cur.nextRetryAt = null;
  fresh[eventId] = cur;
  await saveSentEvents(fresh);
  await chrome.storage.local.set({
    lastMatch: {
      event_id: eventId,
      message_id: cur.message_id,
      channel_id: cur.channel_id,
      at: cur.firstSeen,
      delivery: cur.status,
      detail: outcome.detail,
      alert_price_text: cur.alert_price_text || null,
    },
  });
  return cur.status;
}

async function handleCandidate(candidate, sender) {
  const cfg = await getConfig();
  if (cfg.paused) return { accepted: false, reason: "paused" };
  if (!candidate || typeof candidate !== "object") return { accepted: false, reason: "bad candidate" };
  if (candidate.channel_id !== cfg.channelId) return { accepted: false, reason: "channel mismatch" };
  if (candidate.matched_target !== cfg.targetId) return { accepted: false, reason: "target mismatch" };
  const senderUrl = (sender && sender.url) || "";
  if (!senderUrl.startsWith("https://discord.com/channels/")) return { accepted: false, reason: "unexpected sender" };

  const events = await getSentEvents();
  const eventId = String(candidate.event_id || "");
  if (!/^[A-Za-z0-9_-]{8,128}$/.test(eventId)) return { accepted: false, reason: "bad event id" };
  if (events[eventId]) return { accepted: false, reason: `duplicate (${events[eventId].status})` };

  events[eventId] = {
    event_id: eventId,
    message_id: String(candidate.message_id),
    channel_id: String(candidate.channel_id),
    guild_id: String(candidate.guild_id || "0"),
    message_ts_ms: Number(candidate.message_ts_ms),
    detected_wall_ms: Number(candidate.detected_wall_ms) || Date.now(),
    alert_price_text: candidate.alert_price_text || null,
    firstSeen: Date.now(),
    attempts: 0,
    status: "pending",
  };
  await saveSentEvents(events);
  const status = await deliver(eventId);
  return { accepted: true, status };
}

async function sweepPending() {
  const cfg = await getConfig();
  const events = await getSentEvents();
  const now = Date.now();
  const freshnessMs = (Number(cfg.freshnessSec) || 90) * 1000;
  for (const [id, rec] of Object.entries(events)) {
    if (rec.status !== "pending" && rec.status !== "sending") continue;
    if (now - rec.message_ts_ms > freshnessMs + 30000) {
      rec.status = "terminal";
      rec.lastError = "expired before delivery";
      continue;
    }
    if (!rec.nextRetryAt || rec.nextRetryAt <= now) deliver(id);
  }
  await saveSentEvents(events);
}

// ------------------------------------------------------------------ status
const tabStatus = new Map(); // ephemeral; popup re-requests on open

async function popupState() {
  const cfg = await getConfig();
  const { bridgeStatus, lastMatch } = await chrome.storage.local.get(["bridgeStatus", "lastMatch"]);
  let active = null;
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (tab) {
      let status = tabStatus.get(tab.id) || null;
      if (tab.id != null && (tab.url || "").startsWith("https://discord.com/")) {
        // Pull, don't rely on the cache: this worker may have just been restarted by MV3,
        // and the content script only pushes when its status changes.
        try {
          const fresh = await chrome.tabs.sendMessage(tab.id, { type: "request-status" });
          if (fresh && fresh.type === "tab-status") {
            status = { ...fresh, at: Date.now() };
            tabStatus.set(tab.id, status);
          }
        } catch (e) { /* no content script in this tab (needs reload after install) */ }
      }
      active = { tabId: tab.id, url: tab.url || "", status };
    }
  } catch (e) { /* tabs permission not granted: fine */ }
  return {
    configured: !!(cfg.channelId && cfg.pairingSecret),
    paused: !!cfg.paused,
    channelId: cfg.channelId,
    bridge: bridgeStatus || { connected: false, error: "not checked yet" },
    lastMatch: lastMatch || null,
    activeTab: active,
  };
}

async function broadcastConfig() {
  const cfg = await getConfig();
  const tabs = await chrome.tabs.query({ url: "https://discord.com/channels/*" }).catch(() => []);
  for (const t of tabs) {
    chrome.tabs.sendMessage(t.id, { type: "config-updated", config: contentConfig(cfg) }).catch(() => {});
  }
}

// ---------------------------------------------------------------- wiring
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    if (!msg || typeof msg !== "object") return sendResponse(null);
    switch (msg.type) {
      case "get-config":
        return sendResponse({ config: contentConfig(await getConfig()) });
      case "candidate":
        return sendResponse(await handleCandidate(msg.candidate, sender));
      case "tab-status":
        if (sender.tab && sender.tab.id != null) tabStatus.set(sender.tab.id, { ...msg, at: Date.now() });
        return sendResponse({ ok: true });
      case "get-popup-state":
        return sendResponse(await popupState());
      case "set-paused": {
        const cfg = await getConfig();
        cfg.paused = !!msg.paused;
        await chrome.storage.local.set({ config: cfg });
        await broadcastConfig();
        return sendResponse({ ok: true, paused: cfg.paused });
      }
      case "config-saved":
        await broadcastConfig();
        await checkBridge();
        return sendResponse({ ok: true });
      case "check-bridge":
        await checkBridge();
        return sendResponse(await popupState());
      default:
        return sendResponse(null);
    }
  })().catch((err) => sendResponse({ error: String(err && err.message || err) }));
  return true; // async response
});

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === HEALTH_ALARM) checkBridge();
  if (alarm.name === SWEEP_ALARM) sweepPending();
});

function ensureAlarms() {
  chrome.alarms.create(HEALTH_ALARM, { periodInMinutes: 1 });
  chrome.alarms.create(SWEEP_ALARM, { periodInMinutes: 1 });
}

chrome.runtime.onInstalled.addListener(() => { ensureAlarms(); checkBridge(); });
chrome.runtime.onStartup.addListener(() => { ensureAlarms(); checkBridge(); sweepPending(); });
chrome.tabs.onRemoved.addListener((tabId) => tabStatus.delete(tabId));
ensureAlarms();

// Exposed for the local harness (tests/test_extension_background.py); harmless in Chrome.
globalThis.__ocarinaBackground = { handleCandidate, deliver, sweepPending, checkBridge, buildPayload, getSentEvents };
