// Lampline Phone Bridge: inside the Google Voice page (main world, before the page's own scripts).
//
// Switched on only for a tab Lampline opened. Then:
//   - what the other side of the call says (Google Voice's incoming audio) goes to Lampline's Test Caller, and
//   - what the Test Caller says becomes this tab's "microphone", so Google Voice sends it down the phone line.
// Both directions stay digital, inside the browser: no speakers, no microphone, no virtual cables.
// Nothing else on the page is read or changed; the person still presses Call themselves.

(() => {
  const NativePC = window.RTCPeerConnection;
  const media = navigator.mediaDevices;
  if (!NativePC || !media || !media.getUserMedia) return;
  const nativeGetUserMedia = media.getUserMedia.bind(media);
  const ORIGIN = location.origin;

  const s = {
    active: false, ctx: null, mic: null, toCaller: null, bridge: null, started: false, ended: false,
    pending: new Map(), keep: [], heard: false,
  };

  function status(state, detail, banner) {
    window.postMessage({ source: "lampline-page", type: "status", state, detail, banner }, ORIGIN);
  }

  window.addEventListener("message", (e) => {
    if (e.source !== window || e.origin !== ORIGIN) return;
    const d = e.data || {};
    if (d.source !== "lampline-relay") return;
    if (d.type === "activate") s.active = true;
    if (d.type === "answer" && s.pending.has(d.id)) {
      s.pending.get(d.id)(d);
      s.pending.delete(d.id);
    }
  });

  function graph() {
    if (s.ctx) return;
    s.ctx = new AudioContext();
    s.mic = s.ctx.createMediaStreamDestination();        // the Test Caller's voice, as Google Voice's microphone
    s.toCaller = s.ctx.createMediaStreamDestination();   // the call's incoming audio, for the Test Caller
    const resume = () => { if (s.ctx.state !== "running") s.ctx.resume().catch(() => {}); };
    resume();
    for (const ev of ["pointerdown", "keydown"]) document.addEventListener(ev, resume, { capture: true });
  }

  // Chrome only feeds a remote WebRTC stream into Web Audio while a media element is playing it.
  function playMuted(stream) {
    const a = document.createElement("audio");
    a.muted = true;
    a.srcObject = stream;
    a.play().catch(() => {});
    s.keep.push(a);
  }

  // Google Voice asks for the microphone: give it the Test Caller's voice.
  media.getUserMedia = async function (constraints) {
    if (!s.active || !constraints || !constraints.audio) return nativeGetUserMedia(constraints);
    graph();
    const tracks = [s.mic.stream.getAudioTracks()[0].clone()];
    if (constraints.video) {
      const cam = await nativeGetUserMedia({ video: constraints.video });
      tracks.push(...cam.getVideoTracks());
    }
    status("microphone", "Google Voice is using the Test Caller as its microphone");
    return new MediaStream(tracks);
  };

  // Google Voice's own connections: take the incoming audio of the call.
  function watch(pc) {
    pc.addEventListener("track", (e) => {
      if (!s.active || e.track.kind !== "audio") return;
      graph();
      const stream = e.streams && e.streams[0] ? e.streams[0] : new MediaStream([e.track]);
      pc.__lampline = true;
      playMuted(stream);
      s.ctx.createMediaStreamSource(stream).connect(s.toCaller);
      status("call-audio", "The call's audio is reaching Lampline");
      start();
    });
    pc.addEventListener("connectionstatechange", () => {
      if (s.active && s.started && pc.__lampline && ["closed", "failed"].includes(pc.connectionState)) finish("The call ended");
    });
    // Hanging up in Google Voice closes its connection, which fires no event: notice the close itself.
    const close = pc.close;
    pc.close = function (...a) {
      if (s.active && s.started && pc.__lampline) finish("The call ended");
      return close.apply(this, a);
    };
  }

  function Wrapped(...args) {
    const pc = new NativePC(...args);
    try { watch(pc); } catch (_) { /* never break the page */ }
    return pc;
  }
  Wrapped.prototype = NativePC.prototype;
  Object.setPrototypeOf(Wrapped, NativePC);
  window.RTCPeerConnection = Wrapped;
  if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = Wrapped;

  function iceDone(pc) {
    return new Promise((resolve) => {
      if (pc.iceGatheringState === "complete") return resolve();
      const check = () => { if (pc.iceGatheringState === "complete") resolve(); };
      pc.addEventListener("icegatheringstatechange", check);
      setTimeout(resolve, 2500);
    });
  }

  function ask(offer) {
    const id = Math.random().toString(36).slice(2);
    return new Promise((resolve) => {
      s.pending.set(id, resolve);
      window.postMessage({ source: "lampline-page", type: "offer", id, sdp: offer.sdp, sdpType: offer.type }, ORIGIN);
    });
  }

  // The connection to Lampline's Test Caller: the call's audio goes up, the Test Caller's voice comes down.
  async function start() {
    if (s.started) return;
    s.started = true;
    try {
      const pc = new NativePC({ iceServers: [] });
      s.bridge = pc;
      const up = s.toCaller.stream.getAudioTracks()[0];
      pc.addTransceiver(up, { direction: "sendrecv", streams: [s.toCaller.stream] });
      pc.createDataChannel("messages", { ordered: true });
      pc.addEventListener("track", (e) => {
        if (e.track.kind !== "audio") return;
        const stream = e.streams && e.streams[0] ? e.streams[0] : new MediaStream([e.track]);
        playMuted(stream);
        s.ctx.createMediaStreamSource(stream).connect(s.mic);
      });
      pc.addEventListener("connectionstatechange", () => {
        if (["disconnected", "failed", "closed"].includes(pc.connectionState)) finish("The Test Caller finished");
      });
      await pc.setLocalDescription(await pc.createOffer());
      await iceDone(pc);
      status("connecting", "Connecting to Lampline");
      const r = await ask(pc.localDescription);
      if (!r.ok) {
        status("error", r.error, "Lampline couldn't join this call: " + r.error);
        return;
      }
      await pc.setRemoteDescription(r.answer);
      status("connected", "The Test Caller is on the call", "Lampline eval running: the Test Caller is talking. Watch it on Lampline's Evals page.");
    } catch (e) {
      status("error", String(e), "Lampline couldn't join this call: " + e);
    }
  }

  function finish(why) {
    if (s.ended) return;
    s.ended = true;
    try { if (s.bridge) s.bridge.close(); } catch (_) {}
    // The Test Caller is done: end the Google Voice call too, if its hang-up button can be found.
    const button = Array.from(document.querySelectorAll("button,[role=button]")).find((b) =>
      /end call|hang ?up/i.test(b.getAttribute("aria-label") || b.getAttribute("title") || ""));
    if (button) try { button.click(); } catch (_) {}
    status("finished", why, "Lampline eval finished. " + (button ? "" : "Hang up this call, then ") + "See the results on Lampline's Evals page.");
  }
})();
