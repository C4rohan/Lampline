import { useEffect, useState, type ReactNode } from "react";
import { ArrowRight, Bot, CircleStop, Globe, Play, Plug, Plus, Trash2, Zap, GitBranch, CircleAlert, CircleCheck } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, NativeSelect } from "@/components/ui/input";
import { Field } from "@/components/ui/label";
import { Checkbox, Toggle } from "@/components/ui/switch";
import type { BuilderApi } from "./useBuilder";
import type { DefEdge, DefNode, Doc, NodeType, Param, Tool } from "./model";
import { findingsFor, kindLabel, renameTool, rootId, text, TOOL_NAME, toolName, TRANSFER_TO, TYPES, usesTool, VAR_NAME } from "./model";
import { InsertChips, JsonField, PairsEditor, Section, TextField } from "./fields";
import { allVariables, FlowField, isScripted, ModeSwitch, RouteKind, routeKind, RULE_LABEL, ScriptEditor } from "./script";

const ICONS: Record<NodeType, typeof Bot> = { startCall: Play, agentNode: Bot, endCall: CircleStop, globalNode: Globe };

function Head({ color, icon: Icon, kind, title, onDelete, deleteLabel }: { color: string; icon: typeof Bot; kind: string; title?: ReactNode; onDelete?: () => void; deleteLabel?: string }) {
  return (
    <div className="flex items-center gap-2.5 px-5 pb-1 pt-5" style={{ ["--k" as string]: color }}>
      <span className="grid size-7 place-items-center rounded-md bg-[var(--k)] text-white dark:text-[#10171e]"><Icon className="size-4" /></span>
      <div className="grid min-w-0">
        <span className="text-xs font-semibold text-[var(--k)]">{kind}</span>
        {title && <span className="truncate text-[15px] font-semibold leading-tight">{title}</span>}
      </div>
      {onDelete && <Button variant="ghost" size="icon-sm" className="ml-auto text-muted-foreground hover:text-bad" onClick={onDelete} aria-label={deleteLabel} title={`${deleteLabel} (Delete key)`}><Trash2 /></Button>}
    </div>
  );
}
const Lead = ({ children }: { children: ReactNode }) => <p className="px-5 pb-4 text-[13px] leading-relaxed text-muted-foreground">{children}</p>;

function LinkRow({ main, sub, onClick, color }: { main: ReactNode; sub?: ReactNode; onClick: () => void; color?: string }) {
  return (
    <button type="button" onClick={onClick} style={color ? { ["--k" as string]: color } : undefined}
      className="flex w-full min-w-0 cursor-pointer items-center gap-2 rounded-lg border bg-card px-3 py-2 text-left text-[13px] hover:bg-accent">
      {color && <span className="size-2 shrink-0 rounded-full bg-[var(--k)]" />}
      <span className="truncate font-medium">{main}</span>
      {sub && <span className="ml-auto truncate pl-2 text-muted-foreground">{sub}</span>}
    </button>
  );
}

/* ---------------------------------------------------------------- transfer to a person */

function TransferSection({ b, n }: { b: BuilderApi; n: DefNode }) {
  const doc = b.doc!;
  const d = n.data;
  const set = (fn: (x: DefNode) => void, key?: string) => b.update((dd) => { const x = dd.definition.nodes.find((y) => y.id === n.id); if (x) fn(x); }, { key: key && `n:${n.id}:${key}` });
  const to = text(d.transfer_to).trim();
  const consts = new Set(Object.keys(doc.context_variables));
  const names = ["caller_phone", ...allVariables(doc).filter((v) => !consts.has(v) && v !== "caller_phone")];
  const picked = Array.isArray(d.transfer_details) ? d.transfer_details : [];
  const sip = /^sips?:/.test(to);
  return (
    <Section title="Transfer to a person">
      <TextField label="Transfer the call to" value={text(d.transfer_to)} placeholder="+15551234567 or sip:desk@pbx.example.com" insert={Object.keys(doc.context_variables)}
        invalid={!!to && !TRANSFER_TO.test(to)}
        onChange={(v) => set((x) => { if (v.trim()) x.data.transfer_to = v.trim(); else { delete x.data.transfer_to; delete x.data.transfer_details; delete x.data.transfer_uui; } }, "tto")}
        hint={!to ? "Empty: the call ends here. A phone number or SIP address: after this step speaks, the caller is connected there instead."
          : sip ? "The details below go along as SIP headers (X-Lampline-…), with a session ID for the full record."
          : "The phone network can't carry details, so they stay in this call's record and any after-call trigger. Use a SIP address to send them along."} />
      {to && (
        <>
          <div className="grid gap-2">
            <span className="text-[13px] font-medium">Details to send</span>
            <div className="flex flex-wrap gap-x-4 gap-y-2">
              {names.map((v) => (
                <label key={v} className="flex items-center gap-2 font-mono text-[12.5px]">
                  <Checkbox checked={picked.includes(v)} aria-label={`Send ${v}`} onCheckedChange={(on) => set((x) => {
                    const cur = new Set(Array.isArray(x.data.transfer_details) ? x.data.transfer_details : []);
                    if (on) cur.add(v); else cur.delete(v);
                    if (cur.size) x.data.transfer_details = names.filter((nm) => cur.has(nm)); else delete x.data.transfer_details;
                  })} />{v}
                </label>
              ))}
            </div>
            <p className="text-[12.5px] text-muted-foreground">{picked.length ? `${picked.length} selected.` : "None ticked: everything the call collected."}</p>
          </div>
          {sip && <TextField label="User-to-User data (optional)" value={text(d.transfer_uui)} insert={names} placeholder="e.g. {{account_id}}"
            onChange={(v) => set((x) => { if (v.trim()) x.data.transfer_uui = v; else delete x.data.transfer_uui; }, "tuui")}
            hint="For phone systems that read the User-to-User header, like Avaya and Cisco. Sent hex encoded." />}
        </>
      )}
    </Section>
  );
}

/* ---------------------------------------------------------------- steps */

export function NodePanel({ b, n }: { b: BuilderApi; n: DefNode }) {
  const doc = b.doc!;
  const d = n.data;
  const t = TYPES[n.type] || TYPES.agentNode;
  const isRoot = n.id === rootId(doc);
  const set = (fn: (x: DefNode) => void, key?: string) => b.update((dd) => { const x = dd.definition.nodes.find((y) => y.id === n.id); if (x) fn(x); }, { key: key && `n:${n.id}:${key}` });
  const vars = Object.keys(doc.context_variables);
  const lead = {
    startCall: isRoot ? "Every call begins here. The greeting is spoken word for word, then the caller goes to the root agent."
      : "Every call begins here. The greeting is spoken word for word, then the caller is transferred by what they say.",
    agentNode: isRoot ? "The root agent hears the caller first and transfers them to the right sub-agent."
      : "A sub-agent handles one job. It follows its instructions and transfers the caller when a condition is met.",
    endCall: text(d.transfer_to) ? "Says its line, then connects the caller to a person, with the details collected."
      : "Says a short goodbye, then hangs up. Or hand the caller to a person: see Transfer to a person.",
    globalNode: "Persona, tone and rules added to every step that includes global instructions.",
  }[n.type];
  const outs = doc.definition.edges.filter((e) => e.source === n.id);
  const ins = doc.definition.edges.filter((e) => e.target === n.id);
  const nodeName = (id: string) => text(doc.definition.nodes.find((x) => x.id === id)?.data.name) || id;
  const listed = Array.isArray(d.tools) ? d.tools : [];
  const xs = Array.isArray(d.extraction_variables) ? d.extraction_variables : [];

  return (
    <>
      <Head color={t.color} icon={ICONS[n.type]} kind={kindLabel(doc, n)} title={text(d.name) || n.id}
        onDelete={n.type !== "startCall" ? () => { deleteSel(b, { kind: "node", id: n.id }); } : undefined} deleteLabel="Delete step" />
      <Lead>{lead}</Lead>
      <div className="grid gap-4 px-5 pb-5">
        <TextField label="Name" value={text(d.name)} maxLength={80} onChange={(v) => set((x) => { x.data.name = v; }, "name")} />
        {(n.type === "agentNode" || n.type === "endCall") && <><ModeSwitch b={b} n={n} /><FlowField b={b} n={n} /></>}
        {n.type === "startCall" && (
          <TextField label="Greeting" area rows={3} value={text(d.greeting)} insert={vars} onChange={(v) => set((x) => { x.data.greeting = v; }, "greeting")}
            hint="Spoken exactly as written. Leave it empty to let the model greet." />
        )}
        {!isScripted(n) && <TextField label={n.type === "globalNode" ? "Global instructions" : "Instructions"} area rows={n.type === "startCall" ? 4 : 9} value={text(d.prompt)} insert={vars}
          onChange={(v) => set((x) => { x.data.prompt = v; }, "prompt")}
          hint={n.type === "startCall" ? "Used if the model greets, or if Start transfers by what the caller says." : "Write {{name}} to insert a context variable."} />}
      </div>
      {isScripted(n) && <ScriptEditor b={b} n={n} />}
      {n.type === "endCall" && <TransferSection b={b} n={n} />}
      {isScripted(n) && n.type !== "endCall" && (
        <Section title={`Transfers (${outs.length})`}>
          {outs.map((e) => <LinkRow key={e.id} main={<>{text(e.data.label) || "no label"}{routeKind(e) !== "ask" && <Badge variant="outline" className="ml-2">{RULE_LABEL[routeKind(e)]}</Badge>}</>}
            sub={<span className="inline-flex items-center gap-1"><ArrowRight className="size-3" />{nodeName(e.target)}</span>} onClick={() => b.select({ kind: "edge", id: e.id })} />)}
          <p className="text-[12.5px] text-muted-foreground">Rules are checked in this order once the form is complete. Drag from the dot under the card to add one.</p>
        </Section>
      )}
      {n.type !== "globalNode" && !isScripted(n) && (
        <>
          <Section title="Behavior">
            <Toggle id={`gp-${n.id}`} label="Include global instructions" checked={d.add_global_prompt !== false} onCheckedChange={(v) => set((x) => { x.data.add_global_prompt = v; })} />
            {n.type !== "endCall" && <Toggle id={`ai-${n.id}`} label="Caller can interrupt" hint="Stops speaking when the caller talks over it." checked={d.allow_interrupt !== false} onCheckedChange={(v) => set((x) => { x.data.allow_interrupt = v; })} />}
          </Section>
          <Section title={`Tools (${listed.length})`}>
            {doc.tools.length === 0 ? <p className="text-[13px] text-muted-foreground">No tools yet. Add one from the panel on the left, like a lookup of open appointment times.</p> : (
              <div className="grid gap-2">
                {doc.tools.map((tool, i) => (
                  <div key={i} className="flex items-start gap-3 rounded-lg border px-3 py-2.5">
                    <Checkbox id={`tool-${n.id}-${i}`} className="mt-0.5" checked={listed.includes(tool.name)} aria-label={`Use ${tool.name}`}
                      onCheckedChange={(v) => set((x) => {
                        const cur = new Set(Array.isArray(x.data.tools) ? x.data.tools : []);
                        if (v) cur.add(tool.name); else cur.delete(tool.name);
                        const order = doc.tools.map((y) => y.name);
                        const next = [...cur].sort((a, z) => order.indexOf(a) - order.indexOf(z));
                        if (next.length) x.data.tools = next; else delete x.data.tools;
                      })} />
                    <label htmlFor={`tool-${n.id}-${i}`} className="grid min-w-0 flex-1 cursor-pointer gap-0.5">
                      <span className="truncate font-mono text-[13px]">{tool.name || "(no name)"}</span>
                      {tool.description && <span className="line-clamp-2 text-[12.5px] text-muted-foreground">{tool.description}</span>}
                    </label>
                    <Button variant="ghost" size="sm" onClick={() => b.select({ kind: "tool", id: String(i) })}>Edit</Button>
                  </div>
                ))}
                <p className="text-[12.5px] text-muted-foreground">This step can call the tools you tick, and answers the caller from what they return.</p>
              </div>
            )}
          </Section>
          {n.type !== "endCall" && (
            <Section title="Collect details">
              <Toggle id={`ex-${n.id}`} label="Collect details when leaving this step" hint="Saved as variables the next steps can use."
                checked={!!d.extraction_enabled} onCheckedChange={(v) => set((x) => {
                  x.data.extraction_enabled = v;
                  if (v && !(x.data.extraction_variables || []).length) x.data.extraction_variables = [{ name: "", type: "string", prompt: "" }];
                })} />
              {d.extraction_enabled && (
                <>
                  <TextField label="How to read them" area rows={2} value={text(d.extraction_prompt)} placeholder="e.g. Use the caller's own words; leave out anything they didn't say."
                    onChange={(v) => set((x) => { x.data.extraction_prompt = v; }, "xprompt")} />
                  {xs.map((x0, i) => (
                    <div key={i} className="grid gap-2 rounded-lg border p-3">
                      <div className="grid grid-cols-[minmax(0,1fr)_110px_auto] gap-2">
                        <Input aria-label="Detail name" placeholder="name" className="h-8 font-mono text-[12.5px]" value={x0.name} aria-invalid={(!!x0.name && !VAR_NAME.test(x0.name)) || undefined}
                          onChange={(e) => set((x) => { x.data.extraction_variables![i].name = e.target.value.trim().replace(/\s+/g, "_"); }, `x${i}n`)} />
                        <NativeSelect aria-label="Detail type" value={["string", "number", "boolean"].includes(x0.type || "") ? x0.type : "string"}
                          onChange={(e) => set((x) => { x.data.extraction_variables![i].type = e.target.value; })}>
                          <option value="string">text</option><option value="number">number</option><option value="boolean">yes / no</option>
                        </NativeSelect>
                        <Button variant="ghost" size="icon-sm" aria-label="Remove detail" onClick={() => set((x) => { x.data.extraction_variables!.splice(i, 1); })}><Trash2 /></Button>
                      </div>
                      <Input aria-label="Detail description" placeholder="what it is, e.g. the day the caller wants" className="h-8" value={x0.prompt || ""}
                        onChange={(e) => set((x) => { x.data.extraction_variables![i].prompt = e.target.value; }, `x${i}p`)} />
                    </div>
                  ))}
                  <Button variant="outline" size="sm" className="justify-self-start" onClick={() => set((x) => { (x.data.extraction_variables ||= []).push({ name: "", type: "string", prompt: "" }); })}><Plus />Add a detail</Button>
                </>
              )}
            </Section>
          )}
          {n.type !== "endCall" && (
            <Section title={`Transfers (${outs.length})`}>
              {outs.map((e) => <LinkRow key={e.id} main={<>{text(e.data.label) || "no label"}{routeKind(e) !== "ask" && <Badge variant="outline" className="ml-2">{RULE_LABEL[routeKind(e)]}</Badge>}</>} sub={<span className="inline-flex items-center gap-1"><ArrowRight className="size-3" />{nodeName(e.target)}</span>} onClick={() => b.select({ kind: "edge", id: e.id })} />)}
              <p className="text-[12.5px] text-muted-foreground">To add one, drag from the dot under this card onto another step.</p>
            </Section>
          )}
          {ins.length > 0 && (
            <Section title="Reached from">
              {ins.map((e) => <LinkRow key={e.id} main={nodeName(e.source)} sub={`via ${text(e.data.label) || "no label"}`} onClick={() => b.select({ kind: "edge", id: e.id })} />)}
            </Section>
          )}
        </>
      )}
    </>
  );
}

/* ---------------------------------------------------------------- transfers */

export function EdgePanel({ b, e }: { b: BuilderApi; e: DefEdge }) {
  const doc = b.doc!;
  const a = doc.definition.nodes.find((x) => x.id === e.source)!;
  const z = doc.definition.nodes.find((x) => x.id === e.target)!;
  const set = (fn: (x: DefEdge, d: Doc) => void, key?: string) => b.update((dd) => { const x = dd.definition.edges.find((y) => y.id === e.id); if (x) fn(x, dd); }, { key: key && `e:${e.id}:${key}` });
  const tn = toolName(text(e.data.label));
  const jump = (n: DefNode) => (
    <button type="button" onClick={() => b.select({ kind: "node", id: n.id })} style={{ ["--k" as string]: TYPES[n.type]?.color }}
      className="inline-flex min-w-0 cursor-pointer items-center gap-1.5 rounded-md border bg-card px-2.5 py-1 text-[13px] font-medium hover:bg-accent">
      <span className="size-2 shrink-0 rounded-full bg-[var(--k)]" /><span className="truncate">{text(n.data.name) || n.id}</span>
    </button>
  );
  return (
    <>
      <Head color="var(--muted-foreground)" icon={GitBranch} kind="Transfer" onDelete={() => deleteSel(b, { kind: "edge", id: e.id })} deleteLabel="Delete transfer" />
      <div className="flex flex-wrap items-center gap-2 px-5 pb-3 pt-2">{a && jump(a)}<ArrowRight className="size-4 text-muted-foreground" />{z && jump(z)}</div>
      <Lead>The agent gets one tool per transfer. When the condition is true, it calls the tool and the caller moves to the next step.</Lead>
      <div className="grid gap-4 px-5 pb-5">
        <TextField label="Label" value={text(e.data.label)} maxLength={60} onChange={(v) => set((x) => { x.data.label = v; }, "label")}
          hint={tn.replace(/_/g, "") ? <>Tool name: <code className="text-foreground">{tn}</code></> : "Give it a short label, like “billing question”."} />
        {a?.type === "startCall" && (
          <Toggle id={`ag-${e.id}`} label={<span className="inline-flex items-center gap-1.5"><Zap className="size-3.5 text-k-start" />Right after the greeting</span>}
            hint="Hand the caller straight to this step once the greeting is spoken, without asking the model. One transfer from Start can do this."
            checked={!!e.data.after_greeting} onCheckedChange={(v) => set((x, dd) => {
              if (v) {
                dd.definition.edges.forEach((y) => { if (y.id !== x.id && y.source === x.source) delete y.data.after_greeting; });
                x.data.after_greeting = true;
                if (!text(x.data.condition).trim()) x.data.condition = "Right after the greeting.";
              } else delete x.data.after_greeting;
            })} />
        )}
        {a && a.type !== "startCall" && <RouteKind b={b} e={e} from={a} />}
        {!e.data.after_greeting && routeKind(e) === "ask" && (
          <TextField label="Transfer when" area rows={3} value={text(e.data.condition)} onChange={(v) => set((x) => { x.data.condition = v; }, "condition")}
            hint="Describe it from the caller's side, like “The caller wants to book an appointment.”" />
        )}
        <TextField label="Say while transferring (optional)" value={text(e.data.transition_speech)} placeholder="e.g. Let me check that for you."
          onChange={(v) => set((x) => { if (v) x.data.transition_speech = v; else delete x.data.transition_speech; }, "speech")}
          hint="Spoken as the caller moves to the next step." />
      </div>
    </>
  );
}

/* ---------------------------------------------------------------- agent */

export function AgentPanel({ b }: { b: BuilderApi }) {
  const doc = b.doc!;
  const glob = doc.definition.nodes.find((n) => n.type === "globalNode");
  return (
    <>
      <div className="px-5 pb-1 pt-5"><h2 className="text-[15px] font-semibold">Agent settings</h2></div>
      <Lead>Drag steps in from the left. Drag from the dot under a card onto another card to add a transfer. Click anything to edit it here.</Lead>
      <Section title="Context variables">
        <p className="text-[12.5px] text-muted-foreground">Values every step can use as {"{{name}}"}: business name, hours, address. Use made-up data while testing.</p>
        <PairsEditor key={b.rev} value={doc.context_variables} keyPh="name" valPh="value" keyMono validKey={(k) => VAR_NAME.test(k)} addLabel="Add a variable"
          onChange={(v) => b.update((d) => { d.context_variables = v; }, { key: "vars" })} />
      </Section>
      <Section title="Global instructions">
        <p className="text-[12.5px] text-muted-foreground">{glob ? "Persona, tone and rules shared by every step." : "None yet. Add a persona, tone and rules shared by every step."}</p>
        <Button variant="outline" size="sm" className="justify-self-start" onClick={() => glob ? b.select({ kind: "node", id: glob.id }) : addStep(b, "globalNode")}>
          <Globe />{glob ? "Edit global instructions" : "Add global instructions"}
        </Button>
      </Section>
      <Section title="Shortcuts">
        <ul className="grid gap-1.5 text-[12.5px] text-muted-foreground">
          <li><Kbd>⌘/Ctrl S</Kbd> save</li>
          <li><Kbd>⌘/Ctrl Z</Kbd> undo, <Kbd>⇧⌘/Ctrl Z</Kbd> redo</li>
          <li><Kbd>Delete</Kbd> remove the selected step, transfer or tool</li>
          <li>Drag the background to move around; scroll or pinch to zoom</li>
        </ul>
      </Section>
    </>
  );
}
const Kbd = ({ children }: { children: ReactNode }) => <kbd className="rounded border border-b-2 bg-muted px-1.5 font-mono text-[11px] text-foreground">{children}</kbd>;

/* ---------------------------------------------------------------- tools */

const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"];
const PARAM_TYPES: Record<string, string> = { string: "text", number: "number", integer: "whole number", boolean: "yes / no" };

export function ToolPanel({ b, i, secrets }: { b: BuilderApi; i: number; secrets: string[] }) {
  const doc = b.doc!;
  const t = doc.tools[i];
  const set = (fn: (x: Tool, d: Doc) => void, key?: string) => b.update((dd) => { const x = dd.tools[i]; if (x) fn(x, dd); }, { key: key && `t:${i}:${key}` });
  const params = Array.isArray(t.parameters) ? t.parameters : [];
  const users = doc.definition.nodes.filter((n) => usesTool(n, t.name));
  const method = METHODS.includes(String(t.method || "GET").toUpperCase()) ? String(t.method || "GET").toUpperCase() : "GET";
  const [tryVals, setTryVals] = useState<Record<string, string>>({});
  const [out, setOut] = useState<null | { busy?: boolean; r?: any; err?: string }>(null);
  useEffect(() => { setOut(null); }, [i]);
  const setParam = (j: number, fn: (p: Param) => void, key?: string) => set((x) => { fn(x.parameters![j]); }, key && `p${j}${key}`);

  async function run(mock: boolean) {
    const args: Record<string, unknown> = {};
    for (const p of params) {
      const v = (tryVals[p.name] || "").trim();
      if (!v) continue;
      args[p.name] = p.type === "number" || p.type === "integer" ? Number(v) : p.type === "boolean" ? /^(y|yes|true|1)$/i.test(v) : v;
    }
    setOut({ busy: true });
    try { setOut({ r: await api("POST", "/v1/tools/try", { tool: t, arguments: args, variables: doc.context_variables, mock }) }); }
    catch (e) { setOut({ err: (e as Error).message }); }
  }
  const r = out?.r;
  const body = r ? JSON.stringify(r.ok ? (r.data ?? r.data_truncated ?? null) : (r.data ?? null), null, 2) : "";

  return (
    <>
      <Head color="var(--k-tool)" icon={Plug} kind="Tool" title={<span className="font-mono">{t.name || "(no name)"}</span>} onDelete={() => deleteSel(b, { kind: "tool", id: String(i) })} deleteLabel="Delete tool" />
      <Lead>An HTTP request the agent can make during a call, like looking up open times. Steps that tick it can call it; its answer goes back to the agent, which tells the caller.</Lead>
      <div className="grid gap-4 px-5 pb-5">
        <TextField label="Name" mono value={t.name} maxLength={64} transform={(v) => v.replace(/[^A-Za-z0-9_]/g, "_")}
          onChange={(v) => set((x, d) => {
            for (const n of d.definition.nodes) renameTool(n, x.name, v);
            x.name = v;
          }, "name")}
          hint={TOOL_NAME.test(t.name) ? "The model sees this name. Letters, digits and underscores." : <span className="text-bad">Start with a letter; use letters, digits and underscores.</span>} />
        <TextField label="What it does" area rows={3} value={text(t.description)} placeholder="e.g. Look up open appointment times on a day. Call it once you know the day."
          onChange={(v) => set((x) => { x.description = v; }, "description")} hint="The model reads this to decide when to call it." />
        <div className="grid grid-cols-2 gap-3">
          <Field label="Method" htmlFor={`tm-${i}`}>
            <NativeSelect id={`tm-${i}`} value={method} onChange={(e) => set((x) => { x.method = e.target.value; })}>{METHODS.map((m) => <option key={m}>{m}</option>)}</NativeSelect>
          </Field>
          <Field label="Timeout (seconds)" htmlFor={`tt-${i}`}>
            <Input id={`tt-${i}`} type="number" min={1} max={30} value={t.timeout_seconds ?? 8}
              onChange={(e) => set((x) => { const v = Number(e.target.value); if (v >= 1 && v <= 30) x.timeout_seconds = v; else delete x.timeout_seconds; }, "timeout")} />
          </Field>
        </div>
        <TextField label="URL" mono value={text(t.url)} placeholder="https://api.example.com/v1/slots" onChange={(v) => set((x) => { x.url = v.trim(); }, "url")}
          insert={secrets} insertPrefix="secrets." insertLabel="Secrets"
          hint="Use {{parameter}} in the path. Other parameters go in the query (GET, DELETE) or the JSON body. The address itself can only use {{secrets.NAME}}. Leave it empty to answer with the mock while you build." />
        {secrets.length === 0 && <p className="-mt-2 text-[12.5px] text-muted-foreground">No tool secrets saved yet. <a className="underline" href="#/integrations">Add one in Integrations</a>.</p>}
      </div>
      <Section title="Headers">
        <p className="text-[12.5px] text-muted-foreground">For keys, write {"{{secrets.NAME}}"} and save the value in Integrations → Tool secrets. Never paste a key here.</p>
        <PairsEditor key={`h${i}-${b.rev}`} value={t.headers} keyPh="Header" valPh="Value" valMono onChange={(v) => set((x) => { if (Object.keys(v).length) x.headers = v; else delete x.headers; }, "headers")}
          extra={(ref) => <InsertChips names={secrets} prefix="secrets." target={ref} label="Secrets" />} />
      </Section>
      <Section title={`Parameters (${params.length})`}>
        <p className="text-[12.5px] text-muted-foreground">What the model fills in from the conversation.</p>
        {params.map((p, j) => (
          <div key={j} className="grid gap-2 rounded-lg border p-3">
            <div className="grid grid-cols-[minmax(0,1fr)_118px_auto] gap-2">
              <Input aria-label="Parameter name" placeholder="name" className="h-8 font-mono text-[12.5px]" value={p.name} aria-invalid={(!!p.name && !VAR_NAME.test(p.name)) || undefined}
                onChange={(e) => setParam(j, (q) => { q.name = e.target.value.trim().replace(/\s+/g, "_"); }, "n")} />
              <NativeSelect aria-label="Parameter type" value={p.type && p.type in PARAM_TYPES ? p.type : "string"} onChange={(e) => setParam(j, (q) => { q.type = e.target.value; })}>
                {Object.entries(PARAM_TYPES).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </NativeSelect>
              <Button variant="ghost" size="icon-sm" aria-label="Remove parameter" onClick={() => set((x) => { x.parameters!.splice(j, 1); })}><Trash2 /></Button>
            </div>
            <Input aria-label="Parameter description" placeholder="what the model should put here" className="h-8" value={p.description || ""}
              onChange={(e) => setParam(j, (q) => { q.description = e.target.value; }, "d")} />
            <label className="flex items-center gap-2 text-[12.5px] text-muted-foreground">
              <Checkbox checked={!!p.required} onCheckedChange={(v) => setParam(j, (q) => { if (v) q.required = true; else delete q.required; })} />
              Required: the model asks the caller if it doesn't know it yet
            </label>
          </div>
        ))}
        <Button variant="outline" size="sm" className="justify-self-start" onClick={() => set((x) => { (x.parameters ||= []).push({ name: "", type: "string", description: "", required: true }); })}><Plus />Add a parameter</Button>
      </Section>
      {!["GET", "DELETE"].includes(method) && (
        <Section title="Body">
          <JsonField key={`b${i}-${b.rev}`} label="JSON template (optional)" value={t.body} onChange={(v) => set((x) => { if (v === undefined) delete x.body; else x.body = v; }, "body")}
            hint={<>Like {"{\"slot\": \"{{time}}\"}"}. Empty: the parameters are sent as the JSON body.</>} />
        </Section>
      )}
      <Section title="While it runs">
        <TextField label="Say while running (optional)" value={text(t.say_while_running)} placeholder="e.g. One moment while I check."
          onChange={(v) => set((x) => { if (v.trim()) x.say_while_running = v; else delete x.say_while_running; }, "speech")}
          hint="Spoken while the request runs, so the caller isn't left in silence." />
      </Section>
      <Section title="Save to variables">
        <p className="text-[12.5px] text-muted-foreground">Keep parts of the answer for later steps, like first_open_time from slots.0.time.</p>
        <PairsEditor key={`s${i}-${b.rev}`} value={t.save} keyPh="variable" valPh="path, like slots.0.time" keyMono valMono validKey={(k) => VAR_NAME.test(k)}
          onChange={(v) => set((x) => { if (Object.keys(v).length) x.save = v; else delete x.save; }, "save")} />
      </Section>
      <Section title="Mock answer">
        <JsonField key={`m${i}-${b.rev}`} label="Example answer" value={t.mock} onChange={(v) => set((x) => { if (v === undefined) delete x.mock; else x.mock = v; }, "mock")}
          hint="Used by test chats and calls with “Mock tool answers” on, and always while the URL is empty. {{parameter}} works here too." />
      </Section>
      <Section title="Try it">
        {params.length > 0 && (
          <div className="grid gap-2">
            {params.map((p) => (
              <label key={p.name} className="grid grid-cols-[110px_minmax(0,1fr)] items-center gap-2 text-[12.5px]">
                <span className="truncate font-mono">{p.name || "?"}</span>
                <Input className="h-8" value={tryVals[p.name] || ""} onChange={(e) => setTryVals({ ...tryVals, [p.name]: e.target.value })} aria-label={`Value for ${p.name}`} />
              </label>
            ))}
          </div>
        )}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => run(true)} disabled={out?.busy}><Play />Try the mock</Button>
          <Button variant="outline" size="sm" onClick={() => run(false)} disabled={out?.busy || !text(t.url).trim()}
            title={text(t.url).trim() ? "Makes a real request with your secrets" : "Add a URL first"}><Play />Call the service</Button>
        </div>
        {out && (
          <div className="grid gap-2 rounded-lg border bg-muted/50 p-3 text-[12.5px]" role="status">
            {out.busy && <span className="text-muted-foreground">Running…</span>}
            {out.err && <span className="text-bad">{out.err}</span>}
            {r && (
              <>
                <div className="flex flex-wrap items-center gap-1.5">
                  <Badge variant={r.ok ? "ok" : "bad"}>{r.ok ? <CircleCheck /> : <CircleAlert />}{r.ok ? "OK" : "Failed"}</Badge>
                  <Badge variant="outline">{r.mock ? "Mock" : `Live${r.status ? ` · ${r.status}` : ""}`}</Badge>
                  <span className="text-muted-foreground">{r.seconds}s</span>
                  {r.request && <code className="truncate text-muted-foreground">{r.request}</code>}
                </div>
                {r.error && <p className="text-bad">{r.error}</p>}
                {r.saved && <p className="text-muted-foreground">Saves {Object.entries(r.saved).map(([k, v]) => `${k} = ${JSON.stringify(v)}`).join(", ")}</p>}
                {body && body !== "null" && <pre className="max-h-64 overflow-auto rounded-md bg-card p-2.5 font-mono text-xs whitespace-pre-wrap break-all">{body.length > 3000 ? `${body.slice(0, 3000)}\n…` : body}</pre>}
              </>
            )}
          </div>
        )}
      </Section>
      <Section title={`Used by (${users.length})`}>
        {users.length ? users.map((n) => <LinkRow key={n.id} main={text(n.data.name) || n.id} sub={kindLabel(doc, n)} color={TYPES[n.type]?.color} onClick={() => b.select({ kind: "node", id: n.id })} />)
          : <p className="text-[12.5px] text-muted-foreground">No step uses it yet. Open a step and tick it under Tools.</p>}
      </Section>
    </>
  );
}

/* ---------------------------------------------------------------- problems */

export function Problems({ b, onJump }: { b: BuilderApi; onJump: (where: string) => void }) {
  const doc = b.doc!;
  const sel = b.sel;
  const list = sel ? (sel.kind === "hook" ? b.findings.filter((f) => f.where.startsWith(`node ${sel.id}`)) : findingsFor(b.findings, doc, sel.kind, sel.id)) : b.findings;
  if (!list.length && sel) return null;
  return (
    <Section title={sel ? "Problems here" : list.length ? `Problems (${list.length})` : "Checks"}>
      {b.checkError && <p className="text-[13px] text-bad">Checks didn't run: {b.checkError}</p>}
      {!list.length ? <p className="flex items-center gap-1.5 text-[13px] text-ok"><CircleCheck className="size-4" />No problems found.</p> : (
        <ul className="grid gap-2">
          {list.map((f, k) => (
            <li key={k}>
              <button type="button" onClick={() => onJump(f.where)}
                className={cn("grid w-full cursor-pointer gap-0.5 rounded-lg border border-l-[3px] bg-card px-3 py-2 text-left text-[12.5px] leading-snug hover:bg-accent",
                  f.severity === "error" ? "border-l-bad" : "border-l-warn")}>
                <span><span className="font-mono text-muted-foreground">{f.code}</span> {f.message}</span>
                {!sel && f.where && f.where !== "agent" && <span className="text-[11.5px] text-muted-foreground">{f.where}</span>}
              </button>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}

/* ---------------------------------------------------------------- actions shared with the canvas */

export function addStep(b: BuilderApi, type: NodeType, at?: { x: number; y: number }) {
  const doc = b.doc!;
  if (type === "globalNode") {
    const g = doc.definition.nodes.find((n) => n.type === "globalNode");
    if (g) { b.select({ kind: "node", id: g.id }); return; }
  }
  const base = { agentNode: "agent", endCall: "end", globalNode: "global", startCall: "start" }[type];
  const ids = new Set(doc.definition.nodes.map((n) => n.id));
  let id = base, k = 2;
  while (ids.has(id)) id = `${base}-${k++}`;
  const pos = at || { x: 0, y: 0 };
  b.update((d) => {
    d.definition.nodes.push({ id, type, position: { x: Math.round(pos.x / 10) * 10, y: Math.round(pos.y / 10) * 10 }, data: newData(type, d) });
  });
  b.select({ kind: "node", id });
}
function newData(type: NodeType, d: Doc) {
  const names = d.definition.nodes.map((n) => text(n.data.name).toLowerCase());
  const uniq = (base: string) => { if (!names.includes(base.toLowerCase())) return base; let k = 2; while (names.includes(`${base} ${k}`.toLowerCase())) k++; return `${base} ${k}`; };
  if (type === "agentNode") return { name: uniq("New sub-agent"), prompt: "Describe this agent's job: what it helps the caller with, what it asks, and what it must not do.", add_global_prompt: true, allow_interrupt: true };
  if (type === "endCall") return { name: uniq("Goodbye"), prompt: "Thank the caller and say goodbye in one short sentence.", add_global_prompt: true };
  return { name: "Global instructions", prompt: "You are a friendly phone assistant. Keep answers short and natural; this is a phone call." };
}

export function addHttpTool(b: BuilderApi) {
  const names = new Set(b.doc!.tools.map((t) => t.name));
  let name = "new_tool", k = 2;
  while (names.has(name)) name = `new_tool_${k++}`;
  b.update((d) => { d.tools.push({ name, description: "", method: "GET", url: "", parameters: [], mock: { note: "an example answer, used while testing" } }); });
  b.select({ kind: "tool", id: String(b.doc!.tools.length) });
}

export function deleteSel(b: BuilderApi, sel: NonNullable<BuilderApi["sel"]>): boolean {
  const doc = b.doc!;
  if (sel.kind === "node") {
    const n = doc.definition.nodes.find((x) => x.id === sel.id);
    if (!n || n.type === "startCall") return false;
    b.update((d) => {
      d.definition.nodes = d.definition.nodes.filter((x) => x.id !== n.id);
      d.definition.edges = d.definition.edges.filter((e) => e.source !== n.id && e.target !== n.id);
    });
  } else if (sel.kind === "edge") {
    b.update((d) => { d.definition.edges = d.definition.edges.filter((e) => e.id !== sel.id); });
  } else if (sel.kind === "trigger") {
    b.update((d) => { d.triggers.splice(+sel.id, 1); });
  } else if (sel.kind === "hook") {
    b.update((d) => { d.definition.nodes = d.definition.nodes.filter((n) => n.id !== sel.id); });
  } else {
    const t = doc.tools[+sel.id];
    if (!t) return false;
    b.update((d) => {
      d.tools.splice(+sel.id, 1);
      for (const n of d.definition.nodes) renameTool(n, t.name, null);
    });
  }
  b.select(null);
  return true;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="px-5 py-8 text-sm text-muted-foreground">{children}</div>;
}

export { ICONS };
