import { useEffect, useState } from "react";
import { History, Phone, PhoneForwarded, Search, Trash2, MessageSquare, Globe, Download } from "lucide-react";
import { toast } from "sonner";
import { api, blobUrl, download } from "@/lib/api";
import { useApp, useLoad } from "@/lib/app";
import { cn } from "@/lib/utils";
import { Notice, Page } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

type Call = {
  id: string; agent: string; agent_name: string; channel: string; contact: string; started: number; ended: number;
  duration_seconds: number; outcome: string; transferred_to: string; steps: string[]; collected: Record<string, unknown>;
  turns: number; response_ms: number | null; recording: boolean;
};
type CallDetail = Call & { transcript: { who: "caller" | "agent"; text: string; t: number }[]; events: Record<string, any>[] };
type List = { calls: Call[]; keeping: boolean; days: number; recordings: boolean };

const CHANNELS: Record<string, string> = { phone: "Phone", browser: "Browser call", chat: "Test chat", telegram: "Telegram", whatsapp: "WhatsApp", email: "Email" };
const ICON: Record<string, typeof Phone> = { phone: Phone, browser: Globe };

const when = (s: number) => new Date(s * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const length = (s: number) => (s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`);
const ended = (c: Call) => (c.transferred_to ? `Transferred to ${c.transferred_to}`
  : c.outcome === "end step reached" ? "By the agent"
  : !c.outcome || c.outcome === "disconnected" ? "Caller left" : c.outcome);
const show = (v: unknown) => (typeof v === "string" ? v : JSON.stringify(v));

function Outcome({ c }: { c: Call }) {
  if (c.transferred_to) return <Badge variant="lamp"><PhoneForwarded />Transferred</Badge>;
  if (c.outcome === "error") return <Badge variant="bad">Error</Badge>;
  if (!c.outcome || c.outcome === "disconnected") return <Badge variant="outline">Hung up</Badge>;
  return <Badge variant="ok">Finished</Badge>;
}

export function CallsPage() {
  const [q, setQ] = useState(""), [query, setQuery] = useState(""), [channel, setChannel] = useState("");
  const [sel, setSel] = useState("");
  const { data, reload } = useLoad(() => api<List>("GET", `/v1/calls?limit=100&channel=${channel}&q=${encodeURIComponent(query)}`), [channel, query]);
  useEffect(() => { const t = setTimeout(() => setQuery(q.trim()), 300); return () => clearTimeout(t); }, [q]);
  const calls = data?.calls || [];
  useEffect(() => { if (calls.length && !calls.some((c) => c.id === sel)) setSel(calls[0].id); }, [calls, sel]);
  return (
    <Page title="Calls" wide
      description="Every finished conversation: who it was with, what the agent collected, how it ended, and what was said. Kept on this computer only.">
      {data && !data.keeping && (
        <Notice tone="warn" className="mb-5"><History className="mt-0.5 size-4 shrink-0" /><span>Call history is off, so new calls aren't kept. Turn it on in Integrations → Call history.</span></Notice>
      )}
      {data?.keeping && (
        <Notice className="mb-5"><History className="mt-0.5 size-4 shrink-0" />
          <span>Calls are kept for {data.days} days. {data.recordings ? "Voice calls are recorded." : "Recordings are off."} Change this in Integrations → Call history. Use synthetic test data, not real callers' personal or health details.</span>
        </Notice>
      )}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <div className="relative min-w-[220px] flex-1 sm:max-w-sm">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input aria-label="Search calls" className="pl-8" placeholder="Search numbers, details and what was said" value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
        <div role="group" aria-label="Channel" className="flex flex-wrap gap-1">
          {["", "phone", "browser", "chat", "telegram", "whatsapp", "email"].map((ch) => (
            <button key={ch || "all"} type="button" onClick={() => setChannel(ch)} aria-pressed={channel === ch}
              className={cn("rounded-full border px-2.5 py-1 text-xs transition-colors", channel === ch ? "border-lamp bg-lamp-soft" : "hover:bg-accent")}>
              {ch ? CHANNELS[ch] : "All"}
            </button>
          ))}
        </div>
      </div>
      {data && !calls.length ? (
        <Card className="grid place-items-center gap-3 px-6 py-14 text-center">
          <History className="size-8 text-muted-foreground" />
          <p className="font-medium">{query || channel ? "No calls match" : "No calls yet"}</p>
          <p className="max-w-md text-sm text-muted-foreground">{query || channel ? "Try another search or channel." : "Calls and chats show up here when they end. Try one on the Test call page."}</p>
        </Card>
      ) : (
        <div className="grid items-start gap-5 lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
          <ul aria-label="Calls" className="grid gap-1.5">
            {calls.map((c) => {
              const Icon = ICON[c.channel] || MessageSquare;
              return (
                <li key={c.id}>
                  <button type="button" onClick={() => setSel(c.id)} aria-current={sel === c.id || undefined}
                    className={cn("grid w-full gap-1 rounded-lg border px-3 py-2.5 text-left text-sm transition-colors",
                      sel === c.id ? "border-lamp bg-lamp-soft" : "hover:bg-accent")}>
                    <span className="flex items-center gap-2">
                      <Icon className="size-4 shrink-0 text-muted-foreground" />
                      <span className="truncate font-medium">{c.contact || CHANNELS[c.channel] || c.channel}</span>
                      <span className="ml-auto shrink-0 text-xs text-muted-foreground">{when(c.started)}</span>
                    </span>
                    <span className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
                      <span className="truncate">{c.agent_name}</span>·<span>{length(c.duration_seconds)}</span>
                      <span className="ml-auto"><Outcome c={c} /></span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
          {sel && <CallView key={sel} id={sel} onDeleted={() => { setSel(""); reload(); }} />}
        </div>
      )}
    </Page>
  );
}

function CallView({ id, onDeleted }: { id: string; onDeleted: () => void }) {
  const { me } = useApp();
  const { data: c, error } = useLoad(() => api<CallDetail>("GET", `/v1/calls/${id}`), [id]);
  const [audio, setAudio] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  useEffect(() => {
    if (!c?.recording) return;
    let url: string | null = null;
    blobUrl(`/v1/calls/${id}/recording`).then((u) => { url = u; setAudio(u); }).catch(() => setAudio(null));
    return () => { if (url) URL.revokeObjectURL(url); };
  }, [c?.recording, id]);
  if (error) return <Notice tone="bad">{error}</Notice>;
  if (!c) return null;
  const details = Object.entries(c.collected);
  const tools = c.events.filter((e) => e.type === "tool_call_result" && !e.result?.next_step);   // not step changes
  async function remove() {
    try { await api("DELETE", `/v1/calls/${id}`); toast.success("Call deleted"); onDeleted(); }
    catch (x) { toast.error((x as Error).message); }
  }
  return (
    <Card className="grid gap-5 p-5">
      <header className="flex flex-wrap items-start gap-3">
        <div className="grid gap-0.5">
          <h2 className="text-lg font-semibold">{c.contact || CHANNELS[c.channel] || c.channel}</h2>
          <p className="text-sm text-muted-foreground">{c.agent_name} · {CHANNELS[c.channel] || c.channel} · {when(c.started)} · {length(c.duration_seconds)}</p>
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Outcome c={c} />
          {me?.role === "admin" && <Button size="sm" variant="outline" onClick={() => setConfirm(true)} aria-label="Delete this call"><Trash2 /></Button>}
        </div>
      </header>
      <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-4">
        <Stat label="Caller turns" value={String(c.turns)} />
        <Stat label="Average response" value={c.response_ms ? `${(c.response_ms / 1000).toFixed(2)} s` : "—"} />
        <Stat label="Steps" value={String(c.steps.length)} />
        <Stat label="Ended" value={ended(c)} />
      </dl>
      {c.recording && (
        <section className="grid gap-2">
          <h3 className="text-sm font-medium">Recording</h3>
          <div className="flex flex-wrap items-center gap-2">
            {audio ? <audio controls src={audio} className="h-9 min-w-0 flex-1" /> : <span className="text-sm text-muted-foreground">Loading…</span>}
            <Button size="sm" variant="outline" onClick={() => download(`/v1/calls/${id}/recording`, `call-${id}.wav`).catch((x) => toast.error(x.message))}><Download />WAV</Button>
          </div>
          <p className="text-xs text-muted-foreground">Caller on the left channel, agent on the right.</p>
        </section>
      )}
      <section className="grid gap-2">
        <h3 className="text-sm font-medium">Collected</h3>
        {details.length ? (
          <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[max-content_1fr]">
            {details.map(([k, v]) => (
              <div key={k} className="contents"><dt className="font-mono text-xs text-muted-foreground sm:pt-0.5">{k}</dt><dd className="break-words">{show(v)}</dd></div>
            ))}
          </dl>
        ) : <p className="text-sm text-muted-foreground">Nothing collected.</p>}
      </section>
      {c.steps.length > 0 && (
        <section className="grid gap-2">
          <h3 className="text-sm font-medium">Steps</h3>
          <ol className="flex flex-wrap items-center gap-1.5 text-xs">
            {c.steps.map((s, i) => <li key={i} className="flex items-center gap-1.5">{i > 0 && <span aria-hidden className="text-muted-foreground">→</span>}<Badge variant="outline">{s}</Badge></li>)}
          </ol>
        </section>
      )}
      {tools.length > 0 && (
        <section className="grid gap-2">
          <h3 className="text-sm font-medium">Tools</h3>
          <ul className="grid gap-1 text-sm">
            {tools.map((e, i) => <li key={i} className="truncate"><span className="font-mono text-xs">{e.function_name}</span> <span className="text-muted-foreground">at {Math.round(e.t)} s</span></li>)}
          </ul>
        </section>
      )}
      <section className="grid gap-2">
        <h3 className="text-sm font-medium">Transcript</h3>
        <ol className="grid gap-2">
          {c.transcript.map((t, i) => (
            <li key={i} className={cn("flex", t.who === "caller" ? "justify-end" : "justify-start")}>
              <div className={cn("max-w-[80%] rounded-2xl px-3 py-2 text-sm", t.who === "caller" ? "bg-lamp-soft" : "bg-muted")}>
                <span className="sr-only">{t.who === "caller" ? "Caller: " : "Agent: "}</span>{t.text}
              </div>
            </li>
          ))}
          {!c.transcript.length && <li className="text-sm text-muted-foreground">Nothing was said.</li>}
        </ol>
      </section>
      <Dialog open={confirm} onOpenChange={setConfirm}>
        <DialogContent>
          <DialogHeader><DialogTitle>Delete this call?</DialogTitle>
            <DialogDescription>Its transcript, details{c.recording ? " and recording" : ""} are removed from this computer. This can't be undone.</DialogDescription></DialogHeader>
          <DialogFooter><Button variant="outline" onClick={() => setConfirm(false)}>Keep it</Button><Button variant="destructive" onClick={remove}><Trash2 />Delete</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return <div className="grid gap-0.5 rounded-lg border px-3 py-2"><dt className="text-xs text-muted-foreground">{label}</dt><dd className="truncate font-medium">{value}</dd></div>;
}
