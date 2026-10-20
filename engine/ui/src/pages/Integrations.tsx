import { useEffect, useId, useMemo, useState, type ReactNode } from "react";
import { ArrowUpRight, ChevronDown, Info, Search, Trash2 } from "lucide-react";
import { toast } from "sonner";
import { api, type Settings } from "@/lib/api";
import { useApp, useLoad } from "@/lib/app";
import { cn } from "@/lib/utils";
import { Lamp, Notice, Page } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, NativeSelect } from "@/components/ui/input";
import { Field } from "@/components/ui/label";
import { Toggle } from "@/components/ui/switch";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { ProviderIcon } from "@/components/provider-icon";
import { f, Groups, node, row, usePoll, type Ctx, type FieldOpts, type Group } from "./settings";

const FISH_MODELS = {
  "s2.1-pro-free": "s2.1-pro-free: free during Fish's promotion (through Nov 30, 2026)",
  "s2.1-pro": "s2.1-pro: current model, $15 per million characters",
  "s2-pro": "s2-pro: previous model, $15 per million characters",
  "drama-3": "drama-3: in Fish's API, not on its price list yet",
  "s1": "s1: deprecated, retires Dec 31, 2026",
};
const FISH_LATENCY = { balanced: "balanced: faster replies, good quality (best for calls)", normal: "normal: best quality, slower", low: "low: fastest, lower quality" };
const FISH_STT = { "transcribe-1": "transcribe-1: short utterances (best for calls)", "transcribe-1-pro": "transcribe-1-pro: long or multi-speaker audio" };
const WHISPER_SIZES = { "tiny.en": "tiny.en: 75 MB, fastest", "base.en": "base.en: 145 MB, recommended", "small.en": "small.en: 485 MB, most accurate", "distil-small.en": "distil-small.en: 340 MB" };

/* How each generated model tile shows its fields. */
const LLM_FIELD_OPTS: Record<string, FieldOpts> = {
  vertex_project: { placeholder: "my-project", hint: "The project with the Vertex AI API turned on." },
  vertex_location: { placeholder: "global", hint: "global, or a region like us-central1." },
  vertex_credentials: { placeholder: "Optional: paste a service account's JSON key", hint: "Needs the Vertex AI User role. Empty: this computer's Google credentials (gcloud auth application-default login)." },
  azure_endpoint: { placeholder: "https://my-resource.openai.azure.com", hint: "From Keys and Endpoint in the Azure portal." },
  aws_access_key_id: { placeholder: "Optional: AKIA…" },
  aws_region: { placeholder: "us-east-1", hint: "A region where you've enabled the model in Bedrock." },
  ollama_url: { placeholder: "http://localhost:11434/v1", hint: "In Docker, use http://host.docker.internal:11434/v1 to reach Ollama on this computer." },
};

/* Contact centers on the roadmap: shown as tiles so people can see what's coming. */
const CONTACT_CENTERS: { key: string; name: string; tag: string; blurb: string; how: string }[] = [
  { key: "genesys", name: "Genesys Cloud", tag: "AudioHook and SIP", blurb: "Run your agents as the voice bot in Genesys Cloud flows, with handoff to queues.",
    how: "Planned: Genesys AudioHook streams the call to your agent, and transfers go back to an Architect flow or queue." },
  { key: "avaya", name: "Avaya", tag: "SIP", blurb: "Connect Avaya Experience Platform and Aura call flows to your agents.",
    how: "Planned: calls arrive over a SIP trunk through jambonz, with transfers back to Avaya skills and VDNs." },
  { key: "cisco-wxcc", name: "Cisco Webex Contact Center", tag: "Virtual agent", blurb: "Use your agents as the virtual agent in Webex Contact Center flows.",
    how: "Planned: a virtual agent connector for Webex Contact Center flows, with escalation to live agents." },
  { key: "cisco-ucce", name: "Cisco UCCE and PCCE", tag: "SIP through CVP", blurb: "Bring your agents into Unified and Packaged Contact Center Enterprise.",
    how: "Planned: calls from CVP over SIP through jambonz, with transfers back to the call flow." },
  { key: "amazon-connect", name: "Amazon Connect", tag: "Contact flows", blurb: "Answer calls from Amazon Connect contact flows and hand back to queues.",
    how: "Planned: calls routed from a contact flow to your agent, and back to a queue when a person is needed." },
  { key: "nice", name: "NICE CXone", tag: "Virtual agent hub", blurb: "Run your agents as a virtual agent in NICE CXone Studio scripts.",
    how: "Planned: a CXone virtual agent integration, with transfer back to skills." },
  { key: "five9", name: "Five9", tag: "IVA and SIP", blurb: "Use your agents as the voice front end for Five9 campaigns.",
    how: "Planned: calls from Five9 IVA scripts over SIP through jambonz, with transfers back to skills." },
  { key: "twilio-flex", name: "Twilio Flex", tag: "Handoff to Flex", blurb: "Hand calls from your agents to people working in Twilio Flex.",
    how: "Planned: Twilio numbers already work; this adds transfer into Flex TaskRouter queues with the call's details." },
];

type Moon = { installed: boolean; ready: boolean; state: string; message?: string; progress?: number | null; size_mb: number; model: string };
type Builtin = { installed: boolean; state: string; message?: string; progress?: number | null; size_mb: number; whisper_ready: boolean; kokoro_ready: boolean; whisper_model: string };
type LocalSpeech = { available: boolean; docker: boolean; state: string; message?: string; seconds?: number; servers: { whisper: boolean; kokoro: boolean }; log?: string[] };

function Panel({ lead, children }: { lead: string; children: React.ReactNode }) {
  return (
    <div className="grid gap-3 rounded-lg border bg-muted/40 p-4">
      <p className="flex gap-2 text-[13px] text-muted-foreground"><Info className="mt-0.5 size-4 shrink-0" />{lead}</p>
      {children}
    </div>
  );
}

function BuiltinPanel({ onReady }: { onReady: () => void }) {
  const { data: st, setData, kick } = usePoll<Builtin>("/v1/builtin-speech", (s) => s.state === "downloading");
  const [was, setWas] = useState("");
  useEffect(() => { if (st) { if (was === "downloading" && st.state !== "downloading") onReady(); setWas(st.state); } }, [st]); // eslint-disable-line
  if (!st) return <Panel lead="Free and private: runs inside the engine on this computer. No Docker, no account."><span className="text-sm text-muted-foreground">Checking…</span></Panel>;
  const busy = st.state === "downloading";
  const item = (name: string, ok: boolean) => (
    <span className="inline-flex items-center gap-2 text-sm"><Lamp on={ok} warn={busy} />{name}<span className="text-muted-foreground">{ok ? "ready" : busy ? "downloading" : "not downloaded"}</span></span>
  );
  return (
    <Panel lead="Free and private: runs inside the engine on this computer. No Docker, no account.">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
        {item(`Whisper (${st.whisper_model})`, st.whisper_ready)}{item("Kokoro voices", st.kokoro_ready)}
        <span className="ml-auto">
          {!st.installed ? <span className="text-sm text-bad">Not installed yet: restart the engine with scripts/engine-up.sh.</span>
            : busy ? <Button size="sm" variant="outline" disabled>Downloading…</Button>
            : st.whisper_ready && st.kokoro_ready ? <Badge variant="ok">Ready</Badge>
            : <Button size="sm" variant="lamp" onClick={async () => {
                try { setData(await api<Builtin>("POST", "/v1/builtin-speech/download")); } catch (e) { setData({ ...st, state: "error", message: (e as Error).message }); }
                kick();
              }}>Download models (about {st.size_mb} MB, once)</Button>}
        </span>
      </div>
      {busy && st.progress != null && <div className="h-1.5 overflow-hidden rounded-full bg-border"><div className="h-full bg-lamp transition-[width]" style={{ width: `${Math.round(st.progress * 100)}%` }} /></div>}
      {(busy || st.state === "error") && st.message && <p className={cn("text-[13px]", st.state === "error" ? "text-bad" : "text-muted-foreground")}>{st.message}</p>}
    </Panel>
  );
}

function MoonshinePanel({ onReady }: { onReady: () => void }) {
  const { data: st, setData, kick } = usePoll<Moon>("/v1/moonshine", (s) => s.state === "downloading");
  const [was, setWas] = useState("");
  useEffect(() => { if (st) { if (was === "downloading" && st.state !== "downloading") onReady(); setWas(st.state); } }, [st]); // eslint-disable-line
  const lead = "Free and private: runs inside the engine and transcribes while the caller talks, so replies start sooner. English only.";
  if (!st) return <Panel lead={lead}><span className="text-sm text-muted-foreground">Checking…</span></Panel>;
  const busy = st.state === "downloading";
  return (
    <Panel lead={lead}>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
        <span className="inline-flex items-center gap-2 text-sm"><Lamp on={st.ready} warn={busy} />Moonshine ({st.model.replace("-streaming", "")})
          <span className="text-muted-foreground">{st.ready ? "ready" : busy ? "downloading" : "not downloaded"}</span></span>
        <span className="ml-auto">
          {!st.installed ? <span className="text-sm text-bad">Not available on this computer: it needs Linux or an Apple-silicon Mac on macOS 15.</span>
            : busy ? <Button size="sm" variant="outline" disabled>Downloading…</Button>
            : st.ready ? <Badge variant="ok">Ready</Badge>
            : <Button size="sm" variant="lamp" onClick={async () => {
                try { setData(await api<Moon>("POST", "/v1/moonshine/download")); } catch (e) { setData({ ...st, state: "error", message: (e as Error).message }); }
                kick();
              }}>Download model (about {st.size_mb} MB, once)</Button>}
        </span>
      </div>
      {busy && st.progress != null && <div className="h-1.5 overflow-hidden rounded-full bg-border"><div className="h-full bg-lamp transition-[width]" style={{ width: `${Math.round(st.progress * 100)}%` }} /></div>}
      {(busy || st.state === "error") && st.message && <p className={cn("text-[13px]", st.state === "error" ? "text-bad" : "text-muted-foreground")}>{st.message}</p>}
    </Panel>
  );
}

function LocalSpeechPanel({ lead }: { lead: string }) {
  const { data: st, setData, kick } = usePoll<LocalSpeech>("/v1/local-speech", (s) => ["starting", "downloading", "stopping"].includes(s.state), 8000, 2000);
  if (!st) return <Panel lead={lead}><span className="text-sm text-muted-foreground">Checking…</span></Panel>;
  const busy = ["starting", "downloading", "stopping"].includes(st.state);
  const both = st.servers.whisper && st.servers.kokoro;
  const act = async (a: string) => { try { setData(await api<LocalSpeech>("POST", `/v1/local-speech/${a}`)); } catch (e) { setData({ ...st, state: "error", message: (e as Error).message }); } kick(); };
  const server = (name: string, up: boolean) => <span className="inline-flex items-center gap-2 text-sm"><Lamp on={up} warn={busy} />{name}<span className="text-muted-foreground">{up ? "running" : busy ? "starting" : "not running"}</span></span>;
  return (
    <Panel lead={lead}>
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
        {server("Whisper", st.servers.whisper)}{server("Kokoro", st.servers.kokoro)}
        <span className="ml-auto">
          {!st.available ? <span className="text-sm text-muted-foreground">Start it with scripts/speech-up.sh (this engine can't find the repo's speech setup).</span>
            : !st.docker ? <span className="text-sm text-bad">Docker isn't installed. On a Mac, install OrbStack, open it once, then come back.</span>
            : busy ? <Button size="sm" variant="outline" disabled>{st.state === "stopping" ? "Stopping…" : "Starting…"}</Button>
            : both ? <Button size="sm" variant="outline" onClick={() => act("stop")}>Stop local speech</Button>
            : <Button size="sm" variant="lamp" onClick={() => act("start")}>Start local speech</Button>}
        </span>
      </div>
      {((st.message && !["Running", "Stopped"].includes(st.message)) || st.state === "error") && (
        <p className={cn("text-[13px]", st.state === "error" ? "text-bad" : "text-muted-foreground")}>{st.message}{st.seconds != null && busy ? ` (${st.seconds}s)` : ""}</p>
      )}
      {st.log && st.log.length > 0 && (
        <details className="text-[13px]"><summary className="cursor-pointer text-muted-foreground">Show log</summary>
          <pre className="mt-2 max-h-56 overflow-auto rounded-md bg-card p-3 font-mono text-xs">{st.log.join("\n")}</pre></details>
      )}
    </Panel>
  );
}

function FishVoicePicker({ settings, onPick }: { settings: Settings; onPick: (id: string) => void }) {
  const [voices, setVoices] = useState<{ mine: { id: string; title: string }[]; popular: { id: string; title: string }[] } | null>(null);
  const [err, setErr] = useState("");
  const hasKey = !!settings.fields.fish_key?.set;
  useEffect(() => { if (hasKey) api("GET", "/v1/fish/voices").then(setVoices).catch((e) => setErr(e.message)); }, [hasKey]);
  const current = settings.fields.fish_voice?.value || "";
  return (
    <Field label="Choose a voice" htmlFor="fish-voice-pick" hint={err ? <span className="text-bad">{err}</span> : "Pick one, then Save."}>
      <NativeSelect id="fish-voice-pick" disabled={!voices} defaultValue={current} onChange={(e) => e.target.value && onPick(e.target.value)}>
        {!hasKey ? <option value="">Save your Fish Audio key to list voices</option>
          : !voices ? <option value="">{err ? "Couldn't load voices" : "Loading voices…"}</option>
          : <>
              <option value="">{current ? "Keep the voice below" : "Fish Audio's default voice"}</option>
              {voices.mine.length > 0 && <optgroup label="Your voices">{voices.mine.map((v) => <option key={v.id} value={v.id}>{v.title}</option>)}</optgroup>}
              {voices.popular.length > 0 && <optgroup label="Popular English voices">{voices.popular.map((v) => <option key={v.id} value={v.id}>{v.title}</option>)}</optgroup>}
            </>}
      </NativeSelect>
    </Field>
  );
}

function SecretsPanel() {
  const { data, setData } = useLoad(() => api<{ secrets: { name: string; source: string; hint: string }[] }>("GET", "/v1/secrets"));
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const [armed, setArmed] = useState("");
  const [err, setErr] = useState("");
  return (
    <div className="grid gap-4">
      <div className="grid gap-2">
        {data?.secrets.length === 0 && <p className="text-sm text-muted-foreground">No secrets yet.</p>}
        {data?.secrets.map((s) => (
          <div key={s.name} className="flex items-center gap-3 rounded-lg border px-3 py-2 text-sm">
            <code className="truncate">{`{{secrets.${s.name}}}`}</code>
            <span className="text-muted-foreground">{s.hint}</span>
            <Badge variant="outline">{s.source === "env" ? ".env" : "Saved"}</Badge>
            {s.source === "saved" && (
              <Button variant="ghost" size="sm" className={cn("ml-auto", armed === s.name && "text-bad")} onClick={async () => {
                if (armed !== s.name) { setArmed(s.name); return; }
                try { setData(await api("DELETE", `/v1/secrets/${encodeURIComponent(s.name)}`)); toast.success(`Removed ${s.name}`); } catch (e) { setErr((e as Error).message); }
              }}><Trash2 />{armed === s.name ? "Click to remove" : "Remove"}</Button>
            )}
          </div>
        ))}
      </div>
      <form className="grid gap-4 sm:grid-cols-[1fr_1fr_auto] sm:items-end" onSubmit={async (e) => {
        e.preventDefault(); setErr("");
        try { setData(await api("PUT", `/v1/secrets/${encodeURIComponent(name)}`, { value })); toast.success(`Saved ${name}`); setName(""); setValue(""); }
        catch (er) { setErr((er as Error).message); }
      }}>
        <Field label="Name" htmlFor="secret-name"><Input id="secret-name" className="font-mono" placeholder="SCHEDULING_KEY" value={name} maxLength={64}
          onChange={(e) => setName(e.target.value.toUpperCase().replace(/[^A-Z0-9_]/g, "_"))} /></Field>
        <Field label="Value" htmlFor="secret-value"><Input id="secret-value" type="password" autoComplete="off" placeholder="Paste the value" value={value} onChange={(e) => setValue(e.target.value)} /></Field>
        <Button type="submit" disabled={!name || !value}>Save secret</Button>
      </form>
      {err && <p className="text-sm text-bad">{err}</p>}
    </div>
  );
}

/* ---------------------------------------------------------------- the gallery */

type Category = "model" | "listening" | "speaking" | "phone" | "contact" | "messaging" | "apps" | "automation" | "more";
type Provider = {
  key: string; icon: string; name: string; blurb: string; category: Category;
  chooser?: "llm" | "stt" | "tts"; value?: string; check?: string; groups?: Group[]; body?: ReactNode; footer?: boolean;
  configured: boolean; tag?: string; label?: string;    // label: a neutral badge instead of Connected / Not set up
};
const SECTIONS: { id: Category; title: string; lead: string; chooser?: "llm" | "stt" | "tts" }[] = [
  { id: "model", title: "Model", lead: "Follows each step's instructions and decides where the call goes next.", chooser: "llm" },
  { id: "listening", title: "Listening", lead: "Speech to text: turns what the caller says into text.", chooser: "stt" },
  { id: "speaking", title: "Speaking", lead: "Text to speech: the agent's voice.", chooser: "tts" },
  { id: "phone", title: "Phone numbers and SIP", lead: "Answer real calls with any of your agents: a Twilio or Telnyx number, or your own SIP trunk, phone system or SBC through jambonz." },
  { id: "contact", title: "Contact centers", lead: "Coming soon: hand calls between your agents and the contact-center platform your team already uses." },
  { id: "messaging", title: "Messaging and email", lead: "Let people chat with your agents, and let agents send messages." },
  { id: "apps", title: "Apps and tool servers", lead: "Calendars, mail and MCP servers your agents' tools can use." },
  { id: "automation", title: "Automation", lead: "Outbound calls and messages from triggers, with limits." },
  { id: "more", title: "Tools and workspace", lead: "Keys your agents' tools send, knowledge search, and how this app is named." },
];
const MODEL_TILES = 6;
const USE_LABEL = { llm: "Use for the model", stt: "Use for listening", tts: "Use for speaking" };

function ProviderForm({ p, settings, onSaved, onClose }: { p: Provider; settings: Settings; onSaved: (s: Settings) => void; onClose: () => void }) {
  const id = useId().replace(/:/g, "");
  const { refresh } = useApp();
  const inUse = !!p.chooser && settings.fields[p.chooser]?.value === p.value;
  const [use, setUse] = useState(inUse || !!(p.chooser && !settings.fields[p.chooser]?.value));
  const [changes, setChanges] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<"" | "save" | "test">("");
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const set = (k: string, v: string | undefined) => setChanges((c) => { const n = { ...c }; if (v === undefined) delete n[k]; else n[k] = v; return n; });
  const ctx: Ctx = { settings, changes, set, id };
  const body = { ...changes, ...(p.chooser && use && !inUse ? { [p.chooser]: p.value! } : {}) };

  async function save(): Promise<boolean> {
    if (!Object.keys(body).length) { setResult({ ok: true, text: "Nothing to save" }); return true; }
    setBusy("save"); setResult(null);
    try {
      const s = await api<Settings>("PUT", "/v1/settings", body);
      setChanges({}); onSaved(s); await refresh();
      setResult({ ok: true, text: "Saved" }); toast.success(`${p.name} saved`);
      return true;
    } catch (e) { setResult({ ok: false, text: (e as Error).message }); return false; } finally { setBusy(""); }
  }
  async function test() {
    if (Object.keys(body).length && !(await save())) return;
    setBusy("test"); setResult(null);
    try {
      const r = await api<{ ok: boolean; message: string }>("POST", `/v1/settings/check/${p.check}`);
      setResult({ ok: r.ok, text: r.ok ? "Connected" : r.message });
    } catch (e) { setResult({ ok: false, text: (e as Error).message }); } finally { setBusy(""); }
  }
  const canTest = !!p.check && (p.chooser !== "llm" || use || inUse);
  return (
    <div className="grid gap-5">
      {p.chooser && (inUse
        ? <Notice><span className="inline-flex items-center gap-2 text-foreground"><Lamp on />In use {p.chooser === "llm" ? "as the model" : `for ${p.chooser === "stt" ? "listening" : "speaking"}`}.</span></Notice>
        : <Toggle id={`${id}-use`} label={USE_LABEL[p.chooser]} hint="Agents switch to it on their next call after you save." checked={use} onCheckedChange={setUse} />)}
      {p.groups && <Groups ctx={ctx} groups={p.groups} />}
      {p.body}
      {p.footer !== false && (
        <div className="-mx-6 -mb-6 flex flex-wrap items-center gap-3 border-t bg-muted/30 px-6 py-4">
          <Button onClick={async () => { if (await save()) onClose(); }} disabled={!!busy}>{busy === "save" ? "Saving…" : "Save"}</Button>
          {canTest && <Button variant="outline" onClick={test} disabled={!!busy}>{busy === "test" ? "Testing…" : "Test connection"}</Button>}
          {result && <span className={cn("text-sm", result.ok ? "text-ok" : "text-bad")} role="status">{result.text}</span>}
        </div>
      )}
    </div>
  );
}

function Tile({ p, inUse, ready, onOpen }: { p: Provider; inUse: boolean; ready: boolean; onOpen: () => void }) {
  return (
    <button type="button" onClick={onOpen} data-provider={p.key}
      className={cn("group flex cursor-pointer flex-col gap-3 rounded-xl border bg-card p-4 text-left shadow-xs transition-[box-shadow,border-color] hover:border-ring/60 hover:shadow-md",
        inUse && "border-lamp/60 shadow-[0_0_0_3px_var(--lamp-soft)]")}>
      <div className="flex items-start gap-3">
        <ProviderIcon id={p.icon} />
        <div className="grid min-w-0 flex-1 gap-0.5">
          <span className="truncate font-semibold leading-tight">{p.name}</span>
          {p.tag && <span className="text-xs text-muted-foreground">{p.tag}</span>}
        </div>
        <ArrowUpRight className="size-4 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
      </div>
      <p className="line-clamp-2 flex-1 text-[13px] leading-relaxed text-muted-foreground">{p.blurb}</p>
      <div>
        {inUse ? <Badge variant={ready ? "lamp" : "warn"}><Lamp on={ready} warn={!ready} />{ready ? "In use" : "In use · needs setup"}</Badge>
          : p.label ? <Badge variant="outline">{p.label}</Badge>
          : p.configured ? <Badge variant="ok">Connected</Badge>
          : <Badge variant="outline">Not set up</Badge>}
      </div>
    </button>
  );
}

export function Integrations() {
  const { data: settings, error, setData, reload } = useLoad(() => api<Settings>("GET", "/v1/settings"));
  const { data: builtin } = useLoad(() => api<{ whisper_ready: boolean; kokoro_ready: boolean }>("GET", "/v1/builtin-speech").catch(() => null));
  const { data: moon } = useLoad(() => api<Moon>("GET", "/v1/moonshine").catch(() => null));
  const { data: secrets } = useLoad(() => api<{ secrets: unknown[] }>("GET", "/v1/secrets").catch(() => ({ secrets: [] })));
  const { refresh, health } = useApp();
  const [open, setOpen] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const [voicePick, setVoicePick] = useState(0);
  const [allModels, setAllModels] = useState(false);
  const providers = useMemo<Provider[]>(() => {
    if (!settings) return [];
    const fl = settings.fields;
    const afterDownload = async () => { await refresh(); reload(); };
    const set = (k: string) => !!fl[k]?.set;
    return [
      { key: "anthropic", icon: "anthropic", name: "Claude", tag: "Anthropic", category: "model", chooser: "llm", value: "anthropic", check: "llm",
        blurb: "Claude models. Haiku is fast and inexpensive for calls.", configured: set("anthropic_key"),
        groups: [f("anthropic_key", { hint: "From console.anthropic.com → API keys." }), row({ field: "model", opts: { hint: "claude-haiku-5-5 is fast and cheap for calls.", fresh: fl.llm?.value !== "anthropic", placeholder: "claude-haiku-5-5" } }, { field: "temperature", opts: { placeholder: "provider default" } })] },
      { key: "openai", icon: "openai", name: "OpenAI-compatible", tag: "Any compatible endpoint", category: "model", chooser: "llm", value: "openai", check: "llm",
        blurb: "OpenAI, a gateway, or a model running on this computer.", configured: !!fl.base_url?.value || set("openai_key"),
        groups: [f("base_url", { placeholder: "https://api.openai.com/v1", hint: "Any OpenAI-compatible endpoint, including a local server." }), f("openai_key", { placeholder: "Not needed for most local servers" }), row({ field: "model", opts: { fresh: fl.llm?.value !== "openai", placeholder: "gpt-4.1-mini" } }, { field: "temperature", opts: { placeholder: "provider default" } })] },
      ...(settings.llm_providers || []).filter((p) => p.name !== "anthropic" && p.name !== "openai").map<Provider>((p) => ({
        key: `llm-${p.name}`, icon: p.name, name: p.label, tag: p.tag, category: "model", chooser: "llm", value: p.name, check: "llm",
        blurb: p.blurb, configured: (p.key ? set(p.key) : false) || (p.name === "ollama" && fl.llm?.value === "ollama") || (p.name === "vertex" && !!fl.vertex_project?.value),
        groups: [
          ...(p.key ? [f(p.key, { hint: p.key_hint || undefined, placeholder: p.name === "bedrock" ? "Optional" : undefined })] : []),
          ...p.fields.map((name) => f(name, LLM_FIELD_OPTS[name])),
          row({ field: "model", opts: { label: p.name === "azure" ? "Deployment name" : undefined, fresh: fl.llm?.value !== p.name, placeholder: p.model,
            hint: fl.llm?.value === p.name ? undefined : `Empty: ${p.model}.` } }, { field: "temperature", opts: { placeholder: "provider default" } }),
        ],
      })),
      { key: "stt-whisper", icon: "whisper", name: "Whisper", tag: "On this computer · free", category: "listening", chooser: "stt", value: "whisper", check: "whisper",
        blurb: "Runs inside the engine. Private, no account, downloads once.", configured: !!builtin?.whisper_ready,
        groups: [node(<BuiltinPanel onReady={afterDownload} />), f("local_whisper_model", { labels: WHISPER_SIZES, hint: "Save after changing it, then download." })] },
      { key: "stt-moonshine", icon: "moonshine", name: "Moonshine", tag: "On this computer · free · streaming", category: "listening", chooser: "stt", value: "moonshine", check: "moonshine",
        blurb: "Transcribes while the caller talks, so the agent answers sooner. English.", configured: !!moon?.ready,
        groups: [node(<MoonshinePanel onReady={afterDownload} />),
          f("moonshine_model", { labels: { "small-streaming": "Small: more accurate (140 MB)", "tiny-streaming": "Tiny: fastest, lightest on the CPU (46 MB)" },
            hint: "Save after changing it, then download. Each call uses about half a CPU core while the caller talks (tiny: a third)." })] },
      { key: "stt-deepgram", icon: "deepgram", name: "Deepgram", tag: "Cloud", category: "listening", chooser: "stt", value: "deepgram", check: "deepgram",
        blurb: "Fast streaming speech to text. New accounts get free credit.", configured: set("deepgram_key"),
        groups: [f("deepgram_key", { hint: "New Deepgram accounts get free credit. The same key works for speaking." }), f("stt_model")] },
      { key: "stt-fish", icon: "fish", name: "Fish Audio", tag: "Cloud", category: "listening", chooser: "stt", value: "fish", check: "fish",
        blurb: "Transcription with automatic language detection. $0.36 per audio hour.", configured: set("fish_key"),
        groups: [f("fish_key", { hint: "The same Fish Audio key works for speaking." }),
          row({ field: "fish_stt_model", opts: { labels: FISH_STT } }, { field: "fish_stt_language", opts: { placeholder: "detect automatically", hint: "Two letters, like en or es. Empty: Fish detects it." } })] },
      { key: "stt-whisper-server", icon: "whisper-server", name: "Whisper server", tag: "Advanced · Docker", category: "listening", chooser: "stt", value: "whisper-server", check: "whisper-server",
        blurb: "Whisper in a separate container, or any compatible server.", configured: false,
        groups: [node(<LocalSpeechPanel lead="Runs Whisper in Docker. Most people want Whisper on this computer instead." />), row({ field: "whisper_url" }, { field: "whisper_model" })] },
      { key: "turns", icon: "turns", name: "Turn taking", category: "listening", configured: true,
        tag: fl.turn_detection?.value === "pause" ? "Fixed pause" : "Smart turn", label: fl.turn_detection?.value === "pause" ? "Fixed pause" : "Smart turn",
        blurb: "How a call decides the caller has finished speaking, so the agent answers without cutting them off.",
        groups: [f("turn_detection", { labels: { smart: "Smart turn: listens to how the caller sounds, on this computer (recommended)", pause: "Fixed pause: answers after a set silence" } }),
          f("turn_pause", { placeholder: fl.turn_detection?.value === "pause" ? "0.8" : "3",
            hint: fl.turn_detection?.value === "pause"
              ? "The silence that ends the caller's turn. Shorter replies faster but may cut people off."
              : "The longest the agent waits when the caller pauses mid-thought, before answering anyway." })] },
      { key: "tts-kokoro", icon: "kokoro", name: "Kokoro", tag: "On this computer · free", category: "speaking", chooser: "tts", value: "kokoro", check: "kokoro",
        blurb: "Natural voices that run inside the engine. Private and free.", configured: !!builtin?.kokoro_ready,
        groups: [node(<BuiltinPanel onReady={afterDownload} />), f("kokoro_voice", { hint: "For example af_heart, af_bella, am_adam or bf_emma." })] },
      { key: "tts-deepgram", icon: "deepgram", name: "Deepgram", tag: "Cloud", category: "speaking", chooser: "tts", value: "deepgram", check: "deepgram",
        blurb: "Aura voices with low latency for calls.", configured: set("deepgram_key"),
        groups: [f("deepgram_key"), f("tts_voice", { hint: "For example aura-2-helena-en or aura-2-orion-en." })] },
      { key: "tts-fish", icon: "fish", name: "Fish Audio", tag: "Cloud · free model", category: "speaking", chooser: "tts", value: "fish", check: "fish",
        blurb: "Expressive voices and voice cloning. s2.1-pro-free costs nothing through Nov 30, 2026.", configured: set("fish_key"),
        groups: [
          node(<Notice><Info className="mt-0.5 size-4 shrink-0" /><span>The s2.1-pro-free model costs nothing during Fish Audio's promotion (through Nov 30, 2026). Free-tier requests may be used to improve their models, so use test data only.</span></Notice>),
          f("fish_key", { hint: "From fish.audio → API keys. The same key works for listening." }),
          node(<FishVoicePicker key={voicePick} settings={settings} onPick={async (v) => { try { setData(await api<Settings>("PUT", "/v1/settings", { fish_voice: v })); setVoicePick((n) => n + 1); toast.success("Voice saved"); } catch (e) { toast.error((e as Error).message); } }} />),
          row({ field: "fish_voice", opts: { placeholder: "Default voice", hint: "Or paste any voice's ID from fish.audio." } }, { field: "fish_model", opts: { labels: FISH_MODELS } }),
          { details: "Voice settings", items: [
            f("fish_connection", { labels: { auto: "auto: HTTP, which works with the free model (recommended)", http: "HTTP", live: "live stream: a little faster, needs API credit even for the free model" } }),
            row({ field: "fish_latency", opts: { labels: FISH_LATENCY } }, { field: "fish_normalize", opts: { labels: { on: "on: “$12” is read as “twelve dollars”", off: "off" } } }),
            row({ field: "fish_speed", opts: { placeholder: "1.0", hint: "0.5 to 2.0" } }, { field: "fish_volume", opts: { placeholder: "0", hint: "-20 to 20 dB" } }),
            row({ field: "fish_temperature", opts: { placeholder: "0.7", hint: "0 to 1. Higher is more expressive, lower more consistent." } }, { field: "fish_top_p", opts: { placeholder: "0.7", hint: "0 to 1. Nucleus sampling." } }),
          ] }] },
      { key: "tts-kokoro-server", icon: "kokoro-server", name: "Kokoro server", tag: "Advanced · Docker", category: "speaking", chooser: "tts", value: "kokoro-server", check: "kokoro-server",
        blurb: "Kokoro in a separate container, or any compatible server.", configured: false,
        groups: [node(<LocalSpeechPanel lead="Runs Kokoro in Docker. Most people want Kokoro on this computer instead." />), row({ field: "kokoro_url" }, { field: "kokoro_voice", opts: { hint: "For example af_heart, af_bella or am_adam." } })] },
      { key: "twilio", icon: "twilio", name: "Twilio", tag: "Phone numbers", category: "phone", configured: set("twilio_auth_token"),
        blurb: "Answer calls to a Twilio number. Checks every webhook against your auth token.",
        groups: [f("twilio_auth_token", { hint: "Twilio Console → Account info → Auth token." }), node(<PhoneLink />),
          { details: "Outbound calls (triggers)", items: [row({ field: "twilio_account_sid", opts: { placeholder: "AC…" } }, { field: "twilio_from_number", opts: { placeholder: "+15551234567" } }),
            node(<p className="text-[12.5px] text-muted-foreground">Calls from schedules and the API trigger come from this number. They need the engine's public address (a tunnel) and Outbound turned on under Automation.</p>)] }] },
      { key: "telnyx", icon: "telnyx", name: "Telnyx", tag: "Phone numbers", category: "phone", configured: set("telnyx_public_key"),
        blurb: "Answer calls to a Telnyx number through a TeXML application.",
        groups: [f("telnyx_public_key", { hint: "Mission Control Portal → Keys & Credentials → Public Key." }), f("telnyx_api_key", { hint: "Optional: lets the engine hang up when the agent finishes. Needed for outbound calls." }), node(<PhoneLink />),
          { details: "Outbound calls (triggers)", items: [row({ field: "telnyx_texml_app_id" }, { field: "telnyx_from_number", opts: { placeholder: "+15551234567" } })] }] },
      { key: "jambonz", icon: "jambonz", name: "jambonz", tag: "SIP trunks, phone systems, SBCs", category: "phone", configured: set("jambonz_webhook_secret"),
        blurb: "Answer calls from any SIP trunk, PBX or session border controller through jambonz, open source and self-hostable.",
        groups: [f("jambonz_webhook_secret", { hint: "In jambonz: Account → Webhook secret. Used to check that calls really come from your jambonz." }),
          f("jambonz_sample_rate", { labels: { "8000": "Phone audio, 8 kHz (works with every trunk)", "16000": "HD voice, 16 kHz (G.722, Opus or wideband SIP)" },
            hint: "HD voice is clearer for listening. Use it when your trunk or phone system carries wideband audio; otherwise it only costs bandwidth." }),
          node(<PhoneLink />)] },
      ...CONTACT_CENTERS.map<Provider>((c) => ({ key: c.key, icon: c.key, name: c.name, tag: c.tag, category: "contact", blurb: c.blurb,
        configured: false, footer: false, label: "Coming soon",
        body: <Notice><Info className="mt-0.5 size-4 shrink-0" /><span>{c.how} {c.key === "twilio-flex"
          ? "Until then, the Twilio connection answers calls to your Twilio numbers."
          : `Until then, ${c.name} can usually send calls to your agents over a SIP trunk through the jambonz connection.`}</span></Notice> })),
      { key: "telegram", icon: "telegram", name: "Telegram", tag: "Bot", category: "messaging", check: "telegram", configured: set("telegram_bot_token"),
        blurb: "People chat with an agent through your Telegram bot; agents can send Telegram messages.",
        groups: [node(<Steps items={["In Telegram, message @BotFather and send /newbot.", "Paste the token it gives you below and save.",
          "In the builder, add the trigger “Telegram messages” to the agent that should answer. No public address needed: the engine checks for new messages itself."]} />),
          f("telegram_bot_token"), node(<ChannelOwner channel="telegram" />)] },
      { key: "whatsapp", icon: "whatsapp", name: "WhatsApp", tag: "Cloud API", category: "messaging", check: "whatsapp", configured: set("whatsapp_token") && !!fl.whatsapp_phone_number_id?.value,
        blurb: "Chat with an agent on WhatsApp through Meta's Cloud API; agents can send WhatsApp messages.",
        groups: [node(<Steps items={["Create a Meta app with WhatsApp, and note the phone number ID and a permanent access token.",
          "Save them below, with the app secret (it checks that messages really come from Meta) and a verify token you make up.",
          "In the app's WhatsApp → Configuration, set the callback URL below and the same verify token; subscribe to messages.",
          "Add the trigger “WhatsApp messages” to the agent that should answer."]} />),
          row({ field: "whatsapp_phone_number_id" }, { field: "whatsapp_token" }), row({ field: "whatsapp_app_secret" }, { field: "whatsapp_verify_token" }),
          node(<WebhookAddress path="/v1/whatsapp/webhook" />),
          node(<Notice><Info className="mt-0.5 size-4 shrink-0" /><span>WhatsApp lets a business message someone freely for 24 hours after they last wrote. Outside that window (reminders, for example) Meta only allows approved message templates.</span></Notice>)] },
      { key: "email", icon: "email", name: "Email", tag: fl.email_provider?.value === "gmail" ? "Gmail" : "SMTP and IMAP", category: "messaging", check: "email",
        configured: !!settings.status.email_ready,
        blurb: "Agents answer emails and send them: through Gmail, or any mail server.",
        groups: [f("email_provider", { labels: { smtp: "A mail server (SMTP and IMAP)", gmail: "Gmail, through the Google connection" } }),
          node(<p className="text-[12.5px] text-muted-foreground">With Gmail, connect Google (in Apps) and nothing else is needed here. For a mail server, an app password works with most providers, including Gmail and Outlook.</p>),
          row({ field: "email_address", opts: { placeholder: "desk@example.com" } }, { field: "email_password" }),
          row({ field: "smtp_host", opts: { placeholder: "smtp.example.com" } }, { field: "smtp_port", opts: { placeholder: "587" } }),
          f("imap_host", { placeholder: "imap.example.com (for answering emails)" }), node(<ChannelOwner channel="email" />)] },
      { key: "google", icon: "google", name: "Google Calendar and Gmail", tag: fl.google_account?.value || "Your own Google sign-in", category: "apps", check: "google",
        configured: !!settings.status.google_ready,
        blurb: "Find free times and book appointments; send and answer email with Gmail.",
        groups: [node(<GoogleConnect settings={settings} onChange={setData} />),
          row({ field: "google_client_id" }, { field: "google_client_secret" }),
          { details: "Calendar", items: [row({ field: "calendar_id", opts: { placeholder: "primary", hint: "primary, or a calendar's ID from its settings." } }, { field: "timezone", opts: { placeholder: "this computer's", hint: "Like America/New_York." } }),
            row({ field: "calendar_hours", opts: { placeholder: "09:00-17:00" } }, { field: "calendar_days", opts: { placeholder: "mon-fri" } })] }] },
      { key: "mcp", icon: "mcp", name: "MCP servers", tag: "Model Context Protocol", category: "apps", configured: false, footer: false, label: "Add in the builder",
        blurb: "Give an agent the tools of any MCP server over Streamable HTTP.",
        body: <Steps items={["Open an agent in the builder and choose New tool → MCP server.", "Enter the server's URL; for a key, add a header like Authorization: Bearer {{secrets.NAME}} and save the key under Tool secrets.",
          "Press Refresh to load its tools, then tick the server on the steps that may use them."]} /> },
      { key: "automation", icon: "automation", name: "Outbound and limits", tag: settings.fields.outbound?.value === "on" ? "Outbound on" : "Outbound off", category: "automation",
        configured: settings.fields.outbound?.value === "on",
        blurb: "Lets schedules and the API trigger call or message people, within quiet hours and a daily limit.",
        groups: [f("outbound", { labels: { off: "Off: triggers can't contact anyone (recommended until you've tested)", on: "On" } }),
          row({ field: "quiet_hours", opts: { placeholder: "21:00-08:00", hint: "No outbound calls or messages between these times." } }, { field: "outbound_daily_cap", opts: { placeholder: "50", hint: "Per agent, per day." } }),
          f("timezone", { placeholder: "this computer's", hint: "Used for schedules, quiet hours and calendars. Like America/New_York." }),
          node(<AutomationActivity />)] },
      { key: "knowledge", icon: "knowledge", name: "Knowledge search", category: "more",
        tag: fl.embeddings?.value === "off" ? "Keywords only" : fl.embeddings?.value === "ollama" ? "Ollama embeddings" : "On this computer",
        configured: true, label: fl.embeddings?.value === "off" ? "Keywords only" : "Keywords and meaning",
        blurb: "How knowledge bases are searched: by keywords always, and by meaning with an embedding model.",
        groups: [f("embeddings", { labels: { local: "On this computer (MiniLM, 90 MB download on the Knowledge page)", ollama: "An Ollama or other OpenAI-compatible server", off: "Keywords only" } }),
          row({ field: "embeddings_url", opts: { placeholder: "http://localhost:11434/v1", hint: "Only for the server option." } },
              { field: "embeddings_model", opts: { placeholder: "nomic-embed-text" } }),
          node(<p className="text-[12.5px] text-muted-foreground">Changing the model re-indexes knowledge bases the next time you open the Knowledge page.</p>)] },
      { key: "secrets", icon: "secrets", name: "Tool secrets", tag: `${secrets?.secrets.length || 0} saved`, category: "more", configured: !!secrets?.secrets.length, footer: false,
        blurb: "API keys your agents' tools send, written as {{secrets.NAME}}.", body: <SecretsPanel /> },
      { key: "workspace", icon: "workspace", name: "Workspace", tag: health?.brand || "", category: "more", configured: true,
        blurb: "The name shown across this app.", groups: [f("brand_name")] },
    ];
  }, [settings, builtin, moon, secrets, voicePick, health?.brand]); // eslint-disable-line

  if (error) return <Page title="Integrations"><p className="text-sm text-bad">{error}</p></Page>;
  if (!settings) return <Page title="Integrations"><p className="text-sm text-muted-foreground">Loading…</p></Page>;
  const st = settings.status;
  const ready = (p: Provider) => p.chooser === "llm" ? !!st.llm_ready : p.chooser === "stt" ? !!st.stt_ready : p.chooser === "tts" ? !!st.tts_ready : p.configured;
  const inUse = (p: Provider) => !!p.chooser && settings.fields[p.chooser]?.value === p.value;
  const shown = providers.filter((p) => !q || `${p.name} ${p.tag} ${p.blurb}`.toLowerCase().includes(q.toLowerCase()));
  const current = providers.find((p) => p.key === open) || null;

  return (
    <Page wide title="Integrations" description="The services your agents use. Saved on this computer only, never in your agents or the repo."
      actions={<div className="relative w-64"><Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
        <Input className="pl-8" placeholder="Search integrations" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search integrations" /></div>}>
      <Notice className="mb-8"><Info className="mt-0.5 size-4 shrink-0" /><span>Saved to <code className="text-foreground">{settings.file}</code>. Values from <code className="text-foreground">.env</code> apply until you save one here; keys are never shown again once saved.</span></Notice>
      <div className="grid gap-10">
        {SECTIONS.map((sec) => {
          const all = shown.filter((p) => p.category === sec.id);
          if (!all.length) return null;
          // Many model providers: show the first few, plus any in use or set up, until "Show all".
          const folded = sec.id === "model" && !q && !allModels && all.length > MODEL_TILES;
          const items = folded ? all.filter((p, i) => i < MODEL_TILES || inUse(p) || p.configured) : all;
          const using = sec.chooser ? providers.find((p) => p.chooser === sec.chooser && inUse(p)) : null;
          return (
            <section key={sec.id} aria-labelledby={`sec-${sec.id}`}>
              <div className="mb-3 flex flex-wrap items-baseline gap-x-3 gap-y-1">
                <h2 id={`sec-${sec.id}`} className="text-base font-semibold">{sec.title}</h2>
                <p className="text-sm text-muted-foreground">{sec.lead}</p>
                {using && <span className="ml-auto text-[13px] text-muted-foreground">Using <span className="font-medium text-foreground">{using.name}</span></span>}
              </div>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-4">
                {items.map((p) => <Tile key={p.key} p={p} inUse={inUse(p)} ready={ready(p)} onOpen={() => setOpen(p.key)} />)}
              </div>
              {sec.id === "model" && !q && all.length > MODEL_TILES && (
                <Button variant="ghost" size="sm" className="mt-2" aria-expanded={!folded} onClick={() => setAllModels(!allModels)}>
                  {folded ? `Show all ${all.length} model providers` : "Show fewer"}<ChevronDown className={cn("transition-transform", !folded && "rotate-180")} />
                </Button>
              )}
            </section>
          );
        })}
        {!shown.length && <p className="text-sm text-muted-foreground">No integration matches that search.</p>}
      </div>
      <Dialog open={!!current} onOpenChange={(v) => !v && setOpen(null)}>
        {current && (
          <DialogContent className="max-h-[88vh] max-w-2xl overflow-y-auto">
            <DialogHeader className="flex flex-row items-center gap-3 pr-6">
              <ProviderIcon id={current.icon} size="lg" />
              <div className="grid gap-1">
                <DialogTitle>{current.name}</DialogTitle>
                <DialogDescription>{current.blurb}</DialogDescription>
              </div>
            </DialogHeader>
            <ProviderForm key={current.key} p={current} settings={settings} onSaved={setData} onClose={() => setOpen(null)} />
          </DialogContent>
        )}
      </Dialog>
    </Page>
  );
}

function Steps({ items }: { items: ReactNode[] }) {
  return <ol className="grid list-decimal gap-1.5 pl-5 text-[13px] leading-relaxed text-muted-foreground">{items.map((x, i) => <li key={i}>{x}</li>)}</ol>;
}

function WebhookAddress({ path }: { path: string }) {
  const { health } = useApp();
  const base = health?.phone_webhook ? health.phone_webhook.replace(/\/v1\/twilio\/voice$/, "") : "";
  return (
    <Field label="Callback URL">
      {base ? <code className="block break-all rounded-md border bg-muted/50 px-3 py-2 text-[12.5px]">{base}{path}</code>
        : <p className="text-[12.5px] text-muted-foreground">Meta needs a public https address. Start the engine with a tunnel (<code>scripts/engine-up.sh --tunnel</code>) and this shows the URL to paste.</p>}
    </Field>
  );
}

type Overview = { channels: Record<string, string | null>; log: { at: string; agent: string; trigger: string; what: string; ok: boolean; detail: string; to: string }[];
  agents: Record<string, { name: string }> };

function ChannelOwner({ channel }: { channel: string }) {
  const [o, setO] = useState<Overview | null>(null);
  useEffect(() => { api<Overview>("GET", "/v1/automation").then(setO).catch(() => {}); }, []);
  if (!o) return null;
  const slug = o.channels[channel];
  return (
    <p className="text-[12.5px] text-muted-foreground">
      {slug ? <>Answered by <a className="font-medium text-foreground underline" href={`#/build/${encodeURIComponent(slug)}`}>{o.agents[slug]?.name || slug}</a>.</>
        : <>No agent answers yet. In the builder, add a “{channel === "email" ? "Emails" : channel === "telegram" ? "Telegram messages" : "WhatsApp messages"}” trigger to one.</>}
    </p>
  );
}

function AutomationActivity() {
  const [o, setO] = useState<Overview | null>(null);
  useEffect(() => { api<Overview>("GET", "/v1/automation").then(setO).catch(() => {}); }, []);
  if (!o) return null;
  return (
    <Field label="Recent activity">
      {o.log.length === 0 ? <p className="text-[12.5px] text-muted-foreground">Nothing yet. Triggers' calls, messages and notes show up here.</p> : (
        <ul className="grid max-h-60 gap-1 overflow-y-auto rounded-md border p-2 text-[12.5px]">
          {o.log.slice(0, 40).map((e, i) => (
            <li key={i} className="flex gap-2">
              <span className={cn("mt-1.5 size-1.5 shrink-0 rounded-full", e.ok ? "bg-ok" : "bg-bad")} />
              <span className="min-w-0"><span className="text-muted-foreground">{new Date(e.at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</span>{" "}
                <span className="font-medium">{o.agents[e.agent]?.name || e.agent}</span> · {e.what}{e.to && ` to ${e.to}`}{e.detail && <span className="text-muted-foreground"> · {e.detail}</span>}</span>
            </li>
          ))}
        </ul>
      )}
    </Field>
  );
}

function GoogleConnect({ settings, onChange }: { settings: Settings; onChange: (s: Settings) => void }) {
  const fl = settings.fields;
  const [busy, setBusy] = useState(false);
  const connected = !!fl.google_refresh_token?.set;
  const account = fl.google_account?.value;
  async function connect() {
    setBusy(true);
    try {
      const r = await api<{ url: string }>("POST", "/v1/oauth/google/start");
      window.open(r.url, "_blank", "noopener");
      toast("Finish signing in in the new tab", { description: "Then come back and press Refresh." });
    } catch (e) { toast.error((e as Error).message); } finally { setBusy(false); }
  }
  return (
    <div className="grid gap-3">
      <Steps items={[<>In Google Cloud console, enable the Google Calendar API and the Gmail API, and set up the OAuth consent screen (add yourself as a test user).</>,
        <>Create an OAuth client of type <span className="font-medium text-foreground">Desktop app</span>, paste its ID and secret below, and save.</>,
        <>Press Connect and sign in. Open this app at <code>http://localhost</code> for that step.</>]} />
      <div className="flex flex-wrap items-center gap-2">
        {connected ? <Badge variant="ok">Connected{account ? ` as ${account}` : ""}</Badge> : <Badge variant="outline">Not connected</Badge>}
        <Button size="sm" variant={connected ? "outline" : "default"} onClick={connect} disabled={busy || !fl.google_client_id?.value || !fl.google_client_secret?.set}>
          {connected ? "Connect again" : "Connect Google account"}</Button>
        <Button size="sm" variant="ghost" onClick={async () => onChange(await api<Settings>("GET", "/v1/settings"))}>Refresh</Button>
        {connected && <Button size="sm" variant="ghost" className="text-bad" onClick={async () => {
          if (!confirm("Disconnect Google? Calendar tools and Gmail stop working until you connect again.")) return;
          onChange(await api<Settings>("PUT", "/v1/settings", { google_refresh_token: "", google_account: "" })); toast.success("Google disconnected");
        }}>Disconnect</Button>}
      </div>
      {(!fl.google_client_id?.value || !fl.google_client_secret?.set) && <p className="text-[12.5px] text-muted-foreground">Save the client ID and secret first.</p>}
    </div>
  );
}

function PhoneLink() {
  return <Button asChild variant="outline" size="sm" className="justify-self-start"><a href="#/phone">Webhook address and setup steps<ArrowUpRight /></a></Button>;
}
