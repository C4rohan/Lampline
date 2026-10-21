// Lampline Phone Bridge: the go-between in a Google Voice tab (the extension's isolated world).
// It asks the background whether Lampline opened this tab; if so it switches the page script on, carries its
// offer and status to the background, and shows a small banner. Tabs Lampline didn't open stay untouched.

(() => {
  const ORIGIN = location.origin;
  let banner = null;

  function show(text, tone) {
    const put = () => {
      if (!banner) {
        banner = document.createElement("div");
        banner.setAttribute("role", "status");
        banner.style.cssText = "position:fixed;z-index:2147483647;left:50%;top:12px;transform:translateX(-50%);" +
          "max-width:min(560px,92vw);padding:10px 16px;border-radius:10px;font:14px/1.4 system-ui,sans-serif;" +
          "box-shadow:0 4px 18px rgba(0,0,0,.25);color:#fff;";
        document.documentElement.appendChild(banner);
      }
      banner.style.background = tone === "bad" ? "#b42318" : tone === "done" ? "#2e6b3a" : "#15212b";
      banner.textContent = text;
    };
    if (document.documentElement) put(); else document.addEventListener("DOMContentLoaded", put, { once: true });
  }

  chrome.runtime.sendMessage({ type: "bridge:get" }, (r) => {
    if (!r || !r.active) return;
    window.postMessage({ source: "lampline-relay", type: "activate", runId: r.runId }, ORIGIN);
    show("Lampline eval: press Call. The Test Caller talks for you on this call; your own microphone isn't used.");
  });

  window.addEventListener("message", (e) => {
    if (e.source !== window || e.origin !== ORIGIN) return;
    const d = e.data || {};
    if (d.source !== "lampline-page") return;
    if (d.type === "offer") {
      chrome.runtime.sendMessage({ type: "bridge:offer", sdp: d.sdp, type_: d.sdpType }, (r) => {
        window.postMessage({ source: "lampline-relay", type: "answer", id: d.id, ok: !!(r && r.ok),
                             answer: r && r.answer, error: (r && r.error) || chrome.runtime.lastError?.message }, ORIGIN);
      });
    }
    if (d.type === "status") {
      chrome.runtime.sendMessage({ type: "bridge:status", state: d.state, detail: d.detail });
      const tone = d.state === "error" ? "bad" : d.state === "finished" ? "done" : "";
      if (d.banner) show(d.banner, tone);
    }
  });
})();
