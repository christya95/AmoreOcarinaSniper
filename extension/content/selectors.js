/*
 * Centralized Discord DOM selectors.
 *
 * Verification status: LIVE-VERIFIED 2026-09-24 on discord.com (Chromium/Comet) against the
 * target channel, including a real alert message from the configured app author:
 * ol[data-list-id="chat-messages"], li#chat-messages-<channel>-<message>,
 * #message-content-<id>, #message-accessories-<id>, time[datetime], embedTitle/embedDescription,
 * span#message-username-<id> > span.username_*[data-text] + span.botTag*.
 * The id-based selectors are Discord's stable accessibility ids and are far less brittle
 * than hashed class names. Class-substring selectors are last-resort fallbacks.
 *
 * Verify live: open a channel, run in DevTools
 *   document.querySelector('ol[data-list-id="chat-messages"] li[id^="chat-messages-"]')
 * and confirm ids/timestamps before arming anything. See README "Discord DOM check".
 */
(function (root) {
  "use strict";

  root.OcarinaSelectors = {
    // Ordered candidates for the message list container.
    messageList: [
      'ol[data-list-id="chat-messages"]',
      'main [role="list"][data-list-id^="chat-messages"]',
      'div[class*="scrollerInner"]',
    ],
    messageItem: 'li[id^="chat-messages-"]',
    // chat-messages-<channelId>-<messageId>
    messageIdPattern: /^chat-messages-(\d{5,25})-(\d{5,25})$/,
    content: 'div[id^="message-content-"]',
    accessories: 'div[id^="message-accessories-"]',
    embedTextParts: [
      '[class*="embedTitle"]',
      '[class*="embedDescription"]',
      '[class*="embedFieldName"]',
      '[class*="embedFieldValue"]',
      '[class*="embedAuthor"]',
      '[class*="embedFooter"]',
      '[class*="embedProvider"]',
    ],
    timestamp: 'time[id^="message-timestamp-"][datetime], time[datetime]',
    // Header span; its textContent includes badge text (e.g. "LbabinzAPP"), so the
    // display name is read from usernameInner / data-text with badges stripped.
    username: 'span[id^="message-username-"], [id^="message-username-"], [class*="username"]',
    usernameInner: '[class*="username"][data-text], [class*="username"]',
    // Only present when the author has a custom avatar; users/apps with a default avatar
    // render /assets/<hash>.png and expose NO user id anywhere in the message DOM.
    avatar: 'img[src*="/avatars/"]',
    avatarUserIdPattern: /\/avatars\/(\d{5,25})\//,
    botTag: '[class*="botTag"]',
    sending: '[class*="isSending"]',
    // Route: /channels/<guildId>/<channelId>[/<messageId>]
    routePattern: /^\/channels\/(\d{5,25}|@me)\/(\d{5,25})/,
  };
})(typeof globalThis !== "undefined" ? globalThis : self);
