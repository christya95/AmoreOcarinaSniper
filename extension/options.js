(function () {
  "use strict";
  const FIELDS = ["guildId", "channelId", "bridgePort", "pairingSecret", "targetId", "freshnessSec", "senderName", "senderUserId"];
  const $ = (id) => document.getElementById(id);

  async function load() {
    const { config } = await chrome.storage.local.get("config");
    const cfg = config || {};
    $("guildId").value = cfg.guildId || "";
    $("channelId").value = cfg.channelId || "";
    $("bridgePort").value = cfg.bridgePort || 48620;
    $("pairingSecret").value = cfg.pairingSecret || "";
    $("targetId").value = cfg.targetId || "switch2-zelda-40th";
    $("freshnessSec").value = cfg.freshnessSec || 90;
    $("senderName").value = cfg.senderName || "";
    $("senderUserId").value = cfg.senderUserId || "";
  }

  function validate(cfg) {
    if (!/^\d{5,25}$/.test(cfg.channelId)) return "channel id must be 5-25 digits";
    if (cfg.guildId && !/^\d{5,25}$/.test(cfg.guildId)) return "guild id must be digits or empty";
    if (cfg.senderUserId && !/^\d{5,25}$/.test(cfg.senderUserId)) return "sender user id must be digits or empty";
    if (!(cfg.bridgePort >= 1024 && cfg.bridgePort <= 65535)) return "bridge port out of range";
    if (cfg.pairingSecret.length < 32) return "pairing secret looks too short";
    if (!cfg.targetId) return "target id required";
    if (!(cfg.freshnessSec >= 5 && cfg.freshnessSec <= 600)) return "freshness must be 5-600 seconds";
    return null;
  }

  async function save() {
    const { config } = await chrome.storage.local.get("config");
    const cfg = { ...(config || {}) };
    for (const f of FIELDS) cfg[f] = $(f).value.trim();
    cfg.bridgePort = Number(cfg.bridgePort);
    cfg.freshnessSec = Number(cfg.freshnessSec);
    const err = validate(cfg);
    if (err) { $("msg").textContent = "Not saved: " + err; return; }
    await chrome.storage.local.set({ config: cfg });
    await chrome.runtime.sendMessage({ type: "config-saved" });
    $("msg").textContent = "Saved.";
  }

  async function test() {
    $("msg").textContent = "Testing…";
    const s = await chrome.runtime.sendMessage({ type: "check-bridge" });
    if (s && s.bridge && s.bridge.connected) {
      const py = s.bridge.python || {};
      $("msg").textContent = `Bridge OK — Python state ${py.state}, ${py.dry_run ? "dry-run" : "LIVE"}.`;
    } else {
      $("msg").textContent = `Bridge unreachable: ${(s && s.bridge && s.bridge.error) || "unknown"}. Is \`ocarina run\` running?`;
    }
  }

  $("save").addEventListener("click", save);
  $("test").addEventListener("click", test);
  load();
})();
