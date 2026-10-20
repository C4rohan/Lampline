import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import { Plus, Trash2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/input";
import { Field } from "@/components/ui/label";

export function Section({ title, children, aside }: { title: string; children: ReactNode; aside?: ReactNode }) {
  return (
    <section className="grid gap-3 border-t px-5 py-4">
      <div className="flex items-center gap-2"><h3 className="text-sm font-semibold">{title}</h3>{aside && <span className="ml-auto">{aside}</span>}</div>
      {children}
    </section>
  );
}

/** Chips that insert {{name}} at the cursor of the field they belong to. */
export function InsertChips({ names, prefix = "", target, label = "Insert" }: { names: string[]; prefix?: string; target: React.RefObject<HTMLInputElement | HTMLTextAreaElement | null>; label?: string }) {
  if (!names.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1 text-xs text-muted-foreground">
      <span>{label}:</span>
      {names.map((n) => (
        <button key={n} type="button" title={`Insert {{${prefix}${n}}}`} onMouseDown={(e) => e.preventDefault() /* keep the field focused */}
          className="cursor-pointer rounded border border-dashed px-1.5 py-px font-mono text-[11px] hover:border-lamp hover:text-foreground"
          onClick={() => {
            const el = target.current; if (!el) return;
            const s = el.selectionStart ?? el.value.length, e = el.selectionEnd ?? s;
            el.setRangeText(`{{${prefix}${n}}}`, s, e, "end");
            el.dispatchEvent(new Event("input", { bubbles: true }));
            el.focus();
          }}>{`{{${prefix}${n}}}`}</button>
      ))}
    </div>
  );
}

/** A text field bound to the document; typing in one field is one undo step. */
export function TextField({ label, value, onChange, area, rows = 4, hint, placeholder, maxLength, mono, insert, insertPrefix, insertLabel, transform, quietInsert, invalid }: {
  label: ReactNode; value: string; onChange: (v: string) => void; area?: boolean; rows?: number; hint?: ReactNode; placeholder?: string;
  maxLength?: number; mono?: boolean; insert?: string[]; insertPrefix?: string; insertLabel?: string; transform?: (v: string) => string;
  /** Show the variable chips only while the field (or a chip) has focus: for long lists of small fields. */
  quietInsert?: boolean;
  /** Marks the field as not valid (red outline, aria-invalid). */
  invalid?: boolean;
}) {
  const id = useId();
  const ref = useRef<HTMLInputElement & HTMLTextAreaElement>(null);
  // Keep a local copy so the cursor doesn't jump; follow outside changes (undo).
  const [local, setLocal] = useState(value);
  useEffect(() => { if (value !== local && document.activeElement !== ref.current) setLocal(value); }, [value]); // eslint-disable-line
  const change = (v: string) => { const t = transform ? transform(v) : v; setLocal(t); onChange(t); };
  const common = { id, ref, value: local, placeholder, maxLength, className: cn(mono && "font-mono text-[13px]"), "aria-invalid": invalid || undefined,
    onChange: (e: React.ChangeEvent<HTMLInputElement & HTMLTextAreaElement>) => change(e.target.value) };
  return (
    <Field label={label} htmlFor={id} hint={hint}>
      <div className={cn("grid gap-2", quietInsert && "group/tf")}>
        {area ? <Textarea rows={rows} {...common} /> : <Input {...common} />}
        {insert && <div className={cn(quietInsert && "hidden group-focus-within/tf:block")}><InsertChips names={insert} prefix={insertPrefix} target={ref} label={insertLabel} /></div>}
      </div>
    </Field>
  );
}

/** Rows of key/value pairs. Rows live locally while typing, so two rows can share a key for a moment. */
export function PairsEditor({ value, onChange, keyPh, valPh, keyMono, valMono, validKey, addLabel = "Add", extra }: {
  value: Record<string, string> | undefined; onChange: (v: Record<string, string>) => void; keyPh: string; valPh: string;
  keyMono?: boolean; valMono?: boolean; validKey?: (k: string) => boolean; addLabel?: string;
  extra?: (valueRef: React.RefObject<HTMLInputElement | null>, i: number) => ReactNode;
}) {
  const [rows, setRows] = useState(() => Object.entries(value || {}).map(([k, v]) => ({ k, v: String(v) })));
  const sync = (next: { k: string; v: string }[]) => {
    setRows(next);
    const out: Record<string, string> = {};
    for (const r of next) if (r.k.trim() && !(r.k.trim() in out) && (!validKey || validKey(r.k.trim()))) out[r.k.trim()] = r.v;
    onChange(out);
  };
  return (
    <div className="grid gap-2">
      {rows.map((r, i) => <PairRow key={i} r={r} i={i} rows={rows} sync={sync} keyPh={keyPh} valPh={valPh} keyMono={keyMono} valMono={valMono} validKey={validKey} extra={extra} />)}
      <Button type="button" variant="outline" size="sm" className="justify-self-start" onClick={() => setRows([...rows, { k: "", v: "" }])}><Plus />{addLabel}</Button>
    </div>
  );
}
function PairRow({ r, i, rows, sync, keyPh, valPh, keyMono, valMono, validKey, extra }: {
  r: { k: string; v: string }; i: number; rows: { k: string; v: string }[]; sync: (n: { k: string; v: string }[]) => void;
  keyPh: string; valPh: string; keyMono?: boolean; valMono?: boolean; validKey?: (k: string) => boolean;
  extra?: (valueRef: React.RefObject<HTMLInputElement | null>, i: number) => ReactNode;
}) {
  const vref = useRef<HTMLInputElement>(null);
  const bad = !!r.k && ((validKey && !validKey(r.k.trim())) || rows.findIndex((x) => x.k.trim() === r.k.trim()) !== i);
  const set = (patch: Partial<{ k: string; v: string }>) => sync(rows.map((x, j) => (j === i ? { ...x, ...patch } : x)));
  return (
    <div className="grid gap-1">
      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] gap-2">
        <Input aria-label={keyPh} placeholder={keyPh} value={r.k} aria-invalid={bad || undefined} className={cn("h-8", keyMono && "font-mono text-[12.5px]")} onChange={(e) => set({ k: e.target.value })} />
        <Input ref={vref} aria-label={valPh} placeholder={valPh} value={r.v} className={cn("h-8", valMono && "font-mono text-[12.5px]")}
          onChange={(e) => set({ v: e.target.value })} />
        <Button type="button" variant="ghost" size="icon-sm" aria-label="Remove" onClick={() => sync(rows.filter((_, j) => j !== i))}><Trash2 /></Button>
      </div>
      {extra?.(vref, i)}
    </div>
  );
}

/** A JSON value; the last valid version stays in the document while you type. */
export function JsonField({ label, value, onChange, hint }: { label: string; value: unknown; onChange: (v: unknown | undefined) => void; hint: ReactNode }) {
  const id = useId();
  const [raw, setRaw] = useState(() => (value === undefined ? "" : JSON.stringify(value, null, 2)));
  const [bad, setBad] = useState(false);
  return (
    <Field label={label} htmlFor={id} hint={bad ? <span className="text-bad">Not valid JSON yet; the last valid version is kept.</span> : hint}>
      <Textarea id={id} rows={6} spellCheck={false} value={raw} aria-invalid={bad || undefined} className="font-mono text-[12.5px]"
        onChange={(e) => {
          setRaw(e.target.value);
          const t = e.target.value.trim();
          if (!t) { setBad(false); onChange(undefined); return; }
          try { onChange(JSON.parse(t)); setBad(false); } catch { setBad(true); }
        }} />
    </Field>
  );
}
