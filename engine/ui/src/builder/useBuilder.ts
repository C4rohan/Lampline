import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { api, type Finding } from "@/lib/api";
import type { Doc, Selection } from "./model";
import { selExists } from "./model";

type Loaded = Doc & { slug: string; findings: Finding[] };

/** The agent being edited: one document, undo/redo, live checks and saving. */
export function useBuilder(slug: string) {
  const [doc, setDoc] = useState<Doc | null>(null);
  const [error, setError] = useState("");
  const [findings, setFindings] = useState<Finding[]>([]);
  const [checkError, setCheckError] = useState("");
  const [sel, setSel] = useState<Selection>(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState("");
  const [saved, setSaved] = useState("");
  const undo = useRef<string[]>([]);
  const redo = useRef<string[]>([]);
  const editKey = useRef<string | null>(null);
  const docRef = useRef<Doc | null>(null);
  const [, bump] = useState(0);
  const [rev, setRev] = useState(0);          // bumps on undo/redo so editors re-read the document
  docRef.current = doc;

  useEffect(() => {
    let live = true;
    api<Loaded>("GET", `/v1/agents/${encodeURIComponent(slug)}`).then((a) => {
      if (!live) return;
      const d: Doc = {
        name: a.name,
        definition: { ...a.definition,
          nodes: (a.definition.nodes || []).map((n) => ({ ...n, id: String(n.id), position: n.position || { x: 0, y: 0 }, data: n.data || {} })),
          edges: (a.definition.edges || []).map((e) => ({ ...e, id: String(e.id), source: String(e.source), target: String(e.target), data: e.data || {} })) },
        context_variables: Object.fromEntries(Object.entries(a.context_variables || {}).map(([k, v]) => [k, v == null ? "" : String(v)])),
        tools: Array.isArray(a.tools) ? a.tools : [],
        triggers: Array.isArray(a.triggers) ? a.triggers : [],
      };
      setDoc(d); setFindings(a.findings || []); setSaved(JSON.stringify(d));
    }).catch((e) => live && setError(e.message));
    return () => { live = false; };
  }, [slug]);

  const snap = () => JSON.stringify(docRef.current);
  /** Change the document. key groups typing in one field into one undo step. */
  const update = useCallback((fn: (d: Doc) => void, opts: { key?: string; noUndo?: boolean } = {}) => {
    const cur = docRef.current;
    if (!cur) return;
    if (!opts.noUndo && (!opts.key || editKey.current !== opts.key)) {
      undo.current.push(JSON.stringify(cur));
      if (undo.current.length > 100) undo.current.shift();
      redo.current = [];
    }
    editKey.current = opts.key || null;
    const next: Doc = structuredClone(cur);
    fn(next);
    docRef.current = next;
    setDoc(next);
  }, []);
  /** Take an undo snapshot now (before a drag that changes positions without update()). */
  const checkpoint = useCallback(() => {
    if (!docRef.current) return;
    undo.current.push(JSON.stringify(docRef.current)); redo.current = []; editKey.current = null; bump((x) => x + 1);
  }, []);
  const restore = (s: string) => { const d = JSON.parse(s) as Doc; docRef.current = d; setDoc(d); editKey.current = null; setRev((r) => r + 1); setSel((x) => (selExists(d, x) ? x : null)); };
  const doUndo = useCallback(() => { if (undo.current.length) { redo.current.push(snap()); restore(undo.current.pop()!); } }, []); // eslint-disable-line
  const doRedo = useCallback(() => { if (redo.current.length) { undo.current.push(snap()); restore(redo.current.pop()!); } }, []); // eslint-disable-line
  const select = useCallback((s: Selection) => { editKey.current = null; setSel(s); }, []);

  // Lint the draft in the background.
  useEffect(() => {
    if (!doc) return;
    const t = setTimeout(async () => {
      try { const r = await api<{ findings: Finding[] }>("POST", `/v1/agents/${encodeURIComponent(slug)}/check`, doc); setFindings(r.findings); setCheckError(""); }
      catch (e) { setCheckError((e as Error).message); }
    }, 450);
    return () => clearTimeout(t);
  }, [doc, slug]);

  const dirty = !!doc && JSON.stringify(doc) !== saved;
  const save = useCallback(async () => {
    const cur = docRef.current;
    if (!cur || saving) return false;
    setSaving(true);
    const body = JSON.stringify(cur);
    try {
      const r = await api<{ findings: Finding[] }>("PUT", `/v1/agents/${encodeURIComponent(slug)}`, cur);
      setSaved(body); setFindings(r.findings || []);
      setSavedAt(new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }));
      const errs = (r.findings || []).filter((f) => f.severity === "error").length;
      if (errs) toast.warning("Saved with problems", { description: `${errs} error${errs === 1 ? "" : "s"} to fix before calls can use this version.` });
      else toast.success("Saved", { description: `agents/${slug}` });
      return true;
    } catch (e) { toast.error("Not saved", { description: (e as Error).message }); return false; }
    finally { setSaving(false); }
  }, [slug, saving]);

  return { doc, rev, error, findings, checkError, sel, select, update, checkpoint, undo: doUndo, redo: doRedo,
    canUndo: undo.current.length > 0, canRedo: redo.current.length > 0, dirty, save, saving, savedAt };
}
export type BuilderApi = ReturnType<typeof useBuilder>;
