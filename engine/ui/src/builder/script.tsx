import { useId } from "react";
import { ArrowDown, ArrowUp, Bot, ListChecks, Plus, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input, NativeSelect } from "@/components/ui/input";
import { Field } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/switch";
import type { BuilderApi } from "./useBuilder";
import type { DefEdge, DefNode, Doc } from "./model";
import { text } from "./model";
import { Section, TextField } from "./fields";

/* Scripted pages: say exactly this, ask these fields word for word, move on by rules. */

export type FormField = {
  name: string; type?: string; ask?: string; required?: boolean; reprompt?: string; confirm?: string;
  choices?: (string | { value: string; synonyms?: string[] })[]; min?: number; max?: number; min_length?: number; max_length?: number;
  pattern?: string; keypad?: boolean;
};

const FIELD_TYPES: Record<string, string> = {
  text: "text (a name, a reason…)", number: "number", integer: "whole number", yes_no: "yes or no", choice: "one of a list",
  date: "date", time: "time", digits: "digits (member ID, ZIP…)", phone: "phone number", email: "email address",
};
const KEYPAD = ["number", "integer", "digits", "phone", "yes_no", "choice"];

export const isScripted = (n: DefNode) => n.data.mode === "scripted";

/** Every variable a step could use: context, collected details, form fields. */
export function allVariables(doc: Doc) {
  const names = new Set(Object.keys(doc.context_variables));
  for (const n of doc.definition.nodes) {
    for (const v of n.data.extraction_variables || []) if (v.name) names.add(v.name);
    for (const f of (n.data.form as FormField[] | undefined) || []) if (f.name) names.add(f.name);
  }
  for (const t of doc.tools) for (const k of Object.keys(t.save || {})) names.add(k);
  return [...names];
}

export function flows(doc: Doc) {
  const out = new Map<string, number>();
  for (const n of doc.definition.nodes) { const f = text(n.data.flow).trim(); if (f) out.set(f, (out.get(f) || 0) + 1); }
  return [...out.entries()];
}

const HUES = [210, 150, 30, 280, 350, 190, 90, 250];
export function flowColor(name: string) {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return `oklch(0.62 0.12 ${HUES[h % HUES.length]})`;
}

export function ModeSwitch({ b, n }: { b: BuilderApi; n: DefNode }) {
  const scripted = isScripted(n);
  const set = (mode: "llm" | "scripted") => b.update((d) => {
    const x = d.definition.nodes.find((y) => y.id === n.id)!;
    if (mode === "scripted") {
      x.data.mode = "scripted";
      if (x.type === "agentNode" && !Array.isArray(x.data.form)) x.data.form = [];
      if (!text(x.data.say).trim()) x.data.say = x.type === "endCall" ? "Thank you for calling. Goodbye." : "";
    } else delete x.data.mode;
  });
  const Opt = ({ on, icon: Icon, title, sub, onClick }: { on: boolean; icon: typeof Bot; title: string; sub: string; onClick: () => void }) => (
    <button type="button" onClick={onClick} aria-pressed={on}
      className={cn("grid cursor-pointer gap-0.5 rounded-lg border px-3 py-2 text-left transition-colors", on ? "border-lamp bg-lamp-soft" : "hover:bg-accent")}>
      <span className="flex items-center gap-1.5 text-[13px] font-medium"><Icon className="size-3.5" />{title}</span>
      <span className="text-[11.5px] leading-snug text-muted-foreground">{sub}</span>
    </button>
  );
  return (
    <div className="grid grid-cols-2 gap-2" role="group" aria-label="How this step talks">
      <Opt on={!scripted} icon={Bot} title="LLM" sub="Follows instructions; the model speaks." onClick={() => set("llm")} />
      <Opt on={scripted} icon={ListChecks} title="Scripted" sub={n.type === "endCall" ? "Says exactly your goodbye." : "Exact lines, form fields, rules."} onClick={() => set("scripted")} />
    </div>
  );
}

export function FlowField({ b, n }: { b: BuilderApi; n: DefNode }) {
  const id = useId();
  const doc = b.doc!;
  return (
    <Field label="Flow (optional)" htmlFor={id} hint="Groups steps, like Dialogflow CX flows. Steps in a flow share a color on the canvas.">
      <Input id={id} list={`${id}-flows`} value={text(n.data.flow)} placeholder="e.g. Booking"
        onChange={(e) => b.update((d) => { const x = d.definition.nodes.find((y) => y.id === n.id)!; if (e.target.value) x.data.flow = e.target.value; else delete x.data.flow; }, { key: `n:${n.id}:flow` })} />
      <datalist id={`${id}-flows`}>{flows(doc).map(([f]) => <option key={f} value={f} />)}</datalist>
    </Field>
  );
}

export function ScriptEditor({ b, n }: { b: BuilderApi; n: DefNode }) {
  const doc = b.doc!;
  const d = n.data;
  const vars = allVariables(doc);
  const form: FormField[] = Array.isArray(d.form) ? (d.form as FormField[]) : [];
  const set = (fn: (x: DefNode) => void, key?: string) => b.update((dd) => { const x = dd.definition.nodes.find((y) => y.id === n.id); if (x) fn(x); }, { key: key && `n:${n.id}:${key}` });
  const setField = (i: number, fn: (f: FormField) => void, key?: string) => set((x) => { fn((x.data.form as FormField[])[i]); }, key && `f${i}${key}`);
  const isEnd = n.type === "endCall";
  return (
    <>
      <div className="grid gap-4 px-5 pb-5">
        <TextField label={isEnd ? "Goodbye" : "Say first"} area rows={3} value={text(d.say)} insert={vars}
          onChange={(v) => set((x) => { if (v) x.data.say = v; else delete x.data.say; }, "say")}
          hint={isEnd ? "Spoken exactly as written, then the call ends." : "Spoken exactly as written when this step begins. Optional."} />
      </div>
      <PageTools b={b} n={n} />
      {!isEnd && (
        <>
          <Section title={`Form (${form.length})`}>
            <p className="text-[12.5px] text-muted-foreground">Asked one at a time, word for word, until each has a valid answer. Answers are saved under the field's name for later steps and rules.</p>
            {form.map((f, i) => {
              const type = f.type || "text";
              const choices = (f.choices || []).map((c) => (typeof c === "string" ? c : [c.value, ...(c.synonyms || [])].join(" | "))).join("\n");
              return (
                <div key={i} className="grid gap-2.5 rounded-lg border p-3" data-field={f.name}>
                  <div className="grid grid-cols-[minmax(0,1fr)_auto] gap-2">
                    <Input aria-label="Field name" placeholder="name" className="h-8 font-mono text-[12.5px]" value={f.name}
                      onChange={(e) => setField(i, (q) => { q.name = e.target.value.trim().replace(/\W+/g, "_"); }, "n")} />
                    <div className="flex">
                      <Button variant="ghost" size="icon-sm" aria-label="Move up" disabled={i === 0} onClick={() => set((x) => { const a = x.data.form as FormField[]; [a[i - 1], a[i]] = [a[i], a[i - 1]]; })}><ArrowUp /></Button>
                      <Button variant="ghost" size="icon-sm" aria-label="Move down" disabled={i === form.length - 1} onClick={() => set((x) => { const a = x.data.form as FormField[]; [a[i + 1], a[i]] = [a[i], a[i + 1]]; })}><ArrowDown /></Button>
                      <Button variant="ghost" size="icon-sm" aria-label="Remove field" onClick={() => set((x) => { (x.data.form as FormField[]).splice(i, 1); })}><Trash2 /></Button>
                    </div>
                  </div>
                  <NativeSelect aria-label="Field type" value={type} onChange={(e) => setField(i, (q) => { q.type = e.target.value; })}>
                    {Object.entries(FIELD_TYPES).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
                  </NativeSelect>
                  <TextField label="Ask" value={f.ask || ""} insert={vars} quietInsert placeholder="e.g. What's the member ID on your card?"
                    onChange={(v) => setField(i, (q) => { q.ask = v; }, "a")} />
                  {type === "choice" && (
                    <Field label="Choices" htmlFor={`ch-${n.id}-${i}`} hint="One per line. Add other ways to say it after a |, like Bronze | basic.">
                      <textarea id={`ch-${n.id}-${i}`} rows={3} className="w-full rounded-md border bg-transparent px-3 py-2 text-[13px] shadow-xs outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        defaultValue={choices} onChange={(e) => setField(i, (q) => {
                          q.choices = e.target.value.split("\n").map((l) => l.split("|").map((s) => s.trim()).filter(Boolean)).filter((p) => p.length)
                            .map(([value, ...syn]) => (syn.length ? { value, synonyms: syn } : value));
                        }, "c")} />
                    </Field>
                  )}
                  {(type === "number" || type === "integer") && (
                    <div className="grid grid-cols-2 gap-2">
                      <Input aria-label="Smallest" placeholder="smallest" type="number" className="h-8" value={f.min ?? ""} onChange={(e) => setField(i, (q) => { if (e.target.value === "") delete q.min; else q.min = Number(e.target.value); }, "min")} />
                      <Input aria-label="Largest" placeholder="largest" type="number" className="h-8" value={f.max ?? ""} onChange={(e) => setField(i, (q) => { if (e.target.value === "") delete q.max; else q.max = Number(e.target.value); }, "max")} />
                    </div>
                  )}
                  {(type === "digits" || type === "phone") && (
                    <div className="grid grid-cols-2 gap-2">
                      <Input aria-label="Fewest digits" placeholder="fewest digits" type="number" className="h-8" value={f.min_length ?? ""} onChange={(e) => setField(i, (q) => { if (e.target.value === "") delete q.min_length; else q.min_length = Number(e.target.value); }, "minl")} />
                      <Input aria-label="Most digits" placeholder="most digits" type="number" className="h-8" value={f.max_length ?? ""} onChange={(e) => setField(i, (q) => { if (e.target.value === "") delete q.max_length; else q.max_length = Number(e.target.value); }, "maxl")} />
                    </div>
                  )}
                  <TextField label="If the answer doesn't fit, say" value={f.reprompt || ""} insert={vars} quietInsert placeholder="default: Sorry, I didn't catch that, then the question again"
                    onChange={(v) => setField(i, (q) => { if (v) q.reprompt = v; else delete q.reprompt; }, "r")} />
                  <TextField label="Confirm (optional)" value={f.confirm || ""} placeholder={`e.g. So that's {{${f.name || "name"}}}, right?`}
                    onChange={(v) => setField(i, (q) => { if (v) q.confirm = v; else delete q.confirm; }, "cf")} hint="A yes/no read-back; “no” asks again." />
                  <div className="flex flex-wrap gap-x-4 gap-y-2 text-[12.5px] text-muted-foreground">
                    <label className="flex items-center gap-1.5"><Checkbox checked={f.required !== false} onCheckedChange={(v) => setField(i, (q) => { if (v) delete q.required; else q.required = false; })} />Required</label>
                    {KEYPAD.includes(type) && <label className="flex items-center gap-1.5"><Checkbox checked={!!f.keypad} onCheckedChange={(v) => setField(i, (q) => { if (v) q.keypad = true; else delete q.keypad; })} />Keypad on phone calls</label>}
                  </div>
                  {(type === "text" || type === "digits") && (
                    <Input aria-label="Pattern" placeholder="pattern (optional regular expression, e.g. [A-Z]{2}\d{6})" className="h-8 font-mono text-[12px]" value={f.pattern || ""}
                      onChange={(e) => setField(i, (q) => { if (e.target.value) q.pattern = e.target.value; else delete q.pattern; }, "p")} />
                  )}
                </div>
              );
            })}
            <Button variant="outline" size="sm" className="justify-self-start" onClick={() => set((x) => {
              const a = (x.data.form ||= []) as FormField[];
              let k = a.length + 1; while (a.some((q) => q.name === `field_${k}`)) k++;
              a.push({ name: `field_${k}`, type: "text", ask: "" });
            })}><Plus />Add a field</Button>
          </Section>
          <Section title="Answers and wording">
            <Field label="Reading answers" htmlFor={`int-${n.id}`}
              hint={d.interpret === "exact" ? "Only the built-in parsers read answers: the same words always give the same result, and the model is never called here."
                : "Built-in parsers first; the model helps with free text and notices when the caller asks for something else (your model-decided transfers)."}>
              <NativeSelect id={`int-${n.id}`} value={d.interpret === "exact" ? "exact" : "assisted"} onChange={(e) => set((x) => { if (e.target.value === "exact") x.data.interpret = "exact"; else delete x.data.interpret; })}>
                <option value="assisted">Assisted: parsers, plus the model for free text</option>
                <option value="exact">Exact: parsers only, fully deterministic</option>
              </NativeSelect>
            </Field>
            <Field label="Tries before “no match”" htmlFor={`ma-${n.id}`} hint="After this many unusable answers to one field, a “no match” transfer is taken if there is one.">
              <Input id={`ma-${n.id}`} type="number" min={1} max={10} className="w-24" value={Number(d.max_attempts) || 3}
                onChange={(e) => set((x) => { const v = Number(e.target.value); if (v >= 1 && v <= 10) x.data.max_attempts = v; })} />
            </Field>
            <TextField label="When the form is complete, say (optional)" value={text(d.done_say)} insert={vars}
              onChange={(v) => set((x) => { if (v) x.data.done_say = v; else delete x.data.done_say; }, "done")} />
            <TextField label="When nothing matches, say (optional)" value={text(d.no_match_say)}
              onChange={(v) => set((x) => { if (v) x.data.no_match_say = v; else delete x.data.no_match_say; }, "nm")} placeholder="Sorry, I didn't catch that." />
          </Section>
        </>
      )}
    </>
  );
}

/* Transfers: decided by the model, or by a rule. */

export const routeKind = (e: DefEdge) => (e.data.on === "no_match" ? "no_match" : e.data.otherwise ? "otherwise" : text(e.data.when).trim() || e.data.when === "" ? "when" : "ask");
export const RULE_LABEL: Record<string, string> = { when: "rule", otherwise: "otherwise", no_match: "no match" };

export function RouteKind({ b, e, from }: { b: BuilderApi; e: DefEdge; from: DefNode }) {
  const id = useId();
  const doc = b.doc!;
  const kind = routeKind(e);
  const vars = allVariables(doc);
  const set = (fn: (x: DefEdge) => void, key?: string) => b.update((dd) => { const x = dd.definition.edges.find((y) => y.id === e.id); if (x) fn(x); }, { key: key && `e:${e.id}:${key}` });
  const scripted = isScripted(from);
  return (
    <>
      <Field label="Decided by" htmlFor={id}>
        <NativeSelect id={id} value={kind} onChange={(ev) => set((x) => {
          delete x.data.when; delete x.data.otherwise; delete x.data.on;
          if (ev.target.value === "when") { x.data.when = ""; }
          if (ev.target.value === "otherwise") { x.data.otherwise = true; x.data.condition = "Otherwise."; }
          if (ev.target.value === "no_match") { x.data.on = "no_match"; x.data.condition = "When the caller's answers can't be understood."; }
        })}>
          <option value="ask">The model: when the caller's words fit a description</option>
          <option value="when">A rule: when a condition on variables is true</option>
          {scripted && <option value="otherwise">Otherwise: when no rule matched</option>}
          {scripted && <option value="no_match">No match: after too many unusable answers</option>}
        </NativeSelect>
      </Field>
      {kind === "when" && (
        <TextField label="Condition" mono value={text(e.data.when)} insert={vars} insertLabel="Variables" placeholder={'e.g. plan == "Gold" and party >= 4'}
          onChange={(v) => set((x) => { x.data.when = v.replace(/\{\{\s*(\w+)\s*\}\}/g, "$1"); x.data.condition = `When ${x.data.when}.`; }, "when")}
          hint={scripted ? "Checked in order once the form is complete. Use variable names, \"text\", numbers, true/false, == != < > <= >=, in, and, or, not."
            : "Checked after each reply, without asking the model. Use variable names, \"text\", numbers, true/false, == != < > <= >=, in, and, or, not."} />
      )}
      {kind === "ask" && scripted && <p className="-mt-2 text-[12.5px] text-muted-foreground">On a scripted page the model checks this on every caller turn, like a Dialogflow intent: it only picks the transfer, never speaks.</p>}
    </>
  );
}

/** Tools a scripted page runs itself: when it begins, and once its form is complete (Dialogflow's page webhooks). */
function PageTools({ b, n }: { b: BuilderApi; n: DefNode }) {
  const doc = b.doc!;
  const isEnd = n.type === "endCall";
  const names = doc.tools.map((t) => t.name).filter(Boolean);
  const list = (k: string): string[] => (Array.isArray(n.data[k]) ? (n.data[k] as string[]) : []);
  const toggle = (k: string, name: string, on: boolean) => b.update((d) => {
    const x = d.definition.nodes.find((y) => y.id === n.id)!;
    const cur = Array.isArray(x.data[k]) ? (x.data[k] as string[]) : [];
    const next = on ? [...cur, name] : cur.filter((y) => y !== name);
    if (next.length) x.data[k] = next; else delete x.data[k];
  });
  if (!names.length) return null;
  const rows: [string, string, string][] = [["on_entry", "When the page begins", "Before it speaks: look up what it needs to say."]];
  if (!isEnd) rows.push(["on_done", "Once the form is complete", "Before its rules: look up the record for the ID just collected, or save the answers."]);
  return (
    <Section title="Run tools">
      <p className="text-[12.5px] text-muted-foreground">The page runs these itself, in order, without the model. Arguments come from variables of the same name or from the tool's fixed values; what they save can be used in the page's lines and rules.</p>
      {rows.map(([k, label, hint]) => (
        <div key={k} className="grid gap-1.5" role="group" aria-label={label}>
          <span className="text-[13px] font-medium">{label}</span>
          <span className="text-[12px] text-muted-foreground">{hint}</span>
          <div className="flex flex-wrap gap-x-4 gap-y-1.5 text-[13px]">
            {names.map((name) => (
              <label key={name} className="flex items-center gap-1.5 font-mono">
                <Checkbox checked={list(k).includes(name)} onCheckedChange={(v) => toggle(k, name, !!v)} />{name}
              </label>
            ))}
          </div>
        </div>
      ))}
    </Section>
  );
}
