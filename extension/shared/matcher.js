/*
 * Alert text matcher shared by the content script and the service worker.
 * Mirrors src/amore_ocarina_sniper/matching.py — keep the two in lock-step.
 * Classic script (no modules) so it can be listed in content_scripts and pulled into
 * the service worker with importScripts().
 */
(function (root) {
  "use strict";

  const DEFAULT_REQUIRED_PHRASES = [
    "nintendo switch 2",
    "legend of zelda",
    "40th anniversary edition",
    "has been found",
  ];

  // Trademark/copyright marks carry no meaning; dashes and quotes vary by client.
  const STRIP_SYMBOLS = /[\u2122\u00ae\u00a9\u2120]/gu;
  // Python: [^\w\s$.] with Unicode \w (alnum + underscore)  ==  JS: [^\p{L}\p{N}_\s$.]
  const PUNCT = /[^\p{L}\p{N}_\s$.]/gu;
  const WS = /\s+/g;

  function normalizeText(text) {
    if (!text) return "";
    // Strip marks *before* NFKC, which would otherwise expand "™" into "TM".
    let value = String(text).replace(STRIP_SYMBOLS, " ");
    value = value.normalize("NFKC");
    value = value.toLowerCase();
    value = value.replace(PUNCT, " ");
    value = value.replace(WS, " ").trim();
    return value;
  }

  function matches(texts, requiredPhrases, excludedPhrases) {
    const required = (requiredPhrases && requiredPhrases.length)
      ? requiredPhrases
      : DEFAULT_REQUIRED_PHRASES;
    const parts = (Array.isArray(texts) ? texts : [texts]).filter(Boolean).map(normalizeText);
    const haystack = parts.join(" ").trim();
    if (!haystack) return false;
    for (const phrase of excludedPhrases || []) {
      if (haystack.includes(normalizeText(phrase))) return false;
    }
    return required.every((p) => haystack.includes(normalizeText(p)));
  }

  const PRICE_RE = /\$\s?(\d{1,3}(?:,\d{3})*|\d+)(?:\.(\d{2}))?/;
  function extractAlertPriceText(text) {
    const m = PRICE_RE.exec(text || "");
    if (!m) return null;
    return m[2] ? `$${m[1]}.${m[2]}` : `$${m[1]}`;
  }

  // Discord snowflake -> epoch ms (used only as a sanity cross-check, never as the
  // primary freshness source; the rendered <time datetime> is authoritative).
  function snowflakeToMs(id) {
    try {
      return Number((BigInt(id) >> 22n) + 1420070400000n);
    } catch (e) {
      return null;
    }
  }

  root.OcarinaMatcher = {
    DEFAULT_REQUIRED_PHRASES,
    normalizeText,
    matches,
    extractAlertPriceText,
    snowflakeToMs,
  };
})(typeof globalThis !== "undefined" ? globalThis : self);
