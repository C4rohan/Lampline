// End-to-end test of the Lampline Phone Bridge: the real extension in Chromium, the real Evals page, a real engine
// (fake model, tone-based speech), and a stand-in for Google Voice and the far end of the phone call.
//
//   node engine/tests/e2e/bridge.e2e.mjs            (needs Playwright and Chromium; the engine's Python environment)
//
// What it proves, with nothing but browser WebRTC in between:
//   1. the Evals page finds the extension, and Start opens "Google Voice" with the number filled in;
//   2. after Call, what the far end says reaches Lampline's Test Caller (the engine measures it);
//   3. what the Test Caller says reaches the far end through Google Voice's "microphone" (the far end measures it);
//   4. hanging up in Google Voice ends the eval, and the run is graded.

import { spawn, execFileSync } from "node:child_process";
import { mkdtempSync, readdirSync, existsSync, writeFileSync } from "node:fs";
import { createServer as https } from "node:https";
import { createServer as http } from "node:http";
import { tmpdir } from "node:os";
import { join, resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
let chromium;
try { ({ chromium } = require("playwright")); } catch { ({ chromium } = require("/opt/npm-tools/node_modules/playwright")); }

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = resolve(HERE, "../../..");
const EXT = join(ROOT, "extension");
const PY = process.env.LAMPLINE_PYTHON || "python3";
const WORK = mkdtempSync(join(tmpdir(), "lampline-e2e-"));
const SHOTS = process.env.E2E_SHOTS || WORK;
const ENGINE_PORT = 17861, GV_PORT = 17443, FAR_PORT = 17862;
const fail = (m) => { console.error("FAIL:", m); process.exitCode = 1; throw new Error(m); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// --- the fake Google Voice page (served as https://voice.google.com through a host mapping) -------------------
const GV_HTML = `<!doctype html><meta charset=utf-8><title>Voice</title>
<button id=call>Call</button><button id=end aria-label="End call">End</button><audio id=ring autoplay></audio>
<script>
let pc;
document.getElementById("call").onclick = async () => {
  const mic = await navigator.mediaDevices.getUserMedia({ audio: true });
  pc = new RTCPeerConnection();
  pc.addTrack(mic.getAudioTracks()[0], mic);
  pc.ontrack = (e) => { document.getElementById("ring").srcObject = e.streams[0]; };
  await pc.setLocalDescription(await pc.createOffer());
  await new Promise((r) => { if (pc.iceGatheringState === "complete") r(); pc.onicegatheringstatechange = () => pc.iceGatheringState === "complete" && r(); setTimeout(r, 2000); });
  window.__offer = pc.localDescription.sdp;
};
window.__answer = (sdp) => pc.setRemoteDescription({ type: "answer", sdp });
document.getElementById("end").onclick = () => pc && pc.close();
</script>`;

// --- the far end of the phone call: says something (a tone) and measures what it hears ------------------------
const FAR_HTML = `<!doctype html><meta charset=utf-8><title>Far end</title><script>
window.peak = 0;
window.__answerOffer = async (sdp) => {
  const ctx = new AudioContext();
  const osc = ctx.createOscillator(); osc.frequency.value = 300;
  const gain = ctx.createGain(); gain.gain.value = 0.5;
  const out = ctx.createMediaStreamDestination(); osc.connect(gain).connect(out); osc.start();
  const pc = new RTCPeerConnection();
  pc.addTrack(out.stream.getAudioTracks()[0], out.stream);
  pc.ontrack = (e) => {
    const a = new Audio(); a.muted = true; a.srcObject = e.streams[0]; a.play();
    const an = ctx.createAnalyser(); an.fftSize = 2048; ctx.createMediaStreamSource(e.streams[0]).connect(an);
    const buf = new Float32Array(an.fftSize);
    setInterval(() => { an.getFloatTimeDomainData(buf); let s = 0; for (const v of buf) s += v * v;
      window.peak = Math.max(window.peak, Math.sqrt(s / buf.length)); }, 50);
  };
  await pc.setRemoteDescription({ type: "offer", sdp });
  await pc.setLocalDescription(await pc.createAnswer());
  await new Promise((r) => { if (pc.iceGatheringState === "complete") r(); pc.onicegatheringstatechange = () => pc.iceGatheringState === "complete" && r(); setTimeout(r, 2000); });
  return pc.localDescription.sdp;
};
</script>`;

function chromeBinary() {
  const base = "/opt/pw-browsers";
  for (const d of existsSync(base) ? readdirSync(base) : []) {
    const p = join(base, d, "chrome-linux", "chrome");
    if (d.startsWith("chromium-") && existsSync(p)) return p;
  }
  return undefined;
}

async function main() {
  // A self-signed certificate for the fake Google Voice.
  execFileSync("openssl", ["req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=voice.google.com",
    "-keyout", join(WORK, "key.pem"), "-out", join(WORK, "cert.pem")], { stdio: "ignore" });
  const { readFileSync } = await import("node:fs");
  const gv = https({ key: readFileSync(join(WORK, "key.pem")), cert: readFileSync(join(WORK, "cert.pem")) },
    (_, res) => { res.setHeader("Content-Type", "text/html"); res.end(GV_HTML); }).listen(GV_PORT);
  const far = http((_, res) => { res.setHeader("Content-Type", "text/html"); res.end(FAR_HTML); }).listen(FAR_PORT);

  const engine = spawn(PY, [join(HERE, "bridge_server.py"), String(ENGINE_PORT), WORK], { stdio: ["ignore", "pipe", "inherit"] });
  await new Promise((ok, no) => {
    engine.stdout.on("data", (d) => String(d).includes("READY") && ok());
    engine.on("exit", (c) => no(new Error(`engine exited ${c}`)));
  });
  const base = `http://127.0.0.1:${ENGINE_PORT}`;
  for (let i = 0; i < 60; i++) { try { if ((await fetch(base + "/v1/health")).ok) break; } catch {} await sleep(250); }

  const context = await chromium.launchPersistentContext(join(WORK, "profile"), {
    headless: false, executablePath: chromeBinary(),
    args: ["--headless=new", `--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`,
      `--host-resolver-rules=MAP voice.google.com 127.0.0.1:${GV_PORT}`, "--ignore-certificate-errors", "--no-proxy-server",
      "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream", "--autoplay-policy=no-user-gesture-required"],
    viewport: { width: 1360, height: 980 },
  });
  try {
    // 1. The Evals page finds the extension and starts a Google Voice eval.
    const app = await context.newPage();
    await app.goto(`${base}/#/evals`);
    await app.getByText(/Lampline Phone Bridge .* is installed/).waitFor({ timeout: 15000 });
    await app.getByLabel("Number to call").fill("+1 555 010 1234");
    await app.getByLabel("Who is calling").fill("Sam Rivera, a patient who wants to move a cleaning.");
    await app.getByLabel("What they want").fill("Move the cleaning to Friday morning");
    await app.getByLabel("Pass criteria").fill("The agent greets the caller");
    const opened = context.waitForEvent("page");
    await app.getByRole("button", { name: "Start eval" }).click();
    const voice = await opened;
    await voice.waitForLoadState();
    if (!voice.url().startsWith("https://voice.google.com/u/0/calls?a=nc,%2B15550101234")) fail(`opened ${voice.url()}`);
    await voice.getByText(/Lampline eval: press Call/).waitFor({ timeout: 10000 });
    console.log("ok 1: Start opened Google Voice with the number, and the bridge is on");

    // 2 and 3. Press Call; connect Google Voice to the far end of the phone line.
    const farEnd = await context.newPage();
    await farEnd.goto(`http://127.0.0.1:${FAR_PORT}/`);
    await voice.click("#call");
    await voice.waitForFunction(() => window.__offer, null, { timeout: 15000 });
    const answer = await farEnd.evaluate((sdp) => window.__answerOffer(sdp), await voice.evaluate(() => window.__offer));
    await voice.evaluate((sdp) => window.__answer(sdp), answer);
    await voice.getByText(/Test Caller is talking/).waitFor({ timeout: 20000 });
    await app.getByText("On the call").first().waitFor({ timeout: 20000 });
    let heard = { peak: 0 }, farPeak = 0;
    for (let i = 0; i < 60 && (heard.peak < 0.05 || farPeak < 0.05); i++) {
      await sleep(500);
      heard = await (await fetch(base + "/e2e/heard")).json();
      farPeak = await farEnd.evaluate(() => window.peak);
    }
    if (heard.peak < 0.05) fail(`the Test Caller heard nothing from the call (peak ${heard.peak})`);
    console.log(`ok 2: the call's audio reached the Test Caller (level ${heard.peak.toFixed(3)})`);
    if (farPeak < 0.05) fail(`the far end heard nothing from the Test Caller (peak ${farPeak})`);
    console.log(`ok 3: the Test Caller's voice reached the far end through Google Voice (level ${farPeak.toFixed(3)})`);
    await sleep(1500);
    await app.evaluate(() => document.querySelectorAll("main, main *").forEach((el) => el.scrollTop = 0));
    await app.screenshot({ path: join(SHOTS, "evals-live.png") });

    // 4. Hang up in Google Voice: the eval ends and is graded.
    await voice.click("#end");
    await voice.getByText(/Lampline eval finished/).waitFor({ timeout: 15000 });
    let run;
    for (let i = 0; i < 80; i++) {
      run = (await (await fetch(base + "/v1/evals")).json()).runs[0];
      if (["passed", "failed", "error", "cancelled"].includes(run.status)) break;
      await sleep(500);
    }
    if (!["passed", "failed"].includes(run.status)) fail(`the run ended as ${run.status}: ${run.error}`);
    await app.getByText(run.status === "passed" ? "Passed" : "Failed").first().waitFor({ timeout: 10000 });
    console.log(`ok 4: hanging up ended the eval; graded ${run.status}`);
    await app.evaluate(() => document.querySelectorAll("main, main *").forEach((el) => el.scrollTop = 0));
    await app.screenshot({ path: join(SHOTS, "evals-done.png") });
    console.log(`screenshots in ${SHOTS}`);
  } finally {
    await context.close();
    engine.kill();
    gv.close(); far.close();
  }
}

main().catch((e) => { console.error(e); process.exitCode = 1; });
