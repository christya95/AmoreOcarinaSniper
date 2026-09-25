/*
 * Centralized Discord DOM selectors.
 *
 * Verification status: NOT LIVE-VERIFIED in this environment (Discord requires an
 * authenticated session). The id-based selectors below (chat-messages-<channel>-<message>,
 * message-content-<id>, message-accessories-<id>, message-timestamp-<id>,
 * message-username-<id>) are Discord's stable accessibility ids and are far less
 * brittle than hashed class names. Class-substring selectors are last-resort fallbacks.
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
    username: 'span[id^="message-username-"], [id^="message-username-"], [class*="username"]',
    avatar: 'img[src*="/avatars/"]',
    avatarUserIdPattern: /\/avatars\/(\d{5,25})\//,
    botTag: '[class*="botTag"]',
    sending: '[class*="isSending"]',
    // Route: /channels/<guildId>/<channelId>[/<messageId>]
    routePattern: /^\/channels\/(\d{5,25}|@me)\/(\d{5,25})/,
  };
})(typeof globalThis !== "undefined" ? globalThis : self);
