import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { ChevronDown, Info } from "lucide-react";
import { toast } from "sonner";
import { api, type Settings } from "@/lib/api";
import { useApp } from "@/lib/app";
import { cn } from "@/lib/utils";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input, NativeSelect } from "@/components/ui/input";
import { Field } from "@/components/ui/label";

/** fresh: start empty instead of showing the saved value (a setting shared between providers, like the model). */
export type FieldOpts = { label?: string; hint?: ReactNode; placeholder?: string; labels?: Record<string, string>; fresh?: boolean };
export type Group =
  | { field: string; opts?: FieldOpts }
  | { row: { field: string; opts?: FieldOpts }[] }
  | { node: ReactNode }
  | { details: string; items: Group[] };
export const f = (field: string, opts?: FieldOpts): Group => ({ field, opts });
export const row = (...items: { field: string; opts?: FieldOpts }[]): Group => ({ row: items });
export const node = (n: ReactNode): Group => ({ node: n });

const SOURCE: Record<string, string> = { saved: "Saved here", env: "From .env" };

export type Ctx = { settings: Settings; changes: Record<string, string>; set: (k: string, v: string | undefined) => void; id: string; labels?: Record<string, string> };

export function SettingField({ ctx, name, opts = {} }: { ctx: Ctx; name: string; opts?: FieldOpts }) {
  const spec = ctx.settings.fields[name];
  const id = `${ctx.id}-${name}`;
  const [armed, setArmed] = useState(false);
  if (!spec) return null;
  const src = spec.source && spec.source !== "default" ? <span className="text-[11.5px] text-muted-foreground">{SOURCE[spec.source] || spec.source}</span> : null;
  if (spec.choices) {
    const labels = opts.labels || ctx.labels || {};
    return (
      <Field label={opts.label || spec.label} htmlFor={id} hint={opts.hint} aside={src}>
        <NativeSelect id={id} value={ctx.changes[name] ?? spec.value ?? ""} onChange={(e) => ctx.set(name, e.target.value)}>
          {spec.choices.map((c) => <option key={c} value={c}>{labels[c] || c}</option>)}
        </NativeSelect>
      </Field>
    );
  }
  if ("set" in spec) {   // a key: never shown again, only replaced or removed
    return (
      <Field label={opts.label || spec.label} htmlFor={id} hint={opts.hint}
        aside={spec.set ? <Badge variant="ok">{spec.source === "env" ? "Set in .env" : "Saved"}</Badge> : <Badge variant="outline">Not set</Badge>}>
        <div className="flex gap-2">
          <Input id={id} type="password" autoComplete="off" value={ctx.changes[name] || ""}
            placeholder={spec.set ? `Saved (${spec.hint}). Type to replace.` : opts.placeholder || "Paste the key"}
            onChange={(e) => ctx.set(name, e.target.value || undefined)} />
          {spec.set && spec.source === "saved" && (
            <Button type="button" variant="ghost" className={cn(armed && "text-bad")}
              onClick={() => { if (armed) ctx.set(name, ""); setArmed(!armed); }}>
              {armed ? (ctx.changes[name] === "" ? "Removes on save" : "Click to confirm") : "Remove"}
            </Button>
          )}
        </div>
      </Field>
    );
  }
  return (
    <Field label={opts.label || spec.label} htmlFor={id} hint={opts.hint} aside={src}>
      <Input id={id} autoComplete="off" placeholder={opts.placeholder} value={ctx.changes[name] ?? (opts.fresh ? "" : spec.value ?? "")}
        onChange={(e) => ctx.set(name, e.target.value)} />
    </Field>
  );
}

export function Groups({ ctx, groups }: { ctx: Ctx; groups: Group[] }) {
  return (
    <>
      {groups.map((g, i) => {
        if ("field" in g) return <SettingField key={i} ctx={ctx} name={g.field} opts={g.opts} />;
        if ("row" in g) return <div key={i} className="grid items-start gap-4 sm:grid-cols-2">{g.row.map((x) => <SettingField key={x.field} ctx={ctx} name={x.field} opts={x.opts} />)}</div>;
        if ("node" in g) return <div key={i}>{g.node}</div>;
        return (
          <details key={i} className="group rounded-lg border">
            <summary className="flex cursor-pointer list-none items-center gap-2 px-4 py-3 text-sm font-medium [&::-webkit-details-marker]:hidden">
              <ChevronDown className="size-4 text-muted-foreground transition-transform group-open:rotate-180" />{g.details}
            </summary>
            <div className="grid gap-4 border-t p-4"><Groups ctx={ctx} groups={g.items} /></div>
          </details>
        );
      })}
    </>
  );
}

export function SettingsCard({ settings, onSaved, icon: Icon, title, lead, chooser, labels, groups, check, ready, extra }: {
  settings: Settings; onSaved: (s: Settings) => void; icon: typeof Info; title: string; lead: ReactNode; chooser?: string;
  labels?: Record<string, string>; groups: (choice: string) => Group[]; check?: string; ready?: (s: Record<string, boolean>) => boolean; extra?: ReactNode;
}) {
  const id = useId().replace(/:/g, "");
  const { refresh } = useApp();
  const [changes, setChanges] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<"" | "save" | "test">("");
  const [result, setResult] = useState<{ ok: boolean; text: string } | null>(null);
  const choice = chooser ? (changes[chooser] ?? settings.fields[chooser]?.value ?? "") : "";
  const set = (k: string, v: string | undefined) => setChanges((c) => { const n = { ...c }; if (v === undefined) delete n[k]; else n[k] = v; return n; });
  const ctx: Ctx = { settings, changes, set, id, labels };
  const isReady = ready ? ready(settings.status) : null;

  async function save(): Promise<boolean> {
    if (!Object.keys(changes).length) { setResult({ ok: true, text: "Nothing to save" }); return true; }
    setBusy("save"); setResult(null);
    try {
      const s = await api<Settings>("PUT", "/v1/settings", changes);
      setChanges({}); onSaved(s); await refresh();
      setResult({ ok: true, text: "Saved" }); toast.success(`${title} saved`);
      return true;
    } catch (e) { setResult({ ok: false, text: (e as Error).message }); return false; } finally { setBusy(""); }
  }
  async function test() {
    if (Object.keys(changes).length && !(await save())) return;
    setBusy("test"); setResult(null);
    try {
      const r = await api<{ ok: boolean; message: string }>("POST", `/v1/settings/check/${check}`);
      setResult({ ok: r.ok, text: r.ok ? "Connected" : r.message });
    } catch (e) { setResult({ ok: false, text: (e as Error).message }); } finally { setBusy(""); }
  }

  return (
    <Card id={title.toLowerCase().split(" ")[0]} className="flex flex-col">
      <div className="flex items-start gap-3 px-6 pt-6">
        <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground"><Icon className="size-[18px]" /></span>
        <div className="grid min-w-0 flex-1 gap-1">
          <h2 className="font-semibold leading-tight">{title}</h2>
          <p className="text-sm text-muted-foreground">{lead}</p>
        </div>
        {isReady !== null && <Badge variant={isReady ? "ok" : "warn"}>{isReady ? "Ready" : "Needs setup"}</Badge>}
      </div>
      <div className="grid flex-1 content-start gap-4 px-6 py-5">
        {chooser && <SettingField ctx={ctx} name={chooser} opts={{ label: "Provider" }} />}
        <Groups ctx={ctx} groups={groups(choice)} />
        {extra}
      </div>
      <div className="flex flex-wrap items-center gap-3 border-t px-6 py-4">
        <Button onClick={save} disabled={!!busy}>{busy === "save" ? "Saving…" : "Save"}</Button>
        {check && <Button variant="outline" onClick={test} disabled={!!busy}>{busy === "test" ? "Testing…" : "Test connection"}</Button>}
        {result && <span className={cn("text-sm", result.ok ? "text-ok" : "text-bad")} role="status">{result.text}</span>}
        {Object.keys(changes).length > 0 && !result && <span className="text-sm text-warn">Unsaved changes</span>}
      </div>
    </Card>
  );
}

/** Poll an endpoint while the component is on screen; faster while busy. */
export function usePoll<T>(path: string, busy: (t: T) => boolean, slow = 10000, fast = 1000) {
  const [data, setData] = useState<T | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const [n, setN] = useState(0);
  useEffect(() => {
    let live = true;
    const tick = async () => {
      try { const d = await api<T>("GET", path); if (!live) return; setData(d); timer.current = window.setTimeout(tick, busy(d) ? fast : slow); }
      catch { if (live) timer.current = window.setTimeout(tick, slow); }
    };
    tick();
    return () => { live = false; window.clearTimeout(timer.current); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, n]);
  return { data, setData, kick: () => setN((x) => x + 1) };
}
