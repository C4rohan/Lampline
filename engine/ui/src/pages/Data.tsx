import { useEffect, useMemo, useRef, useState } from "react";
import { Database, Plus, Search, ShieldAlert, Trash2, Upload } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { useLoad } from "@/lib/app";
import { cn } from "@/lib/utils";
import { Notice, Page } from "@/components/shell";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input, Textarea } from "@/components/ui/input";
import { Field } from "@/components/ui/label";

type Collection = { name: string; description: string; key: string; count: number; fields: string[] };
type Rec = { id: string; data: Record<string, unknown>; updated: number };
type Page_ = { name: string; description: string; key: string; total: number; records: Rec[] };

const NAME = /^[a-z][a-z0-9_-]{0,62}$/;
const show = (v: unknown) => (v === null || v === undefined ? "" : typeof v === "object" ? JSON.stringify(v) : String(v));

function warn(w?: string[]) { for (const m of w || []) toast.warning("Check this data", { description: m, duration: 9000 }); }

export function DataPage() {
  const { data, reload } = useLoad(() => api<{ collections: Collection[]; folder: string }>("GET", "/v1/data"));
  const [sel, setSel] = useState<string>("");
  const [creating, setCreating] = useState(false);
  const cols = data?.collections || [];
  useEffect(() => { if (!sel && cols[0]) setSel(cols[0].name); }, [cols, sel]);
  return (
    <Page title="Data" wide
      description="Records your agents look up and save during calls, like appointments or callback requests. Kept in one file on this computer, outside agent folders, so they never end up in git."
      actions={<Button onClick={() => setCreating(true)}><Plus />New collection</Button>}>
      <Notice tone="warn" className="mb-5"><ShieldAlert className="mt-0.5 size-4 shrink-0" /><span>Use synthetic records unless this engine runs where real patient data is allowed. Imports are checked for things that look like real personal details.</span></Notice>
      {data && !cols.length ? (
        <Card className="grid place-items-center gap-3 px-6 py-14 text-center">
          <Database className="size-8 text-muted-foreground" />
          <p className="font-medium">No collections yet</p>
          <p className="max-w-md text-sm text-muted-foreground">A collection is a list of records, like <code>appointments</code> with a confirmation number, name, day and status. Create one, then import a CSV or add records.</p>
          <Button onClick={() => setCreating(true)}><Plus />New collection</Button>
        </Card>
      ) : (
        <div className="grid items-start gap-5 lg:grid-cols-[240px_minmax(0,1fr)]">
          <nav aria-label="Collections" className="flex gap-1.5 overflow-x-auto lg:grid lg:overflow-visible">
            {cols.map((c) => (
              <button key={c.name} type="button" onClick={() => setSel(c.name)} aria-current={sel === c.name || undefined}
                className={cn("flex shrink-0 items-center gap-2 rounded-lg border px-3 py-2 text-left text-sm transition-colors",
                  sel === c.name ? "border-lamp bg-lamp-soft" : "hover:bg-accent")}>
                <Database className="size-4 text-muted-foreground" />
                <span className="truncate font-mono">{c.name}</span>
                <span className="ml-auto text-xs text-muted-foreground">{c.count}</span>
              </button>
            ))}
          </nav>
          {sel && <CollectionView key={sel} name={sel} info={cols.find((c) => c.name === sel)} onChanged={reload}
                                   onDeleted={() => { setSel(""); reload(); }} />}
        </div>
      )}
      {data?.folder && <p className="mt-6 text-xs text-muted-foreground">Stored in <code>{data.folder}</code> (set VPE_DATA_DIR to move it).</p>}
      <NewCollection open={creating} onOpenChange={setCreating} onDone={(n) => { setSel(n); reload(); }} />
    </Page>
  );
}

function NewCollection({ open, onOpenChange, onDone }: { open: boolean; onOpenChange: (v: boolean) => void; onDone: (name: string) => void }) {
  const [name, setName] = useState(""), [desc, setDesc] = useState(""), [key, setKey] = useState(""), [err, setErr] = useState("");
  async function save(e: React.FormEvent) {
    e.preventDefault();
    try {
      await api("POST", "/v1/data", { name, description: desc, key });
      onOpenChange(false); onDone(name); setName(""); setDesc(""); setKey("");
    } catch (x) { setErr((x as Error).message); }
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={save} className="grid gap-4">
          <DialogHeader><DialogTitle>New collection</DialogTitle><DialogDescription>A named list of records your agents can look up and add to.</DialogDescription></DialogHeader>
          <Field label="Name" htmlFor="nc-name" hint="Lowercase, like appointments or callback-requests.">
            <Input id="nc-name" className="font-mono" value={name} onChange={(e) => { setErr(""); setName(e.target.value.toLowerCase().replace(/\s+/g, "-")); }} autoFocus />
          </Field>
          <Field label="What's in it (optional)" htmlFor="nc-desc"><Input id="nc-desc" value={desc} onChange={(e) => setDesc(e.target.value)} /></Field>
          <Field label="Key field (optional)" htmlFor="nc-key" hint="A field that identifies a record, like confirmation. Saving a record with a key that exists updates it.">
            <Input id="nc-key" className="font-mono" value={key} onChange={(e) => setKey(e.target.value.trim())} placeholder="e.g. confirmation" />
          </Field>
          {err && <p className="text-sm text-bad">{err}</p>}
          <DialogFooter><Button type="submit" disabled={!NAME.test(name)}>Create</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function CollectionView({ name, info, onChanged, onDeleted }: { name: string; info?: Collection; onChanged: () => void; onDeleted: () => void }) {
  const [search, setSearch] = useState("");
  const [q, setQ] = useState("");
  const { data, reload } = useLoad(() => api<Page_>("GET", `/v1/data/${name}?limit=200&search=${encodeURIComponent(q)}`), [name, q]);
  const [edit, setEdit] = useState<Rec | "new" | null>(null);
  const file = useRef<HTMLInputElement>(null);
  const cols = useMemo(() => {
    const seen: string[] = [];
    for (const r of data?.records || []) for (const k of Object.keys(r.data)) if (!seen.includes(k)) seen.push(k);
    if (info?.key && seen.includes(info.key)) seen.splice(seen.indexOf(info.key), 1), seen.unshift(info.key);
    return seen.slice(0, 7);
  }, [data, info]);
  useEffect(() => { const t = setTimeout(() => setQ(search), 250); return () => clearTimeout(t); }, [search]);

  async function importFile(f: File | undefined) {
    if (!f) return;
    try {
      const r = await api<{ imported: number; warnings: string[] }>("POST", `/v1/data/${name}/import?filename=${encodeURIComponent(f.name)}`, f);
      toast.success(`Imported ${r.imported} record${r.imported === 1 ? "" : "s"}`); warn(r.warnings); reload(); onChanged();
    } catch (e) { toast.error("Couldn't import that file", { description: (e as Error).message }); }
    if (file.current) file.current.value = "";
  }
  async function drop() {
    if (!confirm(`Delete the collection ${name} and its ${info?.count ?? 0} records? Agents that use it will find nothing.`)) return;
    await api("DELETE", `/v1/data/${name}`); onDeleted();
  }
  return (
    <Card className="min-w-0 overflow-hidden">
      <div className="flex flex-wrap items-start gap-3 border-b px-4 py-3.5">
        <div className="grid min-w-0 gap-0.5">
          <h2 className="font-mono font-semibold">{name}</h2>
          <p className="text-[13px] text-muted-foreground">{info?.description || "No description"}{info?.key && <> · key <code>{info.key}</code></>}</p>
        </div>
        <div className="ml-auto flex flex-wrap gap-2">
          <Button variant="outline" size="sm" onClick={() => file.current?.click()}><Upload />Import CSV or JSON</Button>
          <input ref={file} type="file" accept=".csv,.json,text/csv,application/json" className="hidden" aria-label="CSV or JSON file" onChange={(e) => importFile(e.target.files?.[0])} />
          <Button size="sm" onClick={() => setEdit("new")}><Plus />Add record</Button>
          <Button variant="ghost" size="icon-sm" aria-label="Delete collection" onClick={drop}><Trash2 /></Button>
        </div>
      </div>
      <div className="flex items-center gap-2 border-b px-4 py-2.5">
        <Search className="size-4 text-muted-foreground" />
        <Input aria-label="Search records" className="h-8 border-0 shadow-none focus-visible:ring-0" placeholder="Search records" value={search} onChange={(e) => setSearch(e.target.value)} />
        <span className="shrink-0 text-xs text-muted-foreground">{data ? `${data.total} record${data.total === 1 ? "" : "s"}` : ""}</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[13px]">
          <thead className="bg-muted/50 text-left text-xs text-muted-foreground">
            <tr>{cols.map((c) => <th key={c} className="px-4 py-2 font-medium font-mono">{c}</th>)}<th className="px-4 py-2 font-medium">id</th></tr>
          </thead>
          <tbody>
            {(data?.records || []).map((r) => (
              <tr key={r.id} className="cursor-pointer border-t hover:bg-accent/60" onClick={() => setEdit(r)} tabIndex={0}
                  onKeyDown={(e) => { if (e.key === "Enter") setEdit(r); }}>
                {cols.map((c) => <td key={c} className="max-w-[260px] truncate px-4 py-2">{show(r.data[c])}</td>)}
                <td className="px-4 py-2 font-mono text-xs text-muted-foreground">{r.id}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && !data.records.length && <p className="px-4 py-10 text-center text-sm text-muted-foreground">{q ? "Nothing matches." : "No records yet. Import a CSV or add one."}</p>}
      </div>
      <RecordDialog name={name} rec={edit} fields={info?.fields || cols} onClose={() => setEdit(null)} onSaved={() => { setEdit(null); reload(); onChanged(); }} />
    </Card>
  );
}

function RecordDialog({ name, rec, fields, onClose, onSaved }: { name: string; rec: Rec | "new" | null; fields: string[]; onClose: () => void; onSaved: () => void }) {
  const [textValue, setText] = useState("");
  const [err, setErr] = useState("");
  useEffect(() => {
    setErr("");
    if (rec === "new") setText(JSON.stringify(Object.fromEntries(fields.slice(0, 6).map((f) => [f, ""])), null, 2));
    else if (rec) setText(JSON.stringify(rec.data, null, 2));
  }, [rec, fields]);
  async function save() {
    let parsed: unknown;
    try { parsed = JSON.parse(textValue); } catch { setErr("That isn't valid JSON."); return; }
    try {
      const r = rec === "new" ? await api<{ warnings: string[] }>("POST", `/v1/data/${name}/records`, { data: parsed })
        : await api<{ warnings: string[] }>("PUT", `/v1/data/${name}/records/${encodeURIComponent((rec as Rec).id)}`, { data: parsed });
      warn(r.warnings); onSaved();
    } catch (e) { setErr((e as Error).message); }
  }
  async function remove() {
    await api("DELETE", `/v1/data/${name}/records/${encodeURIComponent((rec as Rec).id)}`); onSaved();
  }
  return (
    <Dialog open={!!rec} onOpenChange={(v) => { if (!v) onClose(); }}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{rec === "new" ? "New record" : "Record"}</DialogTitle>
          <DialogDescription>{rec && rec !== "new" ? <>id <code>{rec.id}</code> in {name}</> : `Adds to ${name}.`}</DialogDescription>
        </DialogHeader>
        <Field label="Fields (JSON)" htmlFor="rec-json">
          <Textarea id="rec-json" rows={12} className="font-mono text-[12.5px]" value={textValue} onChange={(e) => { setErr(""); setText(e.target.value); }} />
        </Field>
        {err && <p className="text-sm text-bad">{err}</p>}
        <DialogFooter className="gap-2">
          {rec && rec !== "new" && <Button variant="ghost" className="mr-auto text-bad" onClick={remove}><Trash2 />Delete</Button>}
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button onClick={save}>Save</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
