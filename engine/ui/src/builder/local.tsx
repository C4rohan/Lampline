import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, NativeSelect } from "@/components/ui/input";
import { Field } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/switch";
import type { BuilderApi } from "./useBuilder";
import type { Tool } from "./model";
import { text, VAR_NAME } from "./model";
import { JsonField, PairsEditor, Section, TextField } from "./fields";
import { allVariables } from "./script";
import { CONNECTORS, Head, LOCAL_SERVICES, NameField, TryBox } from "./extras";

/* Tools on the engine's own data: look up or save a record, search a knowledge base. */

export const isLocal = (t: Tool) => !!t.connector && LOCAL_SERVICES.includes(CONNECTORS[t.connector]?.service || "");

type Collection = { name: string; count: number; key: string; fields: string[] };
type Base = { name: string; docs: number; passages: number };

function useLists() {
  const [cols, setCols] = useState<Collection[] | null>(null);
  const [bases, setBases] = useState<Base[] | null>(null);
  useEffect(() => {
    api<{ collections: Collection[] }>("GET", "/v1/data").then((r) => setCols(r.collections)).catch(() => setCols([]));
    api<{ bases: Base[] }>("GET", "/v1/knowledge").then((r) => setBases(r.bases)).catch(() => setBases([]));
  }, []);
  return { cols, bases };
}

function Pick({ id, label, value, options, onChange, hint, empty }: {
  id: string; label: string; value: string; options: string[]; onChange: (v: string) => void; hint?: React.ReactNode; empty: string;
}) {
  const known = options.includes(value) || !value;
  return (
    <Field label={label} htmlFor={id} hint={hint}>
      <NativeSelect id={id} value={known ? value : "__other"} onChange={(e) => onChange(e.target.value === "__other" ? value : e.target.value)}>
        <option value="">{empty}</option>
        {options.map((o) => <option key={o} value={o}>{o}</option>)}
        {!known && <option value="__other">{value} (not created yet)</option>}
      </NativeSelect>
    </Field>
  );
}

export function LocalToolPanel({ b, i, onDelete }: { b: BuilderApi; i: number; onDelete: () => void }) {
  const doc = b.doc!;
  const t = doc.tools[i];
  const kind = t.connector as "data.lookup" | "data.save" | "knowledge.search";
  const c = CONNECTORS[kind];
  const w = (t.with || {}) as Record<string, any>;
  const set = (fn: (x: Tool) => void, key?: string) => b.update((d) => { fn(d.tools[i]); }, { key: key && `t:${i}:${key}` });
  const setWith = (k: string, v: unknown, key?: string) => set((x) => {
    const n = { ...((x.with || {}) as Record<string, any>) };
    if (v === undefined || v === "" || (Array.isArray(v) && !v.length)) delete n[k]; else n[k] = v;
    x.with = n;
  }, key || `w-${k}`);
  const { cols, bases } = useLists();
  const vars = allVariables(doc);
  const col = cols?.find((x) => x.name === w.collection);
  const fieldNames = [...new Set([...(col?.fields || []), ...(col?.key ? [col.key] : [])])];
  const valueVar = typeof w.value === "string" ? (w.value.match(/^\{\{\s*(\w+)\s*\}\}$/)?.[1] || "") : "";
  const ask: string[] = Array.isArray(w.ask) ? w.ask : [];
  const keep: string[] = Array.isArray(w.fields) ? w.fields : [];

  return (
    <>
      <Head icon={c.icon} kind={c.label} title={<span className="font-mono">{t.name || "(no name)"}</span>} onDelete={onDelete} />
      <p className="px-5 pb-4 text-[13px] leading-relaxed text-muted-foreground">
        {c.blurb} {kind === "knowledge.search" ? <>Manage documents on the <a className="underline" href="#/knowledge">Knowledge</a> page.</>
          : <>Records live on this computer; manage them on the <a className="underline" href="#/data">Data</a> page.</>}
      </p>
      <div className="grid gap-4 px-5 pb-5">
        <NameField b={b} i={i} />
        <TextField label="When to use it" area rows={2} value={text(t.description)} onChange={(v) => set((x) => { x.description = v; }, "d")}
          hint={kind === "knowledge.search" ? "Like “Questions about hours, parking or insurance.” The model reads this to decide when to search."
            : "The model reads this to decide when to call it. A scripted page runs it without the model."} />
        {kind === "knowledge.search" ? (
          <>
            <Pick id={`kb-${i}`} label="Knowledge base" value={text(w.base)} options={(bases || []).map((x) => x.name)} empty="Choose a knowledge base"
              onChange={(v) => setWith("base", v)} hint={bases && !bases.length ? <>None yet: <a className="underline" href="#/knowledge">create one</a>.</> : undefined} />
            <Field label="Passages to return" htmlFor={`kr-${i}`}>
              <Input id={`kr-${i}`} type="number" min={1} max={10} className="h-9 w-24" value={w.results ?? ""} placeholder="3"
                onChange={(e) => setWith("results", e.target.value ? Number(e.target.value) : undefined)} />
            </Field>
          </>
        ) : (
          <Pick id={`dc-${i}`} label="Collection" value={text(w.collection)} options={(cols || []).map((x) => x.name)} empty="Choose a collection"
            onChange={(v) => setWith("collection", v)} hint={cols && !cols.length ? <>None yet: <a className="underline" href="#/data">create one</a>.</> : undefined} />
        )}
        {kind === "data.lookup" && (
          <>
            <Field label="Match on field" htmlFor={`df-${i}`} hint="Case, spaces and dashes don't matter: “hc 4271” finds HC-4271.">
              <Input id={`df-${i}`} list={`dfl-${i}`} className="h-9 font-mono" value={text(w.field)} placeholder="e.g. confirmation"
                onChange={(e) => setWith("field", e.target.value.trim())} />
              <datalist id={`dfl-${i}`}>{fieldNames.map((f) => <option key={f} value={f} />)}</datalist>
            </Field>
            <Field label="Value to look for" htmlFor={`dv-${i}`}>
              <NativeSelect id={`dv-${i}`} value={valueVar} onChange={(e) => setWith("value", e.target.value ? `{{${e.target.value}}}` : undefined)}>
                <option value="">The model fills it in from the conversation</option>
                {vars.map((v) => <option key={v} value={v}>{`{{${v}}}`}</option>)}
              </NativeSelect>
            </Field>
            {fieldNames.length > 0 && (
              <Field label="Fields the model sees" hint="Leave all unticked to return whole records. Fewer fields keep private details out of the conversation.">
                <div className="flex flex-wrap gap-x-4 gap-y-1.5 text-[13px]">
                  {fieldNames.map((f) => (
                    <label key={f} className="flex items-center gap-1.5 font-mono">
                      <Checkbox checked={keep.includes(f)} onCheckedChange={(v) => setWith("fields", v ? [...keep, f] : keep.filter((x) => x !== f))} />{f}
                    </label>
                  ))}
                </div>
              </Field>
            )}
          </>
        )}
        {kind === "data.save" && (
          <>
            <Field label="The record" hint="Field names, and values fixed or from variables like {{caller_name}}.">
              <PairsEditor key={`dv${i}-${b.rev}`} value={(w.values || {}) as Record<string, string>} keyPh="field" valPh="value or {{variable}}" keyMono
                validKey={(k) => /^[A-Za-z_]\w*$/.test(k)} onChange={(v) => setWith("values", Object.keys(v).length ? v : undefined, "values")} />
            </Field>
            <TextField label="Ask the model for (optional)" mono value={ask.join(", ")} placeholder="e.g. reason"
              onChange={(v) => setWith("ask", v.split(",").map((s) => s.trim()).filter(Boolean), "ask")}
              hint="Names the model fills in from the conversation. Use them in the record as {{reason}}." />
          </>
        )}
        {(!w.collection && kind !== "knowledge.search") || (kind === "knowledge.search" && !w.base)
          ? <Badge variant="warn" className="justify-self-start">Choose where it reads from</Badge> : null}
      </div>
      {kind === "data.lookup" && (
        <Section title="Save to variables">
          <p className="text-[12.5px] text-muted-foreground">Keep what's found for later steps and rules, like status from records.0.status.</p>
          {fieldNames.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {fieldNames.filter((f) => !Object.values(t.save || {}).includes(`records.0.${f}`)).slice(0, 8).map((f) => (
                <Button key={f} variant="outline" size="sm" className="h-7 font-mono text-[12px]"
                  onClick={() => set((x) => {
                    x.save = { ...(x.save || {}), [VAR_NAME.test(f) ? f : `found_${f}`]: `records.0.${f}` };
                    const xw = (x.with || {}) as Record<string, any>;          // a saved field must come back from the lookup
                    if (Array.isArray(xw.fields) && xw.fields.length && !xw.fields.includes(f)) x.with = { ...xw, fields: [...xw.fields, f] };
                  })}>+ {f}</Button>
              ))}
              {!Object.values(t.save || {}).includes("found") && (
                <Button variant="outline" size="sm" className="h-7 font-mono text-[12px]" onClick={() => set((x) => { x.save = { ...(x.save || {}), found: "found" }; })}>+ found</Button>
              )}
            </div>
          )}
          <PairsEditor key={`s${i}-${b.rev}-${Object.keys(t.save || {}).length}`} value={t.save} keyPh="variable" valPh="path, like records.0.status" keyMono valMono
            validKey={(k) => VAR_NAME.test(k)} onChange={(v) => set((x) => { if (Object.keys(v).length) x.save = v; else delete x.save; }, "save")} />
        </Section>
      )}
      <Section title="Mock answer">
        <JsonField key={`m${i}-${b.rev}`} label="Example answer" value={t.mock} onChange={(v) => set((x) => { if (v === undefined) delete x.mock; else x.mock = v; }, "mock")}
          hint={kind === "data.save" ? "Used in test chats with “Mock tool answers” on, so testing doesn't add records."
            : "Only used where this computer's data isn't available; lookups and searches otherwise read the real data, even in tests."} />
      </Section>
      <TryBox tool={t} variables={doc.context_variables} params={kind === "data.lookup" ? (valueVar || w.value ? [] : ["value"]) : kind === "knowledge.search" ? ["query"] : ask} />
    </>
  );
}
