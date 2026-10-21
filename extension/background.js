// Lampline Phone Bridge: the background worker.
//
// A Lampline page (on this computer) asks for an eval call: we open Google Voice with the number filled in and
// remember, for that one tab, which engine and which one-call token it belongs to. The Google Voice tab's page script
// sends its WebRTC offer through us, so the token never enters the Google Voice page and the engine sees our request
// with its own token instead of an API key. Tabs we didn't open are never bridged.

const ENGINE = /^http:\/\/(localhost|127\.0\.0\.1)(:\d{1,5})?$/;
const VOICE = /^https:\/\/voice\.google\.com\/u\/\d+\/calls\?a=nc,%2B\d{7,15}$/;

async function bridges() {
  return (await chrome.storage.session.get("bridges")).bridges || {};
}
async function saveBridges(b) {
  await chrome.storage.session.set({ bridges: b });
}

async function notify(bridge, status) {
  if (!bridge || bridge.lamplineTab == null) return;
  try {
    await chrome.tabs.sendMessage(bridge.lamplineTab, { type: "bridge:status", runId: bridge.runId, ...status });
  } catch (_) { /* the Lampline tab was closed */ }
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  (async () => {
    const tabId = sender.tab && sender.tab.id;
    if (msg.type === "start") {
      // From the Lampline page's relay (content script on localhost only).
      if (!ENGINE.test(msg.engine || "") || !VOICE.test(msg.url || "") || typeof msg.token !== "string" || !msg.runId) {
        return reply({ ok: false, error: "the extension refused this request" });
      }
      const tab = await chrome.tabs.create({ url: msg.url, active: true });
      const b = await bridges();
      b[tab.id] = { engine: msg.engine, token: msg.token, runId: msg.runId, lamplineTab: tabId, created: Date.now() };
      await saveBridges(b);
      return reply({ ok: true });
    }
    const b = await bridges();
    const bridge = tabId != null ? b[tabId] : null;
    if (msg.type === "bridge:get") {
      return reply(bridge ? { active: true, runId: bridge.runId } : { active: false });
    }
    if (!bridge) return reply({ ok: false, error: "this tab isn't bridged" });
    if (msg.type === "bridge:offer") {
      if (!bridge.token) return reply({ ok: false, error: "this bridge already started its call; start a new eval in Lampline" });
      try {
        const r = await fetch(`${bridge.engine}/v1/evals/bridge/offer`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "X-Lampline-Bridge": bridge.token },
          body: JSON.stringify({ sdp: msg.sdp, type: msg.type_ || "offer" }),
        });
        const data = await r.json().catch(() => ({}));
        bridge.token = null;                      // one call per token, whatever happened
        b[tabId] = bridge;
        await saveBridges(b);
        if (!r.ok) {
          const error = typeof data.detail === "string" ? data.detail : `the engine answered ${r.status}`;
          await notify(bridge, { state: "error", detail: error });
          return reply({ ok: false, error });
        }
        await notify(bridge, { state: "connected", detail: "The Test Caller is on the call" });
        return reply({ ok: true, answer: { sdp: data.sdp, type: data.type } });
      } catch (e) {
        const error = `can't reach Lampline at ${bridge.engine} (${e})`;
        await notify(bridge, { state: "error", detail: error });
        return reply({ ok: false, error });
      }
    }
    if (msg.type === "bridge:status") {
      await notify(bridge, { state: String(msg.state || ""), detail: String(msg.detail || "").slice(0, 300) });
      return reply({ ok: true });
    }
    reply({ ok: false, error: "unknown message" });
  })();
  return true;                                    // reply asynchronously
});

chrome.tabs.onRemoved.addListener(async (tabId) => {
  const b = await bridges();
  if (b[tabId]) {
    await notify(b[tabId], { state: "closed", detail: "The Google Voice tab was closed" });
    delete b[tabId];
    await saveBridges(b);
  }
});
