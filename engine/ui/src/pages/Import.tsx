import { useRef, useState } from "react";
import { CheckCircle2, FileArchive, TriangleAlert, Upload } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { go } from "@/lib/route";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input, NativeSelect } from "@/components/ui/input";
import { Field } from "@/components/ui/label";

type Imported = { slug: string; name: string; import: { source: string; notes: string[]; counts: Record<string, number> } };

const SOURCES = [
  { value: "auto", label: "Work it out from the file" },
  { value: "dfcx", label: "Dialogflow CX (JSON package)" },
  { value: "ces", label: "CX Agent Studio (app export)" },
];

/** Bring an agent over from Dialogflow CX or CX Agent Studio. */
export function ImportBox({ onDone }: { onDone?: () => void }) {
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState("auto");
  const [name, setName] = useState("");
  const [over, setOver] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [done, setDone] = useState<Imported | null>(null);

  function pick(f: File | undefined | null) {
    setError(""); setDone(null);
    if (!f) return;
    if (!/\.zip$/i.test(f.name)) { setError("Choose the exported .zip file."); return; }
    setFile(f);
  }

  async function run(e: React.FormEvent) {
    e.preventDefault();
    if (!file) return;
    setBusy(true); setError("");
    try {
      const q = new URLSearchParams({ source, name: name.trim(), filename: file.name });
      const a = await api<Imported>("POST", `/v1/agents/import?${q}`, file);
      setDone(a);
      toast.success(`Imported ${a.name}`, { description: `${a.import.notes.length} thing(s) to review.` });
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }

  if (done) {
    const counts = Object.entries(done.import.counts).filter(([, v]) => v).map(([k, v]) => `${v} ${v === 1 ? k.replace(/s$/, "") : k}`).join(" · ");
    return (
      <div className="grid gap-3" data-testid="import-result">
        <div className="flex items-start gap-2.5">
          <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-ok" />
          <div className="min-w-0">
            <p className="font-medium">{done.name}</p>
            <p className="text-sm text-muted-foreground">From {done.import.source}{counts && ` · ${counts}`}</p>
          </div>
        </div>
        {done.import.notes.length > 0 && (
          <div className="rounded-lg border bg-muted/40 p-3">
            <p className="mb-1.5 flex items-center gap-1.5 text-sm font-medium"><TriangleAlert className="size-4 text-warn" />Review before taking calls</p>
            <ul className="max-h-48 list-disc space-y-1 overflow-y-auto pl-5 text-sm text-muted-foreground">
              {done.import.notes.map((n) => <li key={n}>{n}</li>)}
            </ul>
            <p className="mt-2 text-xs text-muted-foreground">Saved as import-report.md in the agent's folder.</p>
          </div>
        )}
        <div className="flex flex-wrap justify-end gap-2">
          <Button variant="outline" onClick={() => { setDone(null); setFile(null); setName(""); }}>Import another</Button>
          <Button onClick={() => { onDone?.(); go(`#/build/${encodeURIComponent(done.slug)}`); }}>Open in builder</Button>
        </div>
      </div>
    );
  }

  return (
    <form className="grid gap-4" onSubmit={run}>
      <button
        type="button"
        onClick={() => input.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setOver(true); }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files?.[0]); }}
        className={cn("flex flex-col items-center gap-2 rounded-xl border-2 border-dashed px-4 py-7 text-center transition-colors",
          "hover:border-primary/60 hover:bg-muted/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
          over && "border-primary bg-primary/5")}
      >
        {file ? <FileArchive className="size-7 text-primary" /> : <Upload className="size-7 text-muted-foreground" />}
        <span className="text-sm font-medium">{file ? file.name : "Drop the exported .zip here, or click to choose it"}</span>
        <span className="text-xs text-muted-foreground">
          {file ? `${(file.size / 1024).toFixed(0)} KB` : "Dialogflow CX: Agent settings → Export (JSON package). CX Agent Studio: Export app (JSON or YAML)."}
        </span>
      </button>
      <input ref={input} type="file" accept=".zip,application/zip" className="hidden" aria-label="Exported zip file"
             onChange={(e) => pick(e.target.files?.[0])} />
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="It came from" htmlFor="imp-source">
          <NativeSelect id="imp-source" value={source} onChange={(e) => setSource(e.target.value)}>
            {SOURCES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
          </NativeSelect>
        </Field>
        <Field label="Name (optional)" htmlFor="imp-name">
          <Input id="imp-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Uses the export's name" maxLength={80} />
        </Field>
      </div>
      <p className="text-xs text-muted-foreground">
        Converted on this computer. Code in the export (Python tools, callbacks) is never run; those become tools with example answers for you to connect.
      </p>
      {error && <p className="text-sm text-bad">{error}</p>}
      <div className="flex justify-end">
        <Button type="submit" disabled={!file || busy}>{busy ? "Converting…" : "Import"}</Button>
      </div>
    </form>
  );
}
