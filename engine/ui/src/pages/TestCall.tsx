import { Fragment, useEffect, useRef, useState } from "react";
import { AudioLines, GitBranch, MessageSquare, Mic, PhoneOff, Send, Timer, Wrench } from "lucide-react";
import { api, auth, STT_LABEL, TTS_LABEL, type AgentSummary } from "@/lib/api";
import { useApp, useLoad } from "@/lib/app";
import { cn, local } from "@/lib/utils";
import { Page } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, NativeSelect } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";

type Ev = { type: string; [k: string]: any };
type Turn = { response_ms?: number | null; turn_ms?: number; listening_ms?: number; model_ms?: number; speaking_ms?: number };

const secs = (ms?: number | null) => (ms == null ? "—" : ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(2)} s`);

/* How long the caller waited for each reply, and where the time went. */
function Latency({ turns, call }: { turns: Turn[]; call: boolean }) {
  const last = turns[turns.length - 1];
  const timed = turns.map((t) => (call ? t.response_ms : t.model_ms)).filter((v): v is number => typeof v === "number");
  const avg = timed.length ? Math.round(timed.reduce((a, b) => a + b, 0) / timed.length) : null;
  const parts: [string, number | undefined, string][] = [
    ["Turn detection", last?.turn_ms, "How long the smart-turn model took to decide the caller had finished"],
    ["Listening", last?.listening_ms, "Speech to text: time to the final transcript"],
    ["Model", last?.model_ms, "Time to the model's first words"],
    ["Speaking", last?.speaking_ms, "Text to speech: time to the first audio"],
  ];
  return (
    <Card className="p-5">
      <h3 className="mb-3 flex items-center gap-2 text-sm font-semibold"><Timer className="size-4 text-muted-foreground" />Response time</h3>
      {!last ? (
        <p className="text-sm text-muted-foreground">{call ? "How long the caller waits after they stop talking, measured on each reply." : "How long the model takes to start each reply."}</p>
      ) : (
        <>
          <div className="mb-3 flex items-baseline gap-4">
            <div><div className="font-mono text-2xl font-medium tabular-nums">{secs(call ? last.response_ms : last.model_ms)}</div><div className="text-xs text-muted-foreground">last reply</div></div>
            <div><div className="font-mono text-lg tabular-nums text-muted-foreground">{secs(avg)}</div><div className="text-xs text-muted-foreground">average of {timed.length}</div></div>
          </div>
          {call && (
            <dl className="grid grid-cols-[1fr_auto] gap-x-3 gap-y-1 text-sm">
              {parts.map(([label, ms, title]) => (
                <Fragment key={label}><dt className="text-muted-foreground" title={title}>{label}</dt><dd className="text-right font-mono text-xs leading-5 tabular-nums">{secs(ms)}</dd></Fragment>
              ))}
            </dl>
          )}
        </>
      )}
    </Card>
  );
}
type Line =
  | { kind: "step"; name: string }
  | { kind: "agent"; text: string; filler?: boolean }
  | { kind: "caller"; text: string }
  | { kind: "tool"; name: string; result: any }
  | { kind: "sys"; text: string; error?: boolean; link?: boolean };

function waitIce(pc: RTCPeerConnection) {
  return new Promise<void>((resolve) => {
    if (pc.iceGatheringState === "complete") return resolve();
    const check = () => { if (pc.iceGatheringState === "complete") { pc.removeEventListener("icegatheringstatechange", check); resolve(); } };
    pc.addEventListener("icegatheringstatechange", check);
    setTimeout(resolve, 2500);
  });
}

function ToolLine({ name, r }: { name: string; r: any }) {
  const http = r && "ok" in r;
  const failed = r?.error || r?.ok === false;
  const data = http && !failed ? JSON.stringify(r.data ?? r.data_truncated ?? null) : "";
  return (
    <div className="max-w-[85%] self-start rounded-lg border border-k-tool/40 bg-lamp-soft px-3.5 py-2.5 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        {http ? <Wrench className="size-3.5 text-k-tool" /> : <GitBranch className="size-3.5 text-k-tool" />}
        <code className="font-medium">{name}</code>
        <Badge variant={failed ? "bad" : "ok"}>{failed ? "Failed" : "Done"}</Badge>
        {http && <Badge variant="outline">{r.mock ? "Mock" : `Live${r.seconds != null ? ` · ${r.seconds}s` : ""}`}</Badge>}
        {r?.next_step && <span className="text-muted-foreground">to {r.next_step}</span>}
      </div>
      {failed && r.error && <p className="mt-1.5 text-[13px] text-bad">{r.error}</p>}
      {data && <p className="mt-1.5 break-all font-mono text-xs text-muted-foreground">{data.length > 240 ? `${data.slice(0, 240)}…` : data}</p>}
    </div>
  );
}

export function TestCall({ params }: { params: URLSearchParams }) {
  const { health, refresh } = useApp();
  const { data } = useLoad(() => api<{ agents: AgentSummary[] }>("GET", "/v1/agents"));
  const agents = (data?.agents || []).filter((a) => !a.error);
  const c = health?.config;
  const [agent, setAgent] = useState(params.get("agent") || local.get("vpe-agent") || "");
  const [mode, setMode] = useState<"chat" | "call">(local.get("vpe-mode") === "call" ? "call" : "chat");
  const [mock, setMock] = useState(local.get("vpe-mock-tools") !== "0");
  const [session, setSession] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");
  const [lines, setLines] = useState<Line[]>([]);
  const [visited, setVisited] = useState<string[]>([]);
  const [collected, setCollected] = useState<Record<string, unknown>>({});
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");
  const [level, setLevel] = useState<number[]>(Array(16).fill(0));
  const rt = useRef<{ pc?: RTCPeerConnection; mic?: MediaStream; es?: EventSource; ctx?: AudioContext; raf?: number; session?: string | null; mode?: string }>({});
  const logRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => { refresh(); }, [refresh]);
  useEffect(() => { if (agents.length && !agents.some((a) => a.slug === agent)) setAgent(agents[0].slug); }, [data]); // eslint-disable-line
  useEffect(() => { logRef.current?.scrollTo({ top: logRef.current.scrollHeight }); }, [lines]);
  useEffect(() => () => { hangUp(); }, []); // eslint-disable-line
  const callMode = mode === "call" && !!c?.voice_ready;

  const add = (l: Line) => setLines((x) => [...x, l]);
  function onEvent(e: Ev) {
    switch (e.type) {
      case "node_transition": setVisited((v) => [...v, e.node_name]); add({ kind: "step", name: e.node_name }); break;
      case "bot_speech": add({ kind: "agent", text: e.text, filler: e.filler }); break;
      case "user_speech": if (rt.current.mode === "call") add({ kind: "caller", text: e.text }); break;
      case "tool_call_result": add({ kind: "tool", name: e.function_name, result: e.result || {} }); break;
      case "variables": setCollected((v) => ({ ...v, ...e.values })); break;
      case "turn_metrics": setTurns((t) => [...t, e as Turn]); break;
      case "execution_error": add({ kind: "sys", text: e.message, error: true }); break;
      case "notice": add({ kind: "sys", text: e.message }); break;
      case "transfer": {
        const sent = Object.keys(e.details || {});
        add({ kind: "sys", text: `Handing the caller to ${e.to}${sent.length ? ` with ${sent.join(", ")}` : ""}. On a phone call they're connected now.` });
        break;
      }
      case "call_ended": add({ kind: "sys", text: `Call ended: ${e.reason}` }); finish(); break;
    }
  }

  function finish() {
    const r = rt.current, s = r.session;
    r.es?.close(); if (r.raf) cancelAnimationFrame(r.raf); r.ctx?.close().catch(() => {}); r.pc?.close(); r.mic?.getTracks().forEach((t) => t.stop());
    rt.current = { mode: r.mode };
    setSession(null); setStatus(""); setLevel(Array(16).fill(0));
    return s;
  }
  async function hangUp() {
    const mode0 = rt.current.mode;
    const s = finish();
    if (s && mode0 === "chat") { try { await api("POST", `/v1/chat/sessions/${s}/end`); } catch { /* ignore */ } }
  }

  async function start() {
    setLines([]); setVisited([]); setCollected({}); setTurns([]); setBusy(true);
    rt.current.mode = callMode ? "call" : "chat";
    try {
      if (!callMode) {
        setStatus("Starting…");
        const r = await api<{ session_id: string; events: Ev[]; ended: boolean }>("POST", "/v1/chat/sessions", { agent, mock_tools: mock });
        rt.current.session = r.session_id; setSession(r.session_id);
        r.events.forEach(onEvent);
        setStatus("Chat open");
        setTimeout(() => inputRef.current?.focus(), 0);
      } else {
        setStatus("Asking for the microphone…");
        const mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
        const pc = new RTCPeerConnection({ iceServers: [] });
        rt.current.mic = mic; rt.current.pc = pc;
        pc.addTransceiver(mic.getAudioTracks()[0], { direction: "sendrecv" });
        pc.addTransceiver("video", { direction: "sendrecv" });
        pc.createDataChannel("messages", { ordered: true });
        pc.ontrack = (e) => { if (e.track.kind === "audio") (document.getElementById("remote") as HTMLAudioElement).srcObject = new MediaStream([e.track]); };
        pc.onconnectionstatechange = () => { if (["failed", "closed"].includes(pc.connectionState) && rt.current.pc === pc) { add({ kind: "sys", text: `Connection ${pc.connectionState}` }); finish(); } };
        await pc.setLocalDescription(await pc.createOffer());
        await waitIce(pc);
        setStatus("Connecting…");
        const ans = await api<{ sdp: string; type: RTCSdpType; session_id: string }>("POST", "/v1/calls/webrtc/offer",
          { agent, sdp: pc.localDescription!.sdp, type: pc.localDescription!.type, mock_tools: mock });
        await pc.setRemoteDescription({ sdp: ans.sdp, type: ans.type });
        rt.current.session = ans.session_id; setSession(ans.session_id);
        const es = new EventSource(`/v1/sessions/${ans.session_id}/events${auth.key ? `?key=${encodeURIComponent(auth.key)}` : ""}`);
        es.onmessage = (m) => onEvent(JSON.parse(m.data));
        rt.current.es = es;
        const ctx = new AudioContext(); const an = ctx.createAnalyser(); an.fftSize = 256;
        ctx.createMediaStreamSource(mic).connect(an);
        const buf = new Uint8Array(an.frequencyBinCount);
        const tick = () => { an.getByteFrequencyData(buf); setLevel(Array.from({ length: 16 }, (_, i) => buf[i * 3 + 2] / 255)); rt.current.raf = requestAnimationFrame(tick); };
        rt.current.ctx = ctx; rt.current.raf = requestAnimationFrame(tick);
        setStatus("On call");
      }
    } catch (e) {
      const msg = (e as Error).message;
      add({ kind: "sys", text: msg, error: true, link: /Integrations/.test(msg) });
      finish();
    } finally { setBusy(false); }
  }

  async function send(e: React.FormEvent) {
    e.preventDefault();
    const t = text.trim();
    if (!t || !session) return;
    setText(""); add({ kind: "caller", text: t }); setBusy(true); setStatus("Agent is replying…");
    try {
      const r = await api<{ events: Ev[] }>("POST", `/v1/chat/sessions/${session}/messages`, { text: t });
      r.events.filter((x) => x.type !== "user_speech").forEach(onEvent);
      if (rt.current.session) setStatus("Chat open");
    } catch (err) { add({ kind: "sys", text: (err as Error).message, error: true }); }
    setBusy(false); setTimeout(() => inputRef.current?.focus(), 0);
  }

  const graph = agents.find((a) => a.slug === agent)?.graph || [];
  const current = visited[visited.length - 1];
  const vars = Object.entries(collected).filter(([, v]) => v !== null && v !== "");

  return (
    <Page wide title="Test call" description="Talk to an agent the way a caller would. Nothing here is recorded.">
      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_300px]">
        <Card className="flex h-[calc(100vh-200px)] min-h-[520px] flex-col overflow-hidden">
          <div className="flex flex-wrap items-center gap-3 border-b px-4 py-3">
            <NativeSelect className="w-56" aria-label="Agent" value={agent} disabled={!!session}
              onChange={(e) => { setAgent(e.target.value); local.set("vpe-agent", e.target.value); setVisited([]); }}>
              {agents.map((a) => <option key={a.slug} value={a.slug}>{a.name}</option>)}
            </NativeSelect>
            <Tabs value={callMode ? "call" : "chat"} onValueChange={(v) => { setMode(v as "chat" | "call"); local.set("vpe-mode", v); }}>
              <TabsList>
                <TabsTrigger value="chat" disabled={!!session}><MessageSquare />Chat</TabsTrigger>
                <TabsTrigger value="call" disabled={!!session || !c?.voice_ready} title={c?.voice_ready ? undefined : "Set up listening and speaking in Integrations"}><Mic />Call</TabsTrigger>
              </TabsList>
            </Tabs>
            {session
              ? <Button variant="destructive" onClick={async () => { await hangUp(); add({ kind: "sys", text: rt.current.mode === "chat" ? "Chat ended" : "Hung up" }); }}>
                  <PhoneOff />{callMode ? "Hang up" : "End chat"}</Button>
              : <Button onClick={start} disabled={busy || !agents.length}>{callMode ? <AudioLines /> : null}Start</Button>}
            <label className="flex items-center gap-2 text-[13px] text-muted-foreground" title="Tools answer with the mock response saved in each tool, so tests never call real services.">
              <Switch checked={mock} onCheckedChange={(v) => { setMock(v); local.set("vpe-mock-tools", v ? null : "0"); }} disabled={!!session} aria-label="Mock tool answers" />
              Mock tool answers
            </label>
            <span className="ml-auto text-[13px] text-muted-foreground" role="status">{status}</span>
          </div>
          <div ref={logRef} className="flex flex-1 flex-col gap-3 overflow-y-auto bg-muted/30 px-4 py-5 md:px-6" aria-live="polite">
            {lines.length === 0 && (
              <div className="m-auto grid max-w-sm justify-items-center gap-2 text-center">
                <span className="grid size-11 place-items-center rounded-full bg-card shadow-xs ring-1 ring-border"><AudioLines className="size-5 text-muted-foreground" /></span>
                <p className="font-medium">{agents.length ? "Pick an agent and press Start" : "No agents yet"}</p>
                <p className="text-sm text-muted-foreground">{!agents.length ? "Create one on the Voice agents page." : c?.voice_ready
                  ? "Chat types to the agent. Call talks to it through your microphone." : "Chat types to the agent. Calls need listening and speaking set up in Integrations."}</p>
              </div>
            )}
            {lines.map((l, i) => l.kind === "step" ? (
              <div key={i} className="flex items-center gap-3 text-xs font-medium text-k-agent">
                <span className="h-px flex-1 bg-border" /><span className="inline-flex items-center gap-1.5 rounded-full border border-k-agent/35 bg-card px-2.5 py-0.5"><GitBranch className="size-3" />{l.name}</span><span className="h-px flex-1 bg-border" />
              </div>
            ) : l.kind === "agent" ? (
              <div key={i} className={cn("max-w-[78%] self-start rounded-2xl rounded-tl-sm border bg-card px-4 py-2.5 text-[14.5px] leading-relaxed shadow-xs", l.filler && "italic text-muted-foreground")}>{l.text}</div>
            ) : l.kind === "caller" ? (
              <div key={i} className="max-w-[78%] self-end rounded-2xl rounded-tr-sm bg-primary px-4 py-2.5 text-[14.5px] leading-relaxed text-primary-foreground">{l.text}</div>
            ) : l.kind === "tool" ? <ToolLine key={i} name={l.name} r={l.result} /> : (
              <div key={i} className={cn("self-center text-center text-xs", l.error ? "text-bad" : "text-muted-foreground")}>
                {l.text}{l.link && <> · <a className="underline" href="#/integrations">Open Integrations</a></>}
              </div>
            ))}
          </div>
          {session && !callMode && (
            <form onSubmit={send} className="flex gap-2 border-t p-3">
              <Input ref={inputRef} value={text} onChange={(e) => setText(e.target.value)} placeholder="Type what the caller says" autoComplete="off" disabled={busy} className="h-10" />
              <Button type="submit" className="h-10" disabled={busy || !text.trim()}><Send />Send</Button>
            </form>
          )}
          {session && callMode && (
            <div className="flex items-center gap-4 border-t px-4 py-3">
              <div className="flex h-6 items-center gap-[3px]" aria-hidden="true">
                {level.map((v, i) => <i key={i} className="w-[3px] rounded-full bg-lamp" style={{ height: `${4 + Math.min(20, v * 30)}px` }} />)}
              </div>
              <span className="text-sm text-muted-foreground">Listening. Speak naturally; you can interrupt.</span>
            </div>
          )}
        </Card>
        <aside className="grid content-start gap-4">
          <Card className="p-5">
            <h3 className="mb-3 text-sm font-semibold">Call progress</h3>
            <ol className="grid gap-0">
              {graph.filter((s) => s.kind !== "global").map((s, i, arr) => {
                const now = !!session && s.name === current, done = visited.includes(s.name);
                return (
                  <li key={i} className="relative flex items-center gap-3 py-1.5 text-sm">
                    {i < arr.length - 1 && <span className="absolute left-[4.5px] top-5 h-[calc(100%-8px)] w-px bg-border" />}
                    <span className={cn("relative size-2.5 shrink-0 rounded-full border-2", now ? "border-lamp bg-lamp shadow-[0_0_0_4px_var(--lamp-soft)]" : done ? "border-muted-foreground bg-muted-foreground" : "border-border bg-card")} />
                    <span className={cn(now ? "font-medium" : done ? "" : "text-muted-foreground")}>{s.name}</span>
                  </li>
                );
              })}
            </ol>
          </Card>
          <Card className="p-5">
            <h3 className="mb-3 text-sm font-semibold">Collected</h3>
            {vars.length ? (
              <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-sm">
                {vars.map(([k, v]) => <Fragment key={k}><dt className="font-mono text-xs leading-5 text-muted-foreground">{k}</dt><dd className="break-words">{typeof v === "object" ? JSON.stringify(v) : String(v)}</dd></Fragment>)}
              </dl>
            ) : <p className="text-sm text-muted-foreground">Values the agent collects show up here.</p>}
          </Card>
          <Latency turns={turns} call={rt.current.mode === "call" || callMode} />
          <Card className="p-5">
            <h3 className="mb-3 text-sm font-semibold">Running on</h3>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 text-sm">
              <dt className="text-muted-foreground">Model</dt><dd className="truncate">{c?.model}</dd>
              <dt className="text-muted-foreground">Listening</dt><dd>{c?.stt_ready ? STT_LABEL[c.stt] : "Not set up"}</dd>
              <dt className="text-muted-foreground">Speaking</dt><dd>{c?.tts_ready ? `${TTS_LABEL[c.tts]}${c.tts_voice ? ` · ${c.tts_voice}` : ""}` : "Not set up"}</dd>
            </dl>
            <Button asChild variant="link" size="sm" className="mt-2"><a href="#/integrations">Change in Integrations</a></Button>
          </Card>
        </aside>
      </div>
    </Page>
  );
}
