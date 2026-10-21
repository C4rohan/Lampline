import { useEffect, useState } from "react";
import { History, Phone, PhoneForwarded, Search, Trash2, MessageSquare, Globe, Download, ClipboardCheck, RefreshCw, Repeat } from "lucide-react";
import { toast } from "sonner";
import { api, blobUrl, download, type AgentSummary } from "@/lib/api";
import { useApp, useLoad } from "@/lib/app";
import { cn, local } from "@/lib/utils";
import { go } from "@/lib/route";
import { Notice, Page } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input, NativeSelect, Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";

type Call = {
  id: string; agent: string; agent_name: string; channel: string; contact: string; started: number; ended: number;
  duration_seconds: number; outcome: string; transferred_to: string; steps: string[]; collected: Record<string, unknown>;
  turns: number; response_ms: number | null; recording: boolean;
  p50_ms: number | null; p95_ms: number | null; max_ms: number | null;
  cost: number | null; cost_detail: { total: number | null; parts: Record<string, number>; missing: string[]; usage: Record<string, number> } | null;
  grade: "" | "passed" | "failed" | "error";
  grade_results: { goal?: string; goal_met: boolean; summary: string; criteria: { criterion: string; passed: boolean; reason: string }[] } | null;
};
type Summary = { days: number; calls: number; turns: number; p50_ms: number | null; p95_ms: number | null; cost: number;
  cost_per_call: number | null; costed: number; graded: number; passed: number; failed: number };
type Checks = { agent: string; goal: string; criteria: string[]; enabled: boolean };
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
const secs = (ms: number | null | undefined) => (ms == null ? "—" : `${(ms / 1000).toFixed(2)} s`);
const money = (d: number | null | undefined) => (d == null ? "—" : d === 0 ? "$0" : d < 0.01 ? `$${d.toFixed(4)}` : `$${d.toFixed(3)}`);

function Grade({ g }: { g: Call["grade"] }) {
  if (g === "passed") return <Badge variant="ok">Checks passed</Badge>;
  if (g === "failed") return <Badge variant="bad">Checks failed</Badge>;
  if (g === "error") return <Badge variant="outline">Not graded</Badge>;
  return null;
}

function Outcome({ c }: { c: Call }) {
  if (c.transferred_to) return <Badge variant="lamp"><PhoneForwarded />Transferred</Badge>;
  if (c.outcome === "error") return <Badge variant="bad">Error</Badge>;
  if (!c.outcome || c.outcome === "disconnected") return <Badge variant="outline">Hung up</Badge>;
  return <Badge variant="ok">Finished</Badge>;
}

export function CallsPage() {
  const [q, setQ] = useState(""), [query, setQuery] = useState(""), [channel, setChannel] = useState("");
  const [sel, setSel] = useState(""), [failed, setFailed] = useState(false), [checksOpen, setChecksOpen] = useState(false);
  const { data, reload } = useLoad(() => api<List>("GET", `/v1/calls?limit=100&channel=${channel}&q=${encodeURIComponent(query)}${failed ? "&grade=failed" : ""}`), [channel, query, failed]);
  const { data: sum } = useLoad(() => api<Summary>("GET", "/v1/calls/summary?days=7"), [data]);
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
          <button type="button" onClick={() => setFailed(!failed)} aria-pressed={failed}
            className={cn("rounded-full border px-2.5 py-1 text-xs transition-colors", failed ? "border-bad bg-bad/10" : "hover:bg-accent")}>
            Failed checks
          </button>
        </div>
        <Button size="sm" variant="outline" className="ml-auto" onClick={() => setChecksOpen(true)}><ClipboardCheck />Call checks</Button>
      </div>
      {sum && sum.calls > 0 && (
        <dl aria-label="Last 7 days" className="mb-4 grid grid-cols-2 gap-3 text-sm sm:grid-cols-5">
          <Stat label="Calls, last 7 days" value={String(sum.calls)} />
          <Stat label="Typical response (p50)" value={secs(sum.p50_ms)} />
          <Stat label="Slow response (p95)" value={secs(sum.p95_ms)} />
          <Stat label="Cost per call (est.)" value={money(sum.cost_per_call)} />
          <Stat label="Passed checks" value={sum.graded ? `${sum.passed} of ${sum.graded}` : "—"} />
        </dl>
      )}
      <ChecksDialog open={checksOpen} onOpenChange={setChecksOpen} />
      {data && !calls.length ? (
        <Card className="grid place-items-center gap-3 px-6 py-14 text-center">
          <History className="size-8 text-muted-foreground" />
          <p className="font-medium">{query || channel || failed ? "No calls match" : "No calls yet"}</p>
          <p className="max-w-md text-sm text-muted-foreground">{query || channel || failed ? "Try another search or filter." : "Calls and chats show up here when they end. Try one on the Test call page."}</p>
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
                      <span className="ml-auto flex gap-1"><Grade g={c.grade} /><Outcome c={c} /></span>
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
  const { data: c, error, reload } = useLoad(() => api<CallDetail>("GET", `/v1/calls/${id}`), [id]);
  const [grading, setGrading] = useState(false);
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
  async function grade() {
    setGrading(true);
    try { await api("POST", `/v1/calls/${id}/grade`); reload(); }
    catch (x) { toast.error((x as Error).message); }
    finally { setGrading(false); }
  }
  async function replay() {
    try {
      local.set("vpe-eval-draft", JSON.stringify(await api("POST", "/v1/evals/replay", { call_id: id })));
      go("#/evals");
    } catch (x) { toast.error((x as Error).message); }
  }
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
        <Stat label="Response p50 / p95" value={c.p50_ms != null ? `${secs(c.p50_ms)} / ${secs(c.p95_ms)}` : secs(c.response_ms)} />
        <Stat label="Cost (estimate)" value={money(c.cost) + (c.cost_detail?.missing.length ? "+" : "")} />
        <Stat label="Ended" value={ended(c)} />
      </dl>
      {c.cost_detail && (Object.keys(c.cost_detail.parts).length > 0 || c.cost_detail.missing.length > 0) && (
        <p className="-mt-2 text-xs text-muted-foreground">
          {Object.entries(c.cost_detail.parts).map(([k, v]) => `${k} ${money(v)}`).join(" · ")}
          {c.cost_detail.missing.length > 0 && ` · no rate for ${c.cost_detail.missing.join(", ")} (set it in Integrations → Call history)`}
        </p>
      )}
      <section className="grid gap-2" aria-label="Checks">
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-medium">Checks</h3>
          <Grade g={c.grade} />
          <Button size="sm" variant="outline" className="ml-auto" onClick={replay} disabled={!c.transcript.some((t) => t.who === "caller")}
            title="A Test Caller phones the agent and says what this caller said"><Repeat />Replay as a test</Button>
          <Button size="sm" variant="outline" onClick={grade} disabled={grading || !c.transcript.length}>
            <RefreshCw className={cn(grading && "animate-spin")} />{c.grade ? "Grade again" : "Grade now"}
          </Button>
        </div>
        {c.grade_results ? (
          <div className="grid gap-1.5 text-sm">
            {c.grade_results.summary && <p>{c.grade_results.summary}</p>}
            <ul className="grid gap-1">
              {c.grade_results.goal && <li className="flex gap-2"><Badge variant={c.grade_results.goal_met ? "ok" : "bad"}>{c.grade_results.goal_met ? "Met" : "Not met"}</Badge><span>{c.grade_results.goal}</span></li>}
              {c.grade_results.criteria.map((r, i) => (
                <li key={i} className="flex gap-2"><Badge variant={r.passed ? "ok" : "bad"}>{r.passed ? "Pass" : "Fail"}</Badge>
                  <span>{r.criterion} <span className="text-muted-foreground">— {r.reason}</span></span></li>
              ))}
            </ul>
          </div>
        ) : <p className="text-sm text-muted-foreground">Not graded. Add checks for this agent with <b>Call checks</b> to grade its calls as they end.</p>}
      </section>
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

function ChecksDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const agents = useLoad(() => api<{ agents: AgentSummary[] }>("GET", "/v1/agents"), [open]);
  const all = useLoad(() => api<{ checks: Checks[] }>("GET", "/v1/call-checks"), [open]);
  const list = (agents.data?.agents || []).filter((a) => !a.error);
  const [agent, setAgent] = useState(""), [goal, setGoal] = useState(""), [criteria, setCriteria] = useState(""), [enabled, setEnabled] = useState(true);
  const [saving, setSaving] = useState(false);
  useEffect(() => { if (!agent && list.length) setAgent(list[0].slug); }, [list, agent]);
  useEffect(() => {
    const found = all.data?.checks.find((x) => x.agent === agent);
    setGoal(found?.goal || ""); setCriteria((found?.criteria || []).join("\n")); setEnabled(found ? found.enabled : true);
  }, [agent, all.data]);
  async function save() {
    setSaving(true);
    try {
      await api("PUT", `/v1/call-checks/${agent}`, { goal, criteria: criteria.split("\n").map((x) => x.trim()).filter(Boolean), enabled });
      toast.success(enabled ? "Calls to this agent will be graded as they end" : "Grading is off for this agent");
      all.reload(); onOpenChange(false);
    } catch (x) { toast.error((x as Error).message); }
    finally { setSaving(false); }
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader><DialogTitle>Call checks</DialogTitle>
          <DialogDescription>When a call to this agent ends, your model reads the transcript and grades it against these checks. Failed calls show on this page. Each grade uses model tokens.</DialogDescription></DialogHeader>
        <div className="grid gap-4">
          <div className="grid gap-1.5"><Label htmlFor="ck-agent">Agent</Label>
            <NativeSelect id="ck-agent" value={agent} onChange={(e) => setAgent(e.target.value)}>
              {list.map((a) => <option key={a.slug} value={a.slug}>{a.name}{all.data?.checks.some((x) => x.agent === a.slug && x.enabled) ? " · grading on" : ""}</option>)}
            </NativeSelect></div>
          <div className="grid gap-1.5"><Label htmlFor="ck-goal">What a good call achieves</Label>
            <Input id="ck-goal" value={goal} onChange={(e) => setGoal(e.target.value)} placeholder="The caller's appointment is booked, or they're passed to a person" /></div>
          <div className="grid gap-1.5"><Label htmlFor="ck-crit">Checks, one per line</Label>
            <Textarea id="ck-crit" rows={5} value={criteria} onChange={(e) => setCriteria(e.target.value)}
              placeholder={"The agent asks for the caller's name\nThe agent confirms the time before ending\nThe agent doesn't promise a refund"} /></div>
          <label className="flex items-center gap-2 text-sm"><Switch checked={enabled} onCheckedChange={setEnabled} />Grade this agent's calls as they end</label>
        </div>
        <DialogFooter><Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={save} disabled={saving || !agent}><ClipboardCheck />Save</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
