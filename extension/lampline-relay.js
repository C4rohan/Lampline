// Lampline Phone Bridge: runs on pages at localhost / 127.0.0.1, and acts only on Lampline's own pages.
// It tells the Evals page the extension is installed, and passes "start a Google Voice eval" to the background.

(() => {
  if (!document.querySelector('meta[name="lampline"]')) return;
  const VERSION = chrome.runtime.getManifest().version;
  const post = (data) => window.postMessage({ source: "lampline-bridge", version: VERSION, ...data }, location.origin);

  window.addEventListener("message", (e) => {
    if (e.source !== window || e.origin !== location.origin) return;
    const d = e.data || {};
    if (d.source !== "lampline-ui") return;
    if (d.type === "bridge:ping") post({ type: "bridge:ready" });
    if (d.type === "bridge:start") {
      chrome.runtime.sendMessage(
        { type: "start", engine: location.origin, url: d.url, token: d.token, runId: d.runId },
        (r) => post({ type: "bridge:started", runId: d.runId, ok: !!(r && r.ok), error: (r && r.error) || chrome.runtime.lastError?.message }),
      );
    }
  });
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg && msg.type === "bridge:status") post(msg);
  });
  post({ type: "bridge:ready" });
})();
