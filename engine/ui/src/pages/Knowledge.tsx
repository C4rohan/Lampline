import { useEffect, useRef, useState } from "react";
import { BookOpen, Download, FileText, Plus, Search, Sparkles, Trash2, Upload } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { useLoad } from "@/lib/app";
import { cn } from "@/lib/utils";
import { Notice, Page } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Field } from "@/components/ui/label";

type Base = { name: string; description: string; docs: number; passages: number; embedded: number };
type Embeddings = { mode: string; ready: boolean; model: string | null; kinds: string[];
  download: null | { state: string; message: string; done_bytes: number; total_bytes: number; ready: boolean; size_mb: number } };
type Doc = { id: number; title: string; chars: number; added: number; passages: number };
type Hit = { text: string; source: string; score: number; similarity?: number };

const NAME = /^[a-z][a-z0-9_-]{0,62}$/;

export function KnowledgePage() {
  const { data, reload, setData } = useLoad(() => api<{ bases: Base[]; embeddings: Embeddings }>("GET", "/v1/knowledge"));
  const [sel, setSel] = useState("");
  const [creating, setCreating] = useState(false);
  const bases = data?.bases || [];
  useEffect(() => { if (!sel && bases[0]) setSel(bases[0].name); }, [bases, sel]);
  const downloading = data?.embeddings.download?.state === "downloading";
  useEffect(() => {
    if (!downloading) return;
    const t = setInterval(async () => {
      const e = await api<Embeddings>("GET", "/v1/embeddings");
      setData((d) => (d ? { ...d, embeddings: e } : d));
      if (e.download?.state !== "downloading") {
        for (const b of bases) await api("POST", `/v1/knowledge/${b.name}/reindex`).catch(() => {});
        reload();
      }
    }, 1000);
    return () => clearInterval(t);
  }, [downloading]); // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <Page title="Knowledge" wide
      description="Documents your agents can search for answers: FAQs, policies, price lists. Searched on this computer, by keywords and by meaning; nothing is sent anywhere."
      actions={<Button onClick={() => setCreating(true)}><Plus />New knowledge base</Button>}>
      {data && <SearchMode e={data.embeddings} onDownload={async () => { setData({ ...data, embeddings: await api<Embeddings>("POST", "/v1/embeddings/download") }); }} />}
      {data && !bases.length ? (
        <Card className="grid place-items-center gap-3 px-6 py-14 text-center">
          <BookOpen className="size-8 text-muted-foreground" />
          <p className="font-medium">No knowledge bases yet</p>
          <p className="max-w-md text-sm text-muted-foreground">Create one, upload documents, then give an agent a “Search knowledge” tool that uses it.</p>
          <Button onClick={() => setCreating(true)}><Plus />New knowledge base</Button>
        </Card>
      ) : (
        <div className="grid items-start gap-5 lg:grid-cols-[240px_minmax(0,1fr)]">
          <nav aria-label="Knowledge bases" className="flex gap-1.5 overflow-x-auto lg:grid lg:overflow-visible">
            {bases.map((b) => (
              <button key={b.name} type="button" onClick={() => setSel(b.name)} aria-current={sel === b.name || undefined}
                className={cn("flex shrink-0 items-center gap-2 rounded-lg border px-3 py-2 text-left text-sm transition-colors",
                  sel === b.name ? "border-lamp bg-lamp-soft" : "hover:bg-accent")}>
                <BookOpen className="size-4 text-muted-foreground" />
                <span className="truncate font-mono">{b.name}</span>
                <span className="ml-auto text-xs text-muted-foreground">{b.docs}</span>
              </button>
            ))}
          </nav>
          {sel && data && <BaseView key={sel} name={sel} info={bases.find((b) => b.name === sel)} e={data.embeddings}
                                   onChanged={reload} onDeleted={() => { setSel(""); reload(); }} />}
        </div>
      )}
      <NewBase open={creating} onOpenChange={setCreating} onDone={(n) => { setSel(n); reload(); }} />
    </Page>
  );
}

function SearchMode({ e, onDownload }: { e: Embeddings; onDownload: () => void }) {
  const d = e.download;
  if (e.mode === "off") return <Notice className="mb-5"><Search className="mt-0.5 size-4 shrink-0" /><span>Keyword search only. Turn on meaning search in Integrations → Knowledge search.</span></Notice>;
  if (e.ready) return <Notice className="mb-5"><Sparkles className="mt-0.5 size-4 shrink-0" /><span>Searching by keywords and by meaning ({e.model}).</span></Notice>;
  if (e.mode === "ollama") return <Notice tone="warn" className="mb-5"><Search className="mt-0.5 size-4 shrink-0" /><span>Meaning search needs the embeddings server address and model (Integrations → Knowledge search). Keyword search works now.</span></Notice>;
  return (
    <Notice className="mb-5">
      <Sparkles className="mt-0.5 size-4 shrink-0" />
      <div className="grid flex-1 gap-2">
        <span>Keyword search works now. For matches by meaning (“where do I leave my car” finding the parking page), download a small model that runs on this computer ({d?.size_mb ?? 90} MB, once).</span>
        {d?.state === "downloading" ? (
          <div className="grid gap-1" role="status">
            <div className="h-1.5 overflow-hidden rounded-full bg-muted"><div className="h-full bg-lamp transition-all" style={{ width: `${Math.min(100, (100 * d.done_bytes) / Math.max(1, d.total_bytes))}%` }} /></div>
            <span className="text-xs">{d.message}</span>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" variant="outline" onClick={onDownload}><Download />Download the model</Button>
            {d?.state === "error" && <span className="text-xs text-bad">{d.message}</span>}
          </div>
        )}
      </div>
    </Notice>
  );
}

function NewBase({ open, onOpenChange, onDone }: { open: boolean; onOpenChange: (v: boolean) => void; onDone: (name: string) => void }) {
  const [name, setName] = useState(""), [desc, setDesc] = useState(""), [err, setErr] = useState("");
  async function save(e: React.FormEvent) {
    e.preventDefault();
    try { await api("POST", "/v1/knowledge", { name, description: desc }); onOpenChange(false); onDone(name); setName(""); setDesc(""); }
    catch (x) { setErr((x as Error).message); }
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={save} className="grid gap-4">
          <DialogHeader><DialogTitle>New knowledge base</DialogTitle><DialogDescription>A set of documents an agent searches together.</DialogDescription></DialogHeader>
          <Field label="Name" htmlFor="nb-name" hint="Lowercase, like clinic-faq.">
            <Input id="nb-name" className="font-mono" value={name} onChange={(e) => { setErr(""); setName(e.target.value.toLowerCase().replace(/\s+/g, "-")); }} autoFocus />
          </Field>
          <Field label="What's in it (optional)" htmlFor="nb-desc"><Input id="nb-desc" value={desc} onChange={(e) => setDesc(e.target.value)} placeholder="e.g. Hours, parking, insurance" /></Field>
          {err && <p className="text-sm text-bad">{err}</p>}
          <DialogFooter><Button type="submit" disabled={!NAME.test(name)}>Create</Button></DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function BaseView({ name, info, e, onChanged, onDeleted }: { name: string; info?: Base; e: Embeddings; onChanged: () => void; onDeleted: () => void }) {
  const { data, reload } = useLoad(() => api<{ documents: Doc[] }>("GET", `/v1/knowledge/${name}`), [name]);
  const file = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState("");
  const [over, setOver] = useState(false);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState<Hit[] | null>(null);
  async function upload(files: FileList | File[] | null | undefined) {
    for (const f of Array.from(files || [])) {
      setBusy(`Reading ${f.name}…`);
      try {
        const r = await api<{ document: { passages: number; embedded: number }; warnings: string[] }>(
          "POST", `/v1/knowledge/${name}/documents?filename=${encodeURIComponent(f.name)}`, f);
        toast.success(`Added ${f.name}`, { description: `${r.document.passages} passage${r.document.passages === 1 ? "" : "s"}` });
        for (const w of r.warnings) toast.warning("Check this document", { description: w, duration: 9000 });
      } catch (x) { toast.error(`Couldn't add ${f.name}`, { description: (x as Error).message }); }
    }
    setBusy(""); reload(); onChanged();
    if (file.current) file.current.value = "";
  }
  async function search(ev: React.FormEvent) {
    ev.preventDefault();
    if (!q.trim()) return;
    setHits((await api<{ results: Hit[] }>("POST", `/v1/knowledge/${name}/search`, { query: q, results: 3 })).results);
  }
  async function drop() {
    if (!confirm(`Delete the knowledge base ${name} and its documents?`)) return;
    await api("DELETE", `/v1/knowledge/${name}`); onDeleted();
  }
  const behind = info && e.ready && info.embedded < info.passages;
  return (
    <div className="grid min-w-0 gap-5">
      <Card className="min-w-0 overflow-hidden">
        <div className="flex flex-wrap items-start gap-3 border-b px-4 py-3.5">
          <div className="grid min-w-0 gap-0.5">
            <h2 className="font-mono font-semibold">{name}</h2>
            <p className="text-[13px] text-muted-foreground">{info?.description || "No description"} · {info?.passages ?? 0} passages</p>
          </div>
          <div className="ml-auto flex flex-wrap gap-2">
            {behind && <Button variant="outline" size="sm" onClick={async () => { setBusy("Indexing…"); await api("POST", `/v1/knowledge/${name}/reindex`); setBusy(""); onChanged(); }}>
              <Sparkles />Index {info!.passages - info!.embedded} for meaning search</Button>}
            <Button variant="ghost" size="icon-sm" aria-label="Delete knowledge base" onClick={drop}><Trash2 /></Button>
          </div>
        </div>
        <button type="button" onClick={() => file.current?.click()}
          onDragOver={(ev) => { ev.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
          onDrop={(ev) => { ev.preventDefault(); setOver(false); upload(ev.dataTransfer.files); }}
          className={cn("m-4 flex w-[calc(100%-2rem)] flex-col items-center gap-1.5 rounded-xl border-2 border-dashed px-4 py-6 text-center transition-colors",
            "hover:border-primary/60 hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring", over && "border-primary bg-primary/5")}>
          <Upload className="size-6 text-muted-foreground" />
          <span className="text-sm font-medium">{busy || "Drop documents here, or click to choose"}</span>
          <span className="text-xs text-muted-foreground">{e.kinds.join(" ")} · up to 20 MB each</span>
        </button>
        <input ref={file} type="file" multiple accept={e.kinds.join(",")} className="hidden" aria-label="Documents" onChange={(ev) => upload(ev.target.files)} />
        <ul className="divide-y border-t">
          {(data?.documents || []).map((d) => (
            <li key={d.id} className="flex items-center gap-3 px-4 py-2.5 text-[13px]">
              <FileText className="size-4 shrink-0 text-muted-foreground" />
              <span className="min-w-0 truncate">{d.title}</span>
              <span className="ml-auto shrink-0 text-xs text-muted-foreground">{d.passages} passage{d.passages === 1 ? "" : "s"} · {new Date(d.added * 1000).toLocaleDateString()}</span>
              <Button variant="ghost" size="icon-sm" aria-label={`Remove ${d.title}`}
                onClick={async () => { await api("DELETE", `/v1/knowledge/${name}/documents/${d.id}`); reload(); onChanged(); }}><Trash2 /></Button>
            </li>
          ))}
          {data && !data.documents.length && <li className="px-4 py-6 text-center text-sm text-muted-foreground">No documents yet.</li>}
        </ul>
      </Card>
      <Card className="grid gap-3 p-4">
        <form onSubmit={search} className="flex gap-2">
          <Input aria-label="Try a search" placeholder="Try a question a caller might ask" value={q} onChange={(ev) => setQ(ev.target.value)} />
          <Button type="submit" variant="outline"><Search />Search</Button>
        </form>
        {hits && (hits.length ? hits.map((h, i) => (
          <div key={i} className="grid gap-1 rounded-lg border bg-muted/40 p-3 text-[13px]">
            <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
              <Badge variant="outline">{h.source}</Badge>
              {h.similarity !== undefined && <span>meaning {Math.round(h.similarity * 100)}%</span>}
            </div>
            <p className="whitespace-pre-wrap leading-relaxed">{h.text}</p>
          </div>
        )) : <p className="text-sm text-muted-foreground">Nothing matches, so an agent would say it doesn't know.</p>)}
      </Card>
    </div>
  );
}
