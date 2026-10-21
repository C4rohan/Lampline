import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle2, CircleSlash, ClipboardCheck, Headphones, Loader2, Phone, PhoneOff, Plus, Puzzle, Trash2, XCircle } from "lucide-react";
import { toast } from "sonner";
import { api, type AgentSummary } from "@/lib/api";
import { useLoad } from "@/lib/app";
import { cn, local } from "@/lib/utils";
import { Lamp, Notice, Page } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, NativeSelect, Textarea } from "@/components/ui/input";
import { Field } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";

type Method = "google_voice" | "speaker";
type Said = { who: "agent" | "caller"; text: string; t?: number };
type Result = { goal_met: boolean; criteria: { criterion: string; passed: boolean; reason: string }[]; summary: string };
type Run = {
  id: string; created: number; caller_agent: string; method: Method; number: string; persona: string; goal: string;
  criteria: string[]; max_minutes: number; status: string; started: number | null; ended: number | null; ended_reason: string | null;
  transcript?: Said[]; results: Result | null; summary: string | null; error: string | null; duration: number | null;
  live?: { session_id: string; events_token: string }; bridge?: { token: string; open_url: string };
};
type Bridge = { found: boolean; version?: string };

const DONE = ["passed", "failed", "error", "cancelled"];
const STATUS: Record<string, string> = {
  waiting: "Waiting for the call", in_call: "On the call", grading: "Grading", passed: "Passed", failed: "Failed",
  error: "Didn't run", cancelled: "Cancelled",
};
const when = (s: number) => new Date(s * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const length = (s: number | null) => (s == null ? "—" : s < 60 ? `${s} s` : `${Math.floor(s / 60)} min ${s % 60} s`);
const masked = (n: string) => n.replace(/\d(?=\d{4})/g, "•");

function StatusBadge({ status }: { status: string }) {
  if (status === "passed") return <Badge variant="ok"><CheckCircle2 />Passed</Badge>;
  if (status === "failed") return <Badge variant="bad"><XCircle />Failed</Badge>;
  if (status === "error") return <Badge variant="warn">Didn't run</Badge>;
  if (status === "cancelled") return <Badge variant="outline"><CircleSlash />Cancelled</Badge>;
  return <Badge variant="lamp"><Loader2 className="animate-spin" />{STATUS[status] || status}</Badge>;
}

function waitIce(pc: RTCPeerConnection) {
  return new Promise<void>((resolve) => {
    if (pc.iceGatheringState === "complete") return resolve();
    pc.addEventListener("icegatheringstatechange", () => { if (pc.iceGatheringState === "complete") resolve(); });
    setTimeout(resolve, 2500);
  });
}

/* The Lampline Phone Bridge extension announces itself on this page. */
function useBridge(): Bridge {
  const [b, setB] = useState<Bridge>({ found: false });
  useEffect(() => {
    const on = (e: MessageEvent) => {
      if (e.source === window && e.data?.source === "lampline-bridge" && e.data.type === "bridge:ready") setB({ found: true, version: e.data.version });
    };
    window.addEventListener("message", on);
    window.postMessage({ source: "lampline-ui", type: "bridge:ping" }, location.origin);
    return () => window.removeEventListener("message", on);
  }, []);
  return b;
}

function useDevices(enabled: boolean) {
  const [devices, setDevices] = useState<MediaDeviceInfo[]>([]);
  const load = useCallback(async () => {
    try {
      const probe = await navigator.mediaDevices.getUserMedia({ audio: true });  // names show only after permission
      probe.getTracks().forEach((t) => t.stop());
      setDevices(await navigator.mediaDevices.enumerateDevices());
    } catch { toast.error("The browser didn't allow the microphone."); }
  }, []);
  useEffect(() => { if (enabled) navigator.mediaDevices?.enumerateDevices().then(setDevices).catch(() => {}); }, [enabled]);
  return { mics: devices.filter((d) => d.kind === "audioinput"), speakers: devices.filter((d) => d.kind === "audiooutput"), load };
}

export function EvalsPage() {
  const agents = useLoad(() => api<{ agents: AgentSummary[] }>("GET", "/v1/agents"));
  const runs = useLoad(() => api<{ runs: Run[] }>("GET", "/v1/evals"));
  const bridge = useBridge();
  const [form, setForm] = useState(() => {
    try { return JSON.parse(local.get("vpe-eval-form") || "") as Record<string, string>; } catch { return {} as Record<string, string>; }
  });
  const field = (k: string, d = "") => form[k] ?? d;
  const set = (k: string, v: string) => setForm((f) => { const n = { ...f, [k]: v }; local.set("vpe-eval-form", JSON.stringify(n)); return n; });
  const method = (field("method", "google_voice") as Method);
  const dev = useDevices(method === "speaker");
  const [run, setRun] = useState<Run | null>(null);
  const [said, setSaid] = useState<Said[]>([]);
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const rt = useRef<{ es?: EventSource; pc?: RTCPeerConnection; mic?: MediaStream; poll?: number; runId?: string }>({});
  const logRef = useRef<HTMLDivElement>(null);

  const all = agents.data?.agents || [];
  const callerSlug = field("caller") && all.some((a) => a.slug === field("caller")) ? field("caller")
    : (all.find((a) => /test.?caller/i.test(a.slug + " " + a.name))?.slug || "");

  useEffect(() => { logRef.current?.scrollTo({ top: logRef.current.scrollHeight, behavior: "smooth" }); }, [said]);

  const stopLive = useCallback(() => {
    const r = rt.current;
    r.es?.close(); r.pc?.close(); r.mic?.getTracks().forEach((t) => t.stop());
    if (r.poll) clearInterval(r.poll);
    rt.current = {};
  }, []);
  useEffect(() => stopLive, [stopLive]);

  // Status from the extension, for the run we started.
  useEffect(() => {
    const on = (e: MessageEvent) => {
      const d = e.data;
      if (e.source !== window || d?.source !== "lampline-bridge" || d.runId !== rt.current.runId) return;
      if (d.type === "bridge:started" && !d.ok) setNote(`The extension couldn't open Google Voice: ${d.error}`);
      if (d.type === "bridge:status" && d.detail) setNote(d.detail);
    };
    window.addEventListener("message", on);
    return () => window.removeEventListener("message", on);
  }, []);

  function listen(sessionId: string, token: string) {
    if (rt.current.es) return;
    const es = new EventSource(`/v1/sessions/${sessionId}/events?token=${encodeURIComponent(token)}`);
    es.onmessage = (m) => {
      const e = JSON.parse(m.data);
      if (e.type === "user_speech" && e.text) setSaid((s) => [...s, { who: "agent", text: e.text }]);
      if (e.type === "bot_speech" && e.text && !e.filler) setSaid((s) => [...s, { who: "caller", text: e.text }]);
    };
    rt.current.es = es;
  }

  function follow(id: string) {
    rt.current.runId = id;
    rt.current.poll = window.setInterval(async () => {
      try {
        const r = await api<Run>("GET", `/v1/evals/${id}`);
        setRun((prev) => { if (prev?.status !== r.status) runs.reload(); return r; });
        if (r.live) listen(r.live.session_id, r.live.events_token);
        if (DONE.includes(r.status)) {
          stopLive();
          if (r.transcript?.length) setSaid(r.transcript);
          runs.reload();
        }
      } catch { /* try again on the next tick */ }
    }, 1500);
  }

  async function start() {
    if (!callerSlug) return toast.error("Create a Test Caller first.");
    if (method === "google_voice" && !bridge.found) return toast.error("Install the Lampline Phone Bridge extension first.");
    stopLive(); setSaid([]); setNote(""); setBusy(true);
    try {
      const criteria = field("criteria").split("\n").map((c) => c.trim()).filter(Boolean);
      const r = await api<Run>("POST", "/v1/evals", {
        caller_agent: callerSlug, method, number: field("number"), persona: field("persona"), goal: field("goal"),
        criteria, max_minutes: Number(field("minutes", "5")),
      });
      setRun(r);
      if (method === "google_voice") {
        rt.current.runId = r.id;
        window.postMessage({ source: "lampline-ui", type: "bridge:start", runId: r.id, token: r.bridge!.token, url: r.bridge!.open_url }, location.origin);
        setNote("Google Voice is opening in a new tab with the number filled in. Press Call there.");
      } else {
        const clean = field("clean") === "1";
        const mic = await navigator.mediaDevices.getUserMedia({ audio: {
          deviceId: field("mic") ? { exact: field("mic") } : undefined,
          echoCancellation: !clean, noiseSuppression: !clean, autoGainControl: !clean } });
        const pc = new RTCPeerConnection({ iceServers: [] });
        rt.current.mic = mic; rt.current.pc = pc;
        pc.addTransceiver(mic.getAudioTracks()[0], { direction: "sendrecv" });
        pc.createDataChannel("messages", { ordered: true });
        pc.ontrack = (e) => {
          if (e.track.kind !== "audio") return;
          const el = document.getElementById("remote") as HTMLAudioElement & { setSinkId?: (id: string) => Promise<void> };
          el.srcObject = new MediaStream([e.track]);
          if (field("speaker") && el.setSinkId) el.setSinkId(field("speaker")).catch(() => toast.error("Couldn't use that speaker; using the default."));
        };
        await pc.setLocalDescription(await pc.createOffer());
        await waitIce(pc);
        const ans = await api<{ sdp: string; type: RTCSdpType; session_id: string; events_token: string }>("POST", `/v1/evals/${r.id}/offer`,
          { sdp: pc.localDescription!.sdp, type: pc.localDescription!.type });
        await pc.setRemoteDescription({ sdp: ans.sdp, type: ans.type });
        listen(ans.session_id, ans.events_token);
        setNote("Dial the agent from your phone on speaker now. The Test Caller waits for it to speak.");
      }
      follow(r.id);
      runs.reload();
    } catch (e) {
      toast.error((e as Error).message);
      if (run && !DONE.includes(run.status)) setRun(null);
    } finally { setBusy(false); }
  }

  async function stop() {
    if (!run) return;
    try { setRun(await api<Run>("POST", `/v1/evals/${run.id}/stop`)); } catch (e) { toast.error((e as Error).message); }
  }

  async function open(id: string) {
    stopLive(); setNote("");
    const r = await api<Run>("GET", `/v1/evals/${id}`);
    setRun(r); setSaid(r.transcript || []);
    if (!DONE.includes(r.status)) follow(id);
  }

  async function remove(id: string) {
    try {
      await api("DELETE", `/v1/evals/${id}`);
      if (run?.id === id) { setRun(null); setSaid([]); }
      runs.reload();
    } catch (e) { toast.error((e as Error).message); }
  }

  async function makeCaller() {
    try {
      const a = await api<{ slug: string }>("POST", "/v1/agents", { name: "Test Caller", template: "test-caller" });
      set("caller", a.slug); agents.reload(); toast.success("Test Caller created");
    } catch (e) { toast.error((e as Error).message); }
  }

  const live = run && !DONE.includes(run.status);
  const ready = callerSlug && field("number").trim() && field("persona").trim() && field("goal").trim()
    && (method !== "google_voice" || bridge.found);

  return (
    <Page wide title="Evals" description="Real phone calls to a voice agent: a Test Caller plays the caller, and the call is graded against what should happen.">
      <div className="grid gap-6 xl:grid-cols-[420px_minmax(0,1fr)]">
        <Card className="grid content-start gap-5 p-5">
          <Field label="Caller" htmlFor="ev-caller" hint="The agent that plays the caller. The Test Caller template waits for the other side to speak first."
            aside={!all.some((a) => /test.?caller/i.test(a.slug + " " + a.name)) && <Button size="sm" variant="outline" onClick={makeCaller}><Plus />Create Test Caller</Button>}>
            <NativeSelect id="ev-caller" value={callerSlug} onChange={(e) => set("caller", e.target.value)} disabled={!!live}>
              {!callerSlug && <option value="">Choose an agent</option>}
              {all.map((a) => <option key={a.slug} value={a.slug}>{a.name}</option>)}
            </NativeSelect>
          </Field>
          <Field label="Number to call" htmlFor="ev-number" hint="The agent you're testing, with its country code. Only call agents you own or may test.">
            <Input id="ev-number" inputMode="tel" placeholder="+1 555 010 1234" value={field("number")} onChange={(e) => set("number", e.target.value)} disabled={!!live} />
          </Field>
          <div className="grid gap-2">
            <span className="text-sm font-medium">How the call goes out</span>
            <div className="grid gap-2" role="radiogroup" aria-label="How the call goes out">
              {([
                ["google_voice", Phone, "Google Voice (free)", "Lampline opens Google Voice with the number filled in; you press Call. The audio is connected inside the browser, on Windows or Mac."],
                ["speaker", Headphones, "Phone on speaker", "Dial from your own phone on speaker next to this computer, or route a phone app through a virtual audio cable."],
              ] as const).map(([id, Icon, title, text]) => (
                <button key={id} type="button" role="radio" aria-checked={method === id} disabled={!!live} onClick={() => set("method", id)}
                  className={cn("flex gap-3 rounded-lg border p-3 text-left transition-colors", method === id ? "border-primary bg-primary/5 ring-1 ring-primary" : "hover:bg-muted/50")}>
                  <Icon className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                  <span><span className="block text-sm font-medium">{title}</span><span className="block text-[12.5px] leading-snug text-muted-foreground">{text}</span></span>
                </button>
              ))}
              <div className="flex gap-3 rounded-lg border border-dashed p-3 text-left opacity-60" aria-disabled>
                <Phone className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
                <span><span className="block text-sm font-medium">Lampline places the call (Twilio)</span><span className="block text-[12.5px] text-muted-foreground">Coming next: unattended runs.</span></span>
              </div>
            </div>
          </div>
          {method === "google_voice" && (bridge.found
            ? <p className="flex items-center gap-2 text-[13px] text-muted-foreground"><Lamp on />Lampline Phone Bridge {bridge.version} is installed.</p>
            : <Notice tone="warn"><Puzzle className="mt-0.5 size-4 shrink-0" /><span>
                Google Voice needs the <strong>Lampline Phone Bridge</strong> extension (Chrome or Edge): open <code>chrome://extensions</code>, turn on Developer mode,
                choose <em>Load unpacked</em> and pick the <code>extension</code> folder of your Lampline download. Then reload this page.
                See <a className="underline" href="https://github.com/C4rohan/Lampline/blob/prod/docs/EVALS.md" target="_blank" rel="noreferrer">docs/EVALS.md</a>.</span></Notice>)}
          {method === "speaker" && (
            <div className="grid gap-3 rounded-lg bg-muted/40 p-3">
              {!dev.mics.some((d) => d.label) && <Button size="sm" variant="outline" onClick={dev.load}>Show microphones and speakers</Button>}
              <Field label="Microphone" htmlFor="ev-mic">
                <NativeSelect id="ev-mic" value={field("mic")} onChange={(e) => set("mic", e.target.value)} disabled={!!live}>
                  <option value="">Default</option>
                  {dev.mics.map((d) => <option key={d.deviceId} value={d.deviceId}>{d.label || "Microphone"}</option>)}
                </NativeSelect>
              </Field>
              <Field label="Speaker" htmlFor="ev-speaker">
                <NativeSelect id="ev-speaker" value={field("speaker")} onChange={(e) => set("speaker", e.target.value)} disabled={!!live}>
                  <option value="">Default</option>
                  {dev.speakers.map((d) => <option key={d.deviceId} value={d.deviceId}>{d.label || "Speaker"}</option>)}
                </NativeSelect>
              </Field>
              <label className="flex items-center gap-2 text-[13px] text-muted-foreground">
                <Switch checked={field("clean") === "1"} onCheckedChange={(v) => set("clean", v ? "1" : "")} disabled={!!live} aria-label="Virtual audio cable" />
                Virtual audio cable (turns off echo cancelling and noise suppression)
              </label>
            </div>
          )}
          <Field label="Who is calling" htmlFor="ev-persona" hint="The Test Caller plays this person. Use made-up details.">
            <Textarea id="ev-persona" rows={3} value={field("persona")} onChange={(e) => set("persona", e.target.value)} disabled={!!live}
              placeholder="Sam Rivera, a patient who wants to move a dental cleaning. Polite, a little rushed." />
          </Field>
          <Field label="What they want" htmlFor="ev-goal">
            <Input id="ev-goal" value={field("goal")} onChange={(e) => set("goal", e.target.value)} disabled={!!live} placeholder="Move the cleaning to Friday morning" />
          </Field>
          <Field label="Pass criteria" htmlFor="ev-criteria" hint="One per line. Each is checked against the transcript; the goal is always checked.">
            <Textarea id="ev-criteria" rows={4} value={field("criteria")} onChange={(e) => set("criteria", e.target.value)} disabled={!!live}
              placeholder={"The agent asks for the caller's name\nThe agent offers a Friday morning time\nThe agent confirms the change before hanging up"} />
          </Field>
          <Field label="Time limit" htmlFor="ev-minutes">
            <NativeSelect id="ev-minutes" value={field("minutes", "5")} onChange={(e) => set("minutes", e.target.value)} disabled={!!live}>
              {["3", "5", "10", "15"].map((m) => <option key={m} value={m}>{m} minutes</option>)}
            </NativeSelect>
          </Field>
          {live
            ? <Button variant="destructive" onClick={stop}><PhoneOff />Stop the eval</Button>
            : <Button onClick={start} disabled={busy || !ready}>{busy ? <Loader2 className="animate-spin" /> : <ClipboardCheck />}Start eval</Button>}
        </Card>

        <div className="grid content-start gap-6">
          <Card className="flex min-h-[420px] flex-col overflow-hidden">
            <div className="flex flex-wrap items-center gap-3 border-b px-4 py-3">
              <h3 className="text-sm font-semibold">{run ? `Call to ${masked(run.number)}` : "The call"}</h3>
              {run && <StatusBadge status={run.status} />}
              {run?.duration != null && <span className="text-[13px] text-muted-foreground">{length(run.duration)}</span>}
              <span className="ml-auto text-[13px] text-muted-foreground" role="status">{note}</span>
            </div>
            <div ref={logRef} className="flex max-h-[52vh] flex-1 flex-col gap-3 overflow-y-auto bg-muted/30 px-4 py-5" aria-live="polite">
              {!said.length && (
                <p className="m-auto max-w-sm text-center text-sm text-muted-foreground">
                  {run ? "The conversation shows here as it happens. The agent you're testing is on the left; the Test Caller on the right."
                    : "Fill in the eval and press Start. You'll see the conversation here, then whether it passed."}
                </p>
              )}
              {said.map((l, i) => l.who === "agent"
                ? <div key={i} className="max-w-[78%] self-start rounded-2xl rounded-tl-sm border bg-card px-4 py-2.5 text-[14.5px] leading-relaxed shadow-xs">{l.text}</div>
                : <div key={i} className="max-w-[78%] self-end rounded-2xl rounded-tr-sm bg-primary px-4 py-2.5 text-[14.5px] leading-relaxed text-primary-foreground">{l.text}</div>)}
            </div>
            {run && (run.results || run.error) && (
              <div className="grid gap-3 border-t p-4">
                {run.error && <Notice tone="bad">{run.error}</Notice>}
                {run.results && (
                  <>
                    {run.results.summary && <p className="text-sm">{run.results.summary}</p>}
                    <ul className="grid gap-2 text-sm">
                      <li className="flex gap-2">{run.results.goal_met ? <CheckCircle2 className="size-4 shrink-0 text-ok" /> : <XCircle className="size-4 shrink-0 text-bad" />}
                        <span><strong>Goal:</strong> {run.goal}</span></li>
                      {run.results.criteria.map((c) => (
                        <li key={c.criterion} className="flex gap-2">
                          {c.passed ? <CheckCircle2 className="size-4 shrink-0 text-ok" /> : <XCircle className="size-4 shrink-0 text-bad" />}
                          <span>{c.criterion}<span className="block text-[12.5px] text-muted-foreground">{c.reason}</span></span>
                        </li>
                      ))}
                    </ul>
                  </>
                )}
              </div>
            )}
          </Card>

          <Card className="overflow-hidden">
            <div className="border-b px-4 py-3"><h3 className="text-sm font-semibold">Past evals</h3></div>
            {!runs.data?.runs.length
              ? <p className="p-4 text-sm text-muted-foreground">No evals yet.</p>
              : (
                <div className="overflow-x-auto">
                  <table className="w-full text-sm">
                    <thead className="text-left text-xs text-muted-foreground"><tr>
                      <th className="px-4 py-2 font-medium">When</th><th className="hidden px-4 py-2 font-medium 2xl:table-cell">Number</th>
                      <th className="px-4 py-2 font-medium">Caller wants</th><th className="px-4 py-2 font-medium">Result</th>
                      <th className="px-4 py-2 font-medium">Length</th><th className="w-10" /></tr></thead>
                    <tbody>
                      {runs.data.runs.map((r) => (
                        <tr key={r.id} className={cn("cursor-pointer border-t hover:bg-muted/40", run?.id === r.id && "bg-muted/50")} onClick={() => open(r.id)}>
                          <td className="whitespace-nowrap px-4 py-2">{when(r.created)}</td>
                          <td className="hidden whitespace-nowrap px-4 py-2 font-mono text-xs 2xl:table-cell">{masked(r.number)}</td>
                          <td className="max-w-[200px] truncate px-4 py-2" title={r.goal}>{r.goal}</td>
                          <td className="px-4 py-2"><StatusBadge status={r.status} /></td>
                          <td className="whitespace-nowrap px-4 py-2 text-muted-foreground">{length(r.duration)}</td>
                          <td className="px-2"><Button size="icon-sm" variant="ghost" aria-label="Delete this eval" onClick={(e) => { e.stopPropagation(); remove(r.id); }}><Trash2 /></Button></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
          </Card>
        </div>
      </div>
    </Page>
  );
}
