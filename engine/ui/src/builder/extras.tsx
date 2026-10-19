import { useEffect, useState, type ReactNode } from "react";
import {
  Bell, BookOpen, Calendar, CircleAlert, Database, DatabaseZap, CircleCheck, Clock, Mail, MessageCircle, Play, Plug, RefreshCw, Send, Server, Trash2, Webhook, Zap,
} from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, NativeSelect } from "@/components/ui/input";
import { Field } from "@/components/ui/label";
import { Checkbox, Toggle } from "@/components/ui/switch";
import type { BuilderApi } from "./useBuilder";
import type { DefNode, Doc, McpTool, Send as SendT, Tool, Trigger } from "./model";
import { renameTool, text, TOOL_NAME } from "./model";
import { JsonField, PairsEditor, Section, TextField } from "./fields";

/* ------------------------------------------------------------------ catalog */

type P = { name: string; label: string; recipient?: boolean; area?: boolean; option?: boolean; type?: string };
export const CONNECTORS: Record<string, { label: string; service: string; icon: typeof Mail; blurb: string; params: P[]; mock: unknown }> = {
  "email.send": { label: "Send an email", service: "email", icon: Mail, blurb: "Email someone, like a booking confirmation.",
    params: [{ name: "to", label: "To", recipient: true }, { name: "subject", label: "Subject" }, { name: "body", label: "Message", area: true }], mock: { sent: true } },
  "whatsapp.send": { label: "Send a WhatsApp message", service: "whatsapp", icon: MessageCircle, blurb: "Message someone on WhatsApp.",
    params: [{ name: "to", label: "To (phone number)", recipient: true }, { name: "text", label: "Message", area: true }], mock: { sent: true } },
  "telegram.send": { label: "Send a Telegram message", service: "telegram", icon: Send, blurb: "Message a Telegram chat.",
    params: [{ name: "chat_id", label: "Chat id", recipient: true }, { name: "text", label: "Message", area: true }], mock: { sent: true } },
  "calendar.free_times": { label: "Find free times", service: "google", icon: Calendar, blurb: "Open times on a Google Calendar day.",
    params: [{ name: "day", label: "Day (YYYY-MM-DD)" }, { name: "duration_minutes", label: "Length (minutes)", type: "integer" },
      { name: "calendar_id", label: "Calendar", option: true }, { name: "hours", label: "Opening hours (09:00-17:00)", option: true }],
    mock: { day: "2026-10-20", open: true, times: ["9:00 AM", "10:30 AM", "2:00 PM"] } },
  "calendar.book": { label: "Book in Google Calendar", service: "google", icon: Calendar, blurb: "Book an appointment on the calendar.",
    params: [{ name: "start", label: "Start (YYYY-MM-DDTHH:MM)" }, { name: "summary", label: "Title" }, { name: "duration_minutes", label: "Length (minutes)", type: "integer" },
      { name: "attendee_email", label: "Invite (email)", recipient: true }, { name: "calendar_id", label: "Calendar", option: true }, { name: "description", label: "Description", option: true, area: true }],
    mock: { booked: true, start: "Tuesday October 20, 10:30 AM" } },
  "data.lookup": { label: "Look up a record", service: "data", icon: Database, blurb: "Find a record by what the caller gave, like a confirmation number.",
    params: [], mock: { found: 1, records: [{ id: "a1b2c3", status: "confirmed" }] } },
  "data.save": { label: "Save a record", service: "data", icon: DatabaseZap, blurb: "Save details from the conversation, like a callback request.",
    params: [], mock: { saved: true, id: "a1b2c3" } },
  "knowledge.search": { label: "Search knowledge", service: "knowledge", icon: BookOpen, blurb: "Search a knowledge base for facts to answer the caller.",
    params: [], mock: { results: [{ text: "We are open 8 to 6, Monday to Saturday.", source: "faq.md" }] } },
};
export const LOCAL_SERVICES = ["data", "knowledge"];
const SERVICE_NAME: Record<string, string> = { email: "Email", whatsapp: "WhatsApp", telegram: "Telegram", google: "Google" };

export function addTool(b: BuilderApi, kind: "http" | "mcp" | string = "http") {
  const doc = b.doc!;
  const names = new Set(doc.tools.map((t) => t.name));
  const base = kind === "http" ? "new_tool" : kind === "mcp" ? "mcp_server" : kind.replace(".", "_");
  let name = base, k = 2;
  while (names.has(name)) name = `${base}_${k++}`;
  const tool: Tool = kind === "http" ? { name, description: "", method: "GET", url: "", parameters: [], mock: { note: "an example answer, used while testing" } }
    : kind === "mcp" ? { name, description: "Tools from an MCP server.", mcp: { url: "", tools: [] }, mock: {} }
    : { name, connector: kind, description: CONNECTORS[kind].blurb, mock: CONNECTORS[kind].mock };
  b.update((d) => { d.tools.push(tool); });
  b.select({ kind: "tool", id: String(doc.tools.length) });
}

/* ------------------------------------------------------------------ connector tools */

export function Head({ icon: Icon, kind, title, onDelete }: { icon: typeof Mail; kind: string; title: ReactNode; onDelete: () => void }) {
  return (
    <div className="flex items-start gap-3 px-5 pb-3 pt-5">
      <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-[color-mix(in_srgb,var(--k-tool)_14%,transparent)] text-k-tool"><Icon className="size-[18px]" /></span>
      <div className="grid min-w-0 flex-1 gap-0.5"><span className="text-xs font-medium text-muted-foreground">{kind}</span><span className="truncate font-semibold">{title}</span></div>
      <Button variant="ghost" size="icon-sm" aria-label="Delete" onClick={onDelete}><Trash2 /></Button>
    </div>
  );
}
const Lead = ({ children }: { children: ReactNode }) => <p className="px-5 pb-4 text-[13px] leading-relaxed text-muted-foreground">{children}</p>;

export function NameField({ b, i }: { b: BuilderApi; i: number }) {
  const t = b.doc!.tools[i];
  return (
    <TextField label="Name" mono value={t.name} maxLength={64} transform={(v) => v.replace(/[^A-Za-z0-9_]/g, "_")}
      onChange={(v) => b.update((d) => {
        const x = d.tools[i];
        for (const n of d.definition.nodes) renameTool(n, x.name, v);
        x.name = v;
      }, { key: `t:${i}:name` })}
      hint={TOOL_NAME.test(t.name) ? "Steps list this name. Letters, digits and underscores." : <span className="text-bad">Start with a letter; use letters, digits and underscores.</span>} />
  );
}

export function TryBox({ tool, variables, fn, params }: { tool: Tool; variables: Record<string, string>; fn?: string; params: string[] }) {
  const [vals, setVals] = useState<Record<string, string>>({});
  const [out, setOut] = useState<null | { busy?: boolean; r?: any; err?: string }>(null);
  async function run(mock: boolean) {
    const args = Object.fromEntries(Object.entries(vals).filter(([, v]) => v.trim()).map(([k, v]) => [k, /^\d+$/.test(v) ? Number(v) : v]));
    setOut({ busy: true });
    try { setOut({ r: await api("POST", "/v1/tools/try", { tool, function: fn, arguments: args, variables, mock }) }); }
    catch (e) { setOut({ err: (e as Error).message }); }
  }
  const r = out?.r;
  return (
    <Section title="Try it">
      {params.map((p) => (
        <label key={p} className="grid grid-cols-[110px_minmax(0,1fr)] items-center gap-2 text-[12.5px]">
          <span className="truncate font-mono">{p}</span>
          <Input className="h-8" value={vals[p] || ""} onChange={(e) => setVals({ ...vals, [p]: e.target.value })} aria-label={`Value for ${p}`} />
        </label>
      ))}
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" size="sm" onClick={() => run(true)} disabled={out?.busy}><Play />Try the mock</Button>
        <Button variant="outline" size="sm" onClick={() => run(false)} disabled={out?.busy} title="Uses the real connection"><Play />Run it for real</Button>
      </div>
      {out && (
        <div className="grid gap-2 rounded-lg border bg-muted/50 p-3 text-[12.5px]" role="status">
          {out.busy && <span className="text-muted-foreground">Running…</span>}
          {out.err && <span className="text-bad">{out.err}</span>}
          {r && (<>
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge variant={r.ok ? "ok" : "bad"}>{r.ok ? <CircleCheck /> : <CircleAlert />}{r.ok ? "OK" : "Failed"}</Badge>
              <Badge variant="outline">{r.mock ? "Mock" : "Live"}</Badge>
              {r.request && <code className="truncate text-muted-foreground">{r.request}</code>}
            </div>
            {r.error && <p className="text-bad">{r.error}</p>}
            {(r.data ?? r.data_truncated) != null && <pre className="max-h-56 overflow-auto rounded-md bg-card p-2.5 font-mono text-xs whitespace-pre-wrap break-all">{JSON.stringify(r.data ?? r.data_truncated, null, 2)}</pre>}
          </>)}
        </div>
      )}
    </Section>
  );
}

export function ConnectorToolPanel({ b, i, onDelete, status }: { b: BuilderApi; i: number; onDelete: () => void; status: Record<string, boolean> }) {
  const doc = b.doc!;
  const t = doc.tools[i];
  const c = CONNECTORS[t.connector || ""];
  const set = (fn: (x: Tool) => void, key?: string) => b.update((d) => { fn(d.tools[i]); }, { key: key && `t:${i}:${key}` });
  const vars = Object.keys(doc.context_variables).concat(doc.definition.nodes.flatMap((n) => (n.data.extraction_variables || []).map((v) => v.name)));
  if (!c) return <><Head icon={Plug} kind="Connector" title={t.name} onDelete={onDelete} /><Lead>Unknown connector {t.connector}.</Lead></>;
  const connected = status[c.service];
  const pinned = t.with || {};
  return (
    <>
      <Head icon={c.icon} kind={c.label} title={<span className="font-mono">{t.name || "(no name)"}</span>} onDelete={onDelete} />
      <Lead>{c.blurb} The connection is set up once in Integrations; nothing about it is stored in this agent.</Lead>
      <div className="grid gap-4 px-5 pb-5">
        <div className="flex flex-wrap items-center gap-2 text-[13px]">
          <Badge variant={connected ? "ok" : "warn"}>{connected ? `${SERVICE_NAME[c.service]} connected` : `${SERVICE_NAME[c.service]} not set up`}</Badge>
          {!connected && <a className="underline" href="#/integrations">Set it up in Integrations</a>}
        </div>
        <NameField b={b} i={i} />
        <Field label="Action" htmlFor={`ca-${i}`}>
          <NativeSelect id={`ca-${i}`} value={t.connector} onChange={(e) => set((x) => { x.connector = e.target.value; x.with = {}; x.mock = CONNECTORS[e.target.value].mock; })}>
            {Object.entries(CONNECTORS).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
          </NativeSelect>
        </Field>
        <TextField label="When to use it" area rows={3} value={text(t.description)} onChange={(v) => set((x) => { x.description = v; }, "d")}
          hint="The model reads this to decide when to call it, like “Once the caller confirms, email them the details.”" />
      </div>
      <Section title="Values">
        <p className="text-[12.5px] text-muted-foreground">Fill in a value to fix it (the model can't change it), or leave it empty for the model to fill from the conversation. Recipients are never chosen by the model: set them here, or leave them empty to reply to whoever wrote in on WhatsApp, Telegram or email.</p>
        {c.params.map((p) => (
          <TextField key={p.name} label={<>{p.label}{p.recipient && <Badge variant="outline" className="ml-2">recipient</Badge>}{p.option && <Badge variant="outline" className="ml-2">optional setting</Badge>}</>}
            area={p.area} rows={3} value={pinned[p.name] || ""} insert={vars} mono={!p.area}
            placeholder={p.recipient ? "e.g. {{caller_email}} or {{caller_phone}}" : p.option ? "default from Integrations" : "filled in by the model"}
            onChange={(v) => set((x) => { const w = { ...(x.with || {}) }; if (v.trim()) w[p.name] = v; else delete w[p.name]; if (Object.keys(w).length) x.with = w; else delete x.with; }, `w-${p.name}`)} />
        ))}
      </Section>
      <Section title="While it runs">
        <TextField label="Say while running (optional)" value={text(t.say_while_running)} placeholder="e.g. One moment while I book that."
          onChange={(v) => set((x) => { if (v.trim()) x.say_while_running = v; else delete x.say_while_running; }, "speech")} />
      </Section>
      <Section title="Mock answer">
        <JsonField key={`m${i}-${b.rev}`} label="Example answer" value={t.mock} onChange={(v) => set((x) => { if (v === undefined) delete x.mock; else x.mock = v; }, "mock")}
          hint="Used in test chats with “Mock tool answers” on, so testing never sends real messages." />
      </Section>
      <TryBox tool={t} variables={doc.context_variables} params={c.params.filter((p) => !p.option && !p.recipient && !pinned[p.name]).map((p) => p.name)} />
    </>
  );
}

/* ------------------------------------------------------------------ MCP servers */

export function McpToolPanel({ b, i, secrets, onDelete }: { b: BuilderApi; i: number; secrets: string[]; onDelete: () => void }) {
  const doc = b.doc!;
  const t = doc.tools[i];
  const server = t.mcp || {};
  const listed: McpTool[] = server.tools || [];
  const set = (fn: (x: Tool) => void, key?: string) => b.update((d) => { const x = d.tools[i]; x.mcp ||= {}; fn(x); }, { key: key && `t:${i}:${key}` });
  const [busy, setBusy] = useState(false);
  const [pick, setPick] = useState(listed[0]?.name || "");
  async function refresh() {
    setBusy(true);
    try {
      const r = await api<{ tools: McpTool[] }>("POST", "/v1/tools/mcp/list", { mcp: { url: server.url, headers: server.headers } });
      set((x) => { x.mcp!.tools = r.tools; });
      toast.success(`${r.tools.length} tool${r.tools.length === 1 ? "" : "s"} from the server`);
      if (r.tools[0]) setPick(r.tools[0].name);
    } catch (e) { toast.error("Couldn't list the server's tools", { description: (e as Error).message }); } finally { setBusy(false); }
  }
  const mocks = (t.mock && typeof t.mock === "object" ? t.mock : {}) as Record<string, unknown>;
  const chosen = listed.find((x) => x.name === pick) || listed[0];
  return (
    <>
      <Head icon={Server} kind="MCP server" title={<span className="font-mono">{t.name || "(no name)"}</span>} onDelete={onDelete} />
      <Lead>A server that offers tools over the Model Context Protocol (Streamable HTTP). Steps that tick it can use every tool listed below. Programs on this computer (stdio servers) aren't started from agents.</Lead>
      <div className="grid gap-4 px-5 pb-5">
        <NameField b={b} i={i} />
        <TextField label="What it's for" area rows={2} value={text(t.description)} onChange={(v) => b.update((d) => { d.tools[i].description = v; }, { key: `t:${i}:d` })} />
        <TextField label="Server URL" mono value={text(server.url)} placeholder="https://mcp.example.com/mcp" insert={secrets} insertPrefix="secrets." insertLabel="Secrets"
          onChange={(v) => set((x) => { x.mcp!.url = v.trim(); }, "url")} hint="The address can only use {{secrets.NAME}}." />
      </div>
      <Section title="Headers">
        <p className="text-[12.5px] text-muted-foreground">For a key, write Authorization: Bearer {"{{secrets.NAME}}"} and save the value in Integrations → Tool secrets.</p>
        <PairsEditor key={`mh${i}-${b.rev}`} value={server.headers} keyPh="Header" valPh="Value" valMono
          onChange={(v) => set((x) => { if (Object.keys(v).length) x.mcp!.headers = v; else delete x.mcp!.headers; }, "headers")} />
      </Section>
      <Section title={`Tools (${listed.length})`} aside={<Button variant="outline" size="sm" onClick={refresh} disabled={busy || !server.url}><RefreshCw className={busy ? "animate-spin" : ""} />{busy ? "Loading…" : "Refresh"}</Button>}>
        {listed.length === 0 ? <p className="text-[12.5px] text-muted-foreground">Press Refresh to load the server's tools. They're saved in the agent so tests and reviews see exactly what it can call.</p>
          : listed.map((x, j) => (
            <div key={x.name} className="flex items-start gap-2 rounded-lg border p-2.5">
              <div className="grid min-w-0 flex-1 gap-0.5">
                <span className="font-mono text-[12.5px]">{x.name}</span>
                {x.description && <span className="line-clamp-2 text-[12px] text-muted-foreground">{x.description}</span>}
                <span className="text-[11.5px] text-muted-foreground">{Object.keys(x.input_schema?.properties || {}).join(", ") || "no arguments"}</span>
              </div>
              <Button variant="ghost" size="icon-sm" aria-label={`Don't offer ${x.name}`} title="Don't offer this tool" onClick={() => set((y) => { y.mcp!.tools = listed.filter((_, k) => k !== j); })}><Trash2 /></Button>
            </div>
          ))}
      </Section>
      {listed.length > 0 && (
        <Section title="Mock answers">
          <Field label="Tool" htmlFor={`mp-${i}`}>
            <NativeSelect id={`mp-${i}`} value={chosen?.name} onChange={(e) => setPick(e.target.value)}>{listed.map((x) => <option key={x.name}>{x.name}</option>)}</NativeSelect>
          </Field>
          {chosen && <JsonField key={`mm${i}-${chosen.name}-${b.rev}`} label="Example answer" value={mocks[chosen.name]}
            onChange={(v) => b.update((d) => { const m = { ...((d.tools[i].mock as Record<string, unknown>) || {}) }; if (v === undefined) delete m[chosen.name]; else m[chosen.name] = v; d.tools[i].mock = m; }, { key: `t:${i}:mock:${chosen.name}` })}
            hint="Used in test chats with “Mock tool answers” on." />}
        </Section>
      )}
      {chosen && <TryBox tool={t} fn={chosen.name} variables={doc.context_variables} params={Object.keys(chosen.input_schema?.properties || {})} />}
    </>
  );
}

/* ------------------------------------------------------------------ triggers */

export const TRIGGER_KINDS = [
  { key: "message:telegram", icon: Send, label: "Telegram messages", sub: "People chat with this agent on Telegram" },
  { key: "message:whatsapp", icon: MessageCircle, label: "WhatsApp messages", sub: "People chat with this agent on WhatsApp" },
  { key: "message:email", icon: Mail, label: "Emails", sub: "Answer emails to your address" },
  { key: "after_call", icon: Bell, label: "After a conversation", sub: "Send a summary by email, message or webhook" },
  { key: "schedule", icon: Clock, label: "On a schedule", sub: "Call or message a list at set times" },
  { key: "api", icon: Zap, label: "API trigger", sub: "Another system starts a call or message" },
  { key: "webhook", icon: Webhook, label: "Webhook after the call", sub: "Post the results to your system" },
] as const;

export function addTrigger(b: BuilderApi, key: string) {
  const doc = b.doc!;
  if (key === "api" || key === "webhook") {
    if (key === "api" && doc.definition.nodes.some((n) => String(n.type) === "trigger")) { toast("This agent already has an API trigger"); return; }
    const id = key === "api" ? "api-trigger" : `webhook-${doc.definition.nodes.filter((n) => String(n.type) === "webhook").length + 1}`;
    const data = key === "api" ? { name: "API Trigger", enabled: true, trigger_path: crypto.randomUUID().slice(0, 8) }
      : { name: "Notify CRM", enabled: true, http_method: "POST", endpoint_url: "", custom_headers: [],
          payload_template: { call_id: "{{workflow_run_id}}", channel: "{{channel}}", summary: "{{summary}}", details: "{{gathered_context}}" } };
    b.update((d) => { d.definition.nodes.push({ id, type: key === "api" ? "trigger" : "webhook", position: { x: -600, y: 0 }, data } as unknown as DefNode); });
    b.select({ kind: "hook", id });
    return;
  }
  const ids = new Set(doc.triggers.map((t) => t.id));
  const base = key.replace(":", "-").replace("_", "-");
  let id = base, k = 2;
  while (ids.has(id)) id = `${base}-${k++}`;
  const t: Trigger = key.startsWith("message:") ? { id, type: "message", channel: key.split(":")[1], enabled: true }
    : key === "after_call" ? { id, type: "after_call", when: "reached_end", enabled: true,
        send: { via: "email", to: "", subject: "New request from {{caller_name | a caller}}", text: "{{summary}}\n\n{{details}}" } }
    : { id, type: "schedule", cron: "0 17 * * 1-5", action: "message", channel: "whatsapp", enabled: false, recipients: [],
        text: "Hi {{caller_name | there}}, a reminder about your appointment tomorrow. Reply here if you need to change it." };
  b.update((d) => { d.triggers.push(t); });
  b.select({ kind: "trigger", id: String(doc.triggers.length) });
}

export function triggerLabel(t: Trigger) {
  if (t.type === "message") return `${SERVICE_NAME[t.channel || ""] || t.channel} messages`;
  if (t.type === "after_call") return `After a conversation → ${t.send?.via || "?"}`;
  return `Schedule · ${describeCron(t.cron || "")}`;
}

const DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
export function describeCron(c: string) {
  const p = c.trim().split(/\s+/);
  if (p.length !== 5) return "invalid";
  const [m, h, dom, , dow] = p;
  const at = /^\d+$/.test(m) && /^\d+$/.test(h) ? new Date(2000, 0, 1, +h, +m).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "several times";
  const days = dow === "*" && dom === "*" ? "every day" : dow === "1-5" ? "weekdays" : dom === "*" ? dow.split(",").map((d) => DAYS[+d % 7] || d).join(", ") : `day ${dom}`;
  return `${days} at ${at}`;
}
const PRESETS: [string, string][] = [["0 17 * * 1-5", "Weekdays at 5 PM"], ["0 9 * * *", "Every day at 9 AM"], ["0 10 * * 1", "Mondays at 10 AM"], ["0 * * * *", "Every hour"]];
const TEMPLATE_VARS = ["summary", "details", "transcript", "contact", "channel", "agent", "caller_name", "caller_phone", "caller_email"];

export function TriggerPanel({ b, i, slug, status, onDelete }: { b: BuilderApi; i: number; slug: string; status: Record<string, boolean>; onDelete: () => void }) {
  const doc = b.doc!;
  const t = doc.triggers[i];
  const set = (fn: (x: Trigger) => void, key?: string) => b.update((d) => { fn(d.triggers[i]); }, { key: key && `g:${i}:${key}` });
  const send = t.send || {};
  const setSend = (fn: (s: SendT) => void, key?: string) => set((x) => { x.send ||= {}; fn(x.send); }, key);
  const vars = [...TEMPLATE_VARS, ...Object.keys(doc.context_variables), ...doc.definition.nodes.flatMap((n) => (n.data.extraction_variables || []).map((v) => v.name))];
  const kind = TRIGGER_KINDS.find((k) => k.key === (t.type === "message" ? `message:${t.channel}` : t.type)) || TRIGGER_KINDS[3];
  const needs = t.type === "message" ? t.channel : t.type === "after_call" ? send.via : t.action === "message" ? t.channel : null;
  return (
    <>
      <Head icon={kind.icon} kind="Trigger" title={triggerLabel(t)} onDelete={onDelete} />
      <Lead>{t.type === "message" ? "Each person who writes in gets their own conversation with this agent (it ends after 30 minutes without a reply)."
        : t.type === "after_call" ? "Runs when a phone call or message conversation with this agent ends (not browser tests, unless you tick them)."
        : "Runs on its own while the engine is running. Outbound calls and messages also need “Outbound” turned on in Integrations → Automation."}</Lead>
      <div className="grid gap-4 px-5 pb-5">
        <Toggle id={`ten-${i}`} label="On" checked={t.enabled !== false} onCheckedChange={(v) => set((x) => { x.enabled = v; })}
          hint={t.type === "schedule" ? "Off by default: try a dry run first." : undefined} />
        {needs && needs !== "webhook" && needs !== "call" && (
          <div className="flex flex-wrap items-center gap-2 text-[13px]">
            <Badge variant={status[needs] ? "ok" : "warn"}>{status[needs] ? `${SERVICE_NAME[needs] || needs} connected` : `${SERVICE_NAME[needs] || needs} not set up`}</Badge>
            {!status[needs] && <a className="underline" href="#/integrations">Set it up</a>}
          </div>
        )}
        <TextField label="Id" mono value={t.id} maxLength={40} transform={(v) => v.toLowerCase().replace(/[^a-z0-9-]/g, "-")} onChange={(v) => set((x) => { x.id = v; }, "id")} />

        {t.type === "message" && (
          <Field label="Channel" htmlFor={`tch-${i}`}>
            <NativeSelect id={`tch-${i}`} value={t.channel} onChange={(e) => set((x) => { x.channel = e.target.value; })}>
              <option value="telegram">Telegram</option><option value="whatsapp">WhatsApp</option><option value="email">Email</option>
            </NativeSelect>
          </Field>
        )}

        {t.type === "after_call" && (<>
          <Field label="When" htmlFor={`tw-${i}`}>
            <NativeSelect id={`tw-${i}`} value={(t.when || "always").startsWith("collected:") ? "collected" : t.when || "always"}
              onChange={(e) => set((x) => { x.when = e.target.value === "collected" ? `collected:${vars.find((v) => !TEMPLATE_VARS.includes(v)) || "caller_name"}` : e.target.value; })}>
              <option value="always">After every conversation</option>
              <option value="reached_end">Only when it reached an End call step</option>
              <option value="collected">Only when a detail was collected</option>
            </NativeSelect>
          </Field>
          {(t.when || "").startsWith("collected:") && (
            <TextField label="Detail" mono value={(t.when || "").slice(10)} onChange={(v) => set((x) => { x.when = `collected:${v.replace(/\W/g, "_")}`; }, "wv")} />
          )}
          <Field label="Conversations on">
            <div className="flex flex-wrap gap-3">
              {["phone", "telegram", "whatsapp", "email", "web"].map((ch) => {
                const on = (t.channels || ["phone", "telegram", "whatsapp", "email"]).includes(ch);
                return (
                  <label key={ch} className="flex items-center gap-1.5 text-[13px]">
                    <Checkbox checked={on} onCheckedChange={(v) => set((x) => {
                      const cur = new Set(x.channels || ["phone", "telegram", "whatsapp", "email"]);
                      if (v) cur.add(ch); else cur.delete(ch);
                      x.channels = [...cur];
                    })} />{ch === "web" ? "browser tests" : SERVICE_NAME[ch] || "Phone"}
                  </label>
                );
              })}
            </div>
          </Field>
          <Field label="Send with" htmlFor={`tv-${i}`}>
            <NativeSelect id={`tv-${i}`} value={send.via || "email"} onChange={(e) => setSend((s) => { s.via = e.target.value; })}>
              <option value="email">Email</option><option value="whatsapp">WhatsApp</option><option value="telegram">Telegram</option><option value="webhook">Webhook (HTTP POST)</option>
            </NativeSelect>
          </Field>
          {send.via === "webhook" ? (<>
            <TextField label="URL" mono value={text(send.url)} placeholder="https://crm.example.com/calls" onChange={(v) => setSend((s) => { s.url = v.trim(); }, "url")} />
            <PairsEditor key={`th${i}-${b.rev}`} value={send.headers} keyPh="Header" valPh="Value ({{secrets.NAME}} for keys)" valMono
              onChange={(v) => setSend((s) => { if (Object.keys(v).length) s.headers = v; else delete s.headers; }, "headers")} />
            <JsonField key={`tp${i}-${b.rev}`} label="JSON body (optional)" value={send.payload} onChange={(v) => setSend((s) => { if (v === undefined) delete s.payload; else s.payload = v; }, "payload")}
              hint="Empty: agent, session id, channel, summary and every collected value." />
          </>) : (<>
            <TextField label="To" mono value={text(send.to)} insert={vars} placeholder={send.via === "email" ? "desk@example.com" : send.via === "whatsapp" ? "+15551234567 or {{caller_phone}}" : "chat id"}
              onChange={(v) => setSend((s) => { s.to = v; }, "to")} hint="Your team's address, or the person's own (like {{caller_email}}) to send them a recap." />
            {send.via === "email" && <TextField label="Subject" value={text(send.subject)} insert={vars} onChange={(v) => setSend((s) => { s.subject = v; }, "subject")} />}
            <TextField label="Message" area rows={5} value={text(send.text)} insert={vars} onChange={(v) => setSend((s) => { s.text = v; }, "text")}
              hint="{{summary}} is a short summary written by your model; {{details}} lists what was collected." />
          </>)}
        </>)}

        {t.type === "schedule" && (<>
          <Field label="When" htmlFor={`tc-${i}`} hint={<>Runs {describeCron(t.cron || "")} in your time zone (Integrations → Automation). Five fields: minute hour day month weekday.</>}>
            <div className="grid gap-2">
              <Input id={`tc-${i}`} className="font-mono" value={t.cron || ""} onChange={(e) => set((x) => { x.cron = e.target.value; }, "cron")} />
              <div className="flex flex-wrap gap-1">{PRESETS.map(([c, l]) => <button key={c} type="button" onClick={() => set((x) => { x.cron = c; })}
                className="cursor-pointer rounded-full border px-2 py-0.5 text-[12px] hover:border-ring/60 hover:bg-accent">{l}</button>)}</div>
            </div>
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Do" htmlFor={`ta-${i}`}>
              <NativeSelect id={`ta-${i}`} value={t.action || "message"} onChange={(e) => set((x) => { x.action = e.target.value as "call" | "message"; })}>
                <option value="message">Send a message</option><option value="call">Place a call</option>
              </NativeSelect>
            </Field>
            {t.action !== "call" && (
              <Field label="On" htmlFor={`tsc-${i}`}>
                <NativeSelect id={`tsc-${i}`} value={t.channel || "whatsapp"} onChange={(e) => set((x) => { x.channel = e.target.value; })}>
                  <option value="whatsapp">WhatsApp</option><option value="telegram">Telegram</option><option value="email">Email</option>
                </NativeSelect>
              </Field>
            )}
          </div>
          {t.action !== "call" && <TextField label="First message" area rows={3} value={text(t.text)} insert={vars} onChange={(v) => set((x) => { x.text = v; }, "text")}
            hint="Empty: the agent's greeting. Replies continue the conversation with this agent." />}
          <RecipientsField t={t} onChange={(r) => set((x) => { x.recipients = r; }, "recipients")} />
          <TextField label="Or fetch the list from (optional)" mono value={text(t.recipients_from?.url)} placeholder="https://scheduling.example.com/reminders/tomorrow"
            onChange={(v) => set((x) => { if (v.trim()) x.recipients_from = { ...(x.recipients_from || {}), url: v.trim() }; else delete x.recipients_from; }, "rf")}
            hint={<>A JSON array of {"{to, context}"}. Headers can use {"{{secrets.NAME}}"}.</>} />
          {t.recipients_from?.url && <PairsEditor key={`rf${i}-${b.rev}`} value={t.recipients_from.headers} keyPh="Header" valPh="Value" valMono
            onChange={(v) => set((x) => { x.recipients_from!.headers = Object.keys(v).length ? v : undefined; }, "rfh")} />}
          <p className="text-[12.5px] text-muted-foreground">Use made-up numbers and addresses while testing.</p>
        </>)}
      </div>
      {t.type !== "message" && <RunBox slug={slug} id={t.id} dirty={b.dirty} />}
    </>
  );
}

function RecipientsField({ t, onChange }: { t: Trigger; onChange: (r: { to: string; context?: Record<string, string> }[]) => void }) {
  const toText = (r: Trigger["recipients"]) => (r || []).map((x) => [x.to, x.context?.caller_name].filter(Boolean).join(", ")).join("\n");
  const [value, setValue] = useState(toText(t.recipients));
  useEffect(() => { setValue(toText(t.recipients)); }, [t.id]); // eslint-disable-line
  return (
    <Field label="Recipients" htmlFor={`rcp-${t.id}`} hint="One per line: number, email or chat id, then optionally a comma and their name ({{caller_name}}).">
      <textarea id={`rcp-${t.id}`} rows={4} className="w-full rounded-md border bg-transparent px-3 py-2 font-mono text-[13px] shadow-xs outline-none focus-visible:ring-2 focus-visible:ring-ring"
        value={value} placeholder={"+15550100001, Sam\n+15550100002, Lee"}
        onChange={(e) => {
          setValue(e.target.value);
          onChange(e.target.value.split("\n").map((l) => l.trim()).filter(Boolean).map((l) => {
            const [to, ...rest] = l.split(",");
            const name = rest.join(",").trim();
            return name ? { to: to.trim(), context: { caller_name: name } } : { to: to.trim() };
          }));
        }} />
    </Field>
  );
}

function RunBox({ slug, id, dirty }: { slug: string; id: string; dirty: boolean }) {
  const [out, setOut] = useState<null | { busy?: boolean; results?: any[]; err?: string }>(null);
  async function run(dry: boolean) {
    if (!dry && !confirm("Run it for real? This sends real messages or places real calls.")) return;
    setOut({ busy: true });
    try { setOut({ results: (await api<{ results: any[] }>("POST", `/v1/agents/${encodeURIComponent(slug)}/triggers/${encodeURIComponent(id)}/run`, { dry_run: dry })).results }); }
    catch (e) { setOut({ err: (e as Error).message }); }
  }
  return (
    <Section title="Try it">
      {dirty && <p className="text-[12.5px] text-warn">Save first: runs use the saved agent.</p>}
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" size="sm" onClick={() => run(true)} disabled={out?.busy || dirty}><Play />Dry run</Button>
        <Button variant="outline" size="sm" onClick={() => run(false)} disabled={out?.busy || dirty}><Zap />Run now</Button>
      </div>
      {out && (
        <div className="grid gap-1.5 rounded-lg border bg-muted/50 p-3 text-[12.5px]" role="status">
          {out.busy && <span className="text-muted-foreground">Running…</span>}
          {out.err && <span className="text-bad">{out.err}</span>}
          {out.results?.map((r, k) => (
            <div key={k} className="flex items-start gap-2">
              {r.ok ? <CircleCheck className="mt-0.5 size-3.5 shrink-0 text-ok" /> : <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-bad" />}
              <span><span className="font-medium">{r.what}</span>{r.to && <span className="text-muted-foreground"> to {r.to}</span>}{r.detail && <span className="text-muted-foreground"> · {r.detail}</span>}</span>
            </div>
          ))}
        </div>
      )}
    </Section>
  );
}

/* ------------------------------------------------------------------ API Trigger and Webhook nodes */

export function HookPanel({ b, id, onDelete }: { b: BuilderApi; id: string; onDelete: () => void }) {
  const doc = b.doc!;
  const n = doc.definition.nodes.find((x) => x.id === id)!;
  const d = n.data as Record<string, any>;
  const set = (fn: (x: Record<string, any>) => void, key?: string) => b.update((dd) => { const x = dd.definition.nodes.find((y) => y.id === id); if (x) fn(x.data as Record<string, any>); }, { key: key && `h:${id}:${key}` });
  if (String(n.type) === "trigger") {
    const path = text(d.trigger_path);
    const url = `${location.origin}/v1/public/agent/${path}`;
    return (
      <>
        <Head icon={Zap} kind="API trigger" title={text(d.name) || "API Trigger"} onDelete={onDelete} />
        <Lead>Lets another system start a call (or a message) with this agent.</Lead>
        <div className="grid gap-4 px-5 pb-5">
          <Toggle id={`hen-${id}`} label="On" checked={d.enabled !== false} onCheckedChange={(v) => set((x) => { x.enabled = v; })} hint="Off: the address answers 404." />
          <TextField label="Path" mono value={path} maxLength={36} transform={(v) => v.replace(/[^A-Za-z0-9_-]/g, "")} onChange={(v) => set((x) => { x.trigger_path = v; }, "path")} />
          <div className="grid gap-1.5 text-[12.5px]">
            <span className="font-medium">Call it like this</span>
            <pre className="overflow-x-auto rounded-md border bg-muted/50 p-2.5 font-mono text-[11.5px] leading-relaxed">{`curl -X POST ${url} \\
  -H "X-API-Key: $VPE_API_KEY" -H "Content-Type: application/json" \\
  -d '{"phone_number": "+15550100000",
       "initial_context": {"caller_name": "Sam"}}'

# or a message:
  -d '{"channel": "whatsapp", "to": "+15550100000",
       "text": "Your car is ready."}'`}</pre>
            <span className="text-muted-foreground">Add "dry_run": true to check without sending. From other computers it needs VPE_API_KEY; outbound must be on in Integrations → Automation.</span>
          </div>
        </div>
      </>
    );
  }
  const headers = Object.fromEntries((Array.isArray(d.custom_headers) ? d.custom_headers : []).map((h: { key: string; value: string }) => [h.key, h.value]));
  return (
    <>
      <Head icon={Webhook} kind="Webhook after the call" title={text(d.name) || "Webhook"} onDelete={onDelete} />
      <Lead>Posts the results to your system after each real conversation.</Lead>
      <div className="grid gap-4 px-5 pb-5">
        <Toggle id={`hen-${id}`} label="On" checked={d.enabled !== false} onCheckedChange={(v) => set((x) => { x.enabled = v; })} />
        <TextField label="Name" value={text(d.name)} onChange={(v) => set((x) => { x.name = v; }, "name")} />
        <div className="grid grid-cols-[110px_minmax(0,1fr)] gap-3">
          <Field label="Method" htmlFor={`hm-${id}`}>
            <NativeSelect id={`hm-${id}`} value={text(d.http_method) || "POST"} onChange={(e) => set((x) => { x.http_method = e.target.value; })}>{["POST", "PUT", "PATCH"].map((m) => <option key={m}>{m}</option>)}</NativeSelect>
          </Field>
          <TextField label="URL" mono value={text(d.endpoint_url)} placeholder="https://crm.example.com/calls" onChange={(v) => set((x) => { x.endpoint_url = v.trim(); }, "url")} />
        </div>
      </div>
      <Section title="Headers">
        <PairsEditor key={`hh${id}-${b.rev}`} value={headers} keyPh="Header" valPh="Value ({{secrets.NAME}} for keys)" valMono
          onChange={(v) => set((x) => { x.custom_headers = Object.entries(v).map(([key, value]) => ({ key, value })); }, "headers")} />
      </Section>
      <Section title="Body">
        <JsonField key={`hp${id}-${b.rev}`} label="Payload template" value={d.payload_template} onChange={(v) => set((x) => { x.payload_template = v; }, "payload")}
          hint="{{workflow_run_id}}, {{gathered_context.name}}, {{summary}}, {{channel}} and every collected value work here." />
      </Section>
    </>
  );
}

export function deleteExtra(b: BuilderApi, sel: { kind: string; id: string }) {
  if (sel.kind === "trigger") b.update((d) => { d.triggers.splice(+sel.id, 1); });
  else if (sel.kind === "hook") b.update((d) => { d.definition.nodes = d.definition.nodes.filter((n) => n.id !== sel.id); });
  b.select(null);
}

export type { Doc };
