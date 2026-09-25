(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);

  function setText(id, text, cls) {
    const el = $(id);
    el.textContent = text;
    el.className = cls || "";
  }

  function render(s) {
    if (!s) return;
    if (!s.configured) {
      setText("bridge", "not configured", "warn");
    } else if (s.bridge && s.bridge.connected) {
      setText("bridge", "connected", "ok");
    } else {
      setText("bridge", `disconnected${s.bridge && s.bridge.error ? " (" + s.bridge.error + ")" : ""}`, "bad");
    }
    const py = s.bridge && s.bridge.python;
    if (py) {
      const bits = [py.state, py.dry_run ? "dry-run" : "LIVE", py.armed ? "armed" : "not armed"];
      if (py.purchase_disabled) bits.push("purchases disabled");
      if (py.kill_switch) bits.push("KILL SWITCH");
      setText("pystate", bits.join(" · "), py.armed && !py.dry_run ? "warn" : "");
    } else {
      setText("pystate", "unknown");
    }
    const tab = s.activeTab;
    const st = tab && tab.status;
    if (!tab || !(tab.url || "").startsWith("https://discord.com/")) {
      setText("tab", "not a Discord tab", "warn");
      setText("channel", "-");
      setText("monitoring", "-");
    } else if (!st) {
      setText("tab", "Discord (no status yet)", "warn");
      setText("channel", "-");
      setText("monitoring", "-");
    } else {
      setText("tab", "Discord");
      setText("channel", st.onChannel ? "correct channel" : "WRONG channel", st.onChannel ? "ok" : "bad");
      const mon = st.paused ? "paused" : st.monitoring ? "monitoring" : st.attached ? "attached, not on channel" : "list not found";
      setText("monitoring", mon, st.paused ? "warn" : st.monitoring ? "ok" : "bad");
    }
    if (s.lastMatch) {
      const when = new Date(s.lastMatch.at || Date.now()).toLocaleTimeString();
      setText("last", `${when} msg ${s.lastMatch.message_id} → ${s.lastMatch.delivery}`);
    } else {
      setText("last", "none");
    }
    // Most recent fail-closed reason from the content script (stale, no timestamp,
    // sender not identifiable, ...). Without this, skipped alerts are invisible.
    setText("skip", (st && st.lastSkip) || "none", st && st.lastSkip ? "warn" : "");
    $("pause").textContent = s.paused ? "Resume" : "Pause";
    $("pause").dataset.paused = s.paused ? "1" : "0";
  }

  function refresh() {
    chrome.runtime.sendMessage({ type: "get-popup-state" }).then(render).catch(() => {});
  }

  $("pause").addEventListener("click", () => {
    const paused = $("pause").dataset.paused === "1";
    chrome.runtime.sendMessage({ type: "set-paused", paused: !paused }).then(refresh);
  });
  $("recheck").addEventListener("click", () => {
    chrome.runtime.sendMessage({ type: "check-bridge" }).then(render).catch(() => {});
  });
  $("options").addEventListener("click", (e) => {
    e.preventDefault();
    chrome.runtime.openOptionsPage();
  });

  refresh();
  chrome.runtime.sendMessage({ type: "check-bridge" }).then(render).catch(() => {});
  // Tab status arrives asynchronously after request-status; poll briefly.
  let n = 0;
  const t = setInterval(() => { refresh(); if (++n > 6) clearInterval(t); }, 400);
})();
