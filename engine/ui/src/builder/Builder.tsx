import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  applyNodeChanges, Background, BackgroundVariant, Controls, MarkerType, MiniMap, ReactFlow, ReactFlowProvider, useReactFlow,
  type Connection, type NodeChange,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import dagre from "@dagrejs/dagre";
import { toast } from "sonner";
import { AlertTriangle, ArrowLeft, ListChecks, Layers, AudioLines, Bot, ChevronDown, CircleCheck, CircleStop, Download, Globe, LayoutGrid, Plug, Plus, Redo2, Save, Server, Undo2, Webhook, Zap } from "lucide-react";
import { go } from "@/lib/route";
import { api, download } from "@/lib/api";
import { useApp } from "@/lib/app";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger, Tip } from "@/components/ui/menu";
import { StepCard, TransferLine, type StepNode, type TransferEdge } from "./canvas";
import { useBuilder, type BuilderApi } from "./useBuilder";
import type { DefNode, NodeType } from "./model";
import { findingsFor, kindLabel, onCanvas, rootId, selExists, text, toolKind, toolName, TYPES, usesTool } from "./model";
import { addStep, AgentPanel, deleteSel, EdgePanel, NodePanel, Problems, ToolPanel } from "./Inspector";
import { flowColor, flows, routeKind, RULE_LABEL } from "./script";
import { isLocal, LocalToolPanel } from "./local";
import { addTool, addTrigger, CONNECTORS, LOCAL_SERVICES, ConnectorToolPanel, HookPanel, McpToolPanel, TRIGGER_KINDS, TriggerPanel, triggerLabel } from "./extras";

const nodeTypes = { step: StepCard };
const edgeTypes = { transfer: TransferLine };
const PALETTE: { type: NodeType; icon: typeof Bot; title: string; sub: string; scripted?: boolean }[] = [
  { type: "agentNode", icon: Bot, title: "Sub-agent", sub: "Handles one job, then transfers" },
  { type: "agentNode", icon: ListChecks, title: "Scripted page", sub: "Exact lines, form fields, rules", scripted: true },
  { type: "endCall", icon: CircleStop, title: "End call", sub: "Says goodbye and hangs up" },
  { type: "globalNode", icon: Globe, title: "Global instructions", sub: "Persona and rules for every step" },
];

export default function Builder({ slug }: { slug: string }) {
  const b = useBuilder(slug);
  if (b.error) return <div className="p-8"><h1 className="text-xl font-semibold">Couldn't open this agent</h1><p className="mt-2 text-sm text-bad">{b.error}</p></div>;
  if (!b.doc) return <div className="p-8 text-sm text-muted-foreground">Opening the builder…</div>;
  return <ReactFlowProvider><Studio b={b} slug={slug} /></ReactFlowProvider>;
}

function Studio({ b, slug }: { b: BuilderApi; slug: string }) {
  const doc = b.doc!;
  const rf = useReactFlow();
  const wrap = useRef<HTMLDivElement>(null);
  const [secrets, setSecrets] = useState<string[]>([]);
  const { health } = useApp();
  const c = (health?.config || {}) as Record<string, unknown>;
  const status = { email: !!c.email_ready, whatsapp: !!c.whatsapp_ready, telegram: !!c.telegram_ready, google: !!c.google_ready, data: true, knowledge: true };
  const exportTo = async (target: "dfcx" | "ces") => {
    if (b.dirty && !(await b.save())) return;
    try {
      const notes = await download(`/v1/agents/${encodeURIComponent(slug)}/export?target=${target}`, `${slug}.zip`);
      toast.success(`Exported for ${target === "dfcx" ? "Dialogflow CX" : "CX Agent Studio"}`, {
        description: target === "dfcx" ? "Restore it into a Dialogflow CX agent (Agent settings → Restore), then Validate." : "Import it in CX Agent Studio (Import app).",
        duration: 8000 });
      if (notes && notes !== "0") toast(`${notes} thing${notes === "1" ? "" : "s"} to check after importing`, { description: "Run `vk export` to see the list." });
    } catch (e) { toast.error("Export failed", { description: (e as Error).message }); }
  };
  useEffect(() => { api<{ secrets: { name: string }[] }>("GET", "/v1/secrets").then((r) => setSecrets(r.secrets.map((s) => s.name))).catch(() => {}); }, []);

  /* ---- what React Flow draws, derived from the document ---- */
  const root = rootId(doc);
  const [focus, setFocus] = useState<string | null>(null);         // a flow to highlight
  const inFocus = (n?: DefNode) => !focus || text(n?.data.flow).trim() === focus;
  const derived: StepNode[] = useMemo(() => doc.definition.nodes.filter(onCanvas).map((n) => {
    const f = findingsFor(b.findings, doc, "node", n.id);
    return {
      id: n.id, type: "step", position: n.position, selected: b.sel?.kind === "node" && b.sel.id === n.id,
      data: { def: n, kind: kindLabel(doc, n), isRoot: n.id === root, errors: f.filter((x) => x.severity === "error").length,
        warnings: f.filter((x) => x.severity === "warning").length, problems: f.map((x) => `${x.code}: ${x.message}`).join("\n"),
        transfers: doc.definition.edges.filter((e) => e.source === n.id).length, dimmed: !inFocus(n) },
    };
  }), [doc, b.findings, b.sel, root, focus]); // eslint-disable-line
  const [nodes, setNodes] = useState<StepNode[]>(derived);
  useEffect(() => { setNodes((prev) => { const old = new Map(prev.map((n) => [n.id, n])); return derived.map((d) => ({ ...old.get(d.id), ...d })); }); }, [derived]);

  const edges: TransferEdge[] = useMemo(() => {
    // Two transfers between the same pair of steps (there and back) get their labels apart.
    const seen = new Map<string, number>();
    return doc.definition.edges.map((e) => {
    const pair = [e.source, e.target].sort().join("|");
    const shift = seen.get(pair) || 0;
    seen.set(pair, shift + 1);
    const f = findingsFor(b.findings, doc, "edge", e.id);
    const selected = b.sel?.kind === "edge" && b.sel.id === e.id;
    const errors = f.filter((x) => x.severity === "error").length;
    return {
      id: e.id, source: e.source, target: e.target, type: "transfer", selected,
      markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16, color: selected ? "var(--lamp)" : errors ? "var(--bad)" : "var(--muted-foreground)" },
      data: { label: text(e.data.label), condition: text(e.data.condition), errors, warnings: f.length - errors, afterGreeting: !!e.data.after_greeting, shift,
        rule: routeKind(e) !== "ask" ? RULE_LABEL[routeKind(e)] : undefined,
        dimmed: !!focus && !(inFocus(doc.definition.nodes.find((x) => x.id === e.source)) && inFocus(doc.definition.nodes.find((x) => x.id === e.target))) },
    };
  });
  }, [doc, b.findings, b.sel, focus]); // eslint-disable-line

  const onNodesChange = useCallback((changes: NodeChange<StepNode>[]) => {
    setNodes((ns) => applyNodeChanges(changes, ns));
    const moved = changes.filter((c): c is Extract<NodeChange<StepNode>, { type: "position" }> => c.type === "position" && !!c.position);
    if (moved.length) b.update((d) => {
      for (const c of moved) { const n = d.definition.nodes.find((x) => x.id === c.id); if (n && c.position) n.position = { x: Math.round(c.position.x), y: Math.round(c.position.y) }; }
    }, { noUndo: true });
  }, [b]);

  /* ---- connecting steps ---- */
  const typeOf = (id: string) => doc.definition.nodes.find((n) => n.id === id)?.type;
  const isValidConnection = useCallback((c: Connection | TransferEdge) => {
    const s = typeOf(c.source), t = typeOf(c.target);
    return !!s && !!t && c.source !== c.target && TYPES[s].outs && TYPES[t].ins;
  }, [doc]); // eslint-disable-line
  const onConnect = useCallback((c: Connection) => {
    const target = doc.definition.nodes.find((n) => n.id === c.target);
    if (!target || !c.source) return;
    if (target.type === "startCall") { toast.error("Start is where every call begins, so nothing can transfer back to it."); return; }
    if (target.type === "globalNode") { toast.error("Global instructions aren't a step; they're added to every step's instructions."); return; }
    if (typeOf(c.source) === "endCall" || typeOf(c.source) === "globalNode") return;
    const existing = doc.definition.edges.find((e) => e.source === c.source && e.target === c.target);
    if (existing) { b.select({ kind: "edge", id: existing.id }); toast("That transfer already exists; it's selected."); return; }
    const name = text(target.data.name).trim() || target.id;
    const used = new Set(doc.definition.edges.filter((e) => e.source === c.source).map((e) => toolName(text(e.data.label))));
    const base = target.type === "endCall" ? "end call" : name.toLowerCase();
    let label = base, k = 2;
    while (used.has(toolName(label))) label = `${base} ${k++}`;
    const ids = new Set(doc.definition.edges.map((e) => e.id));
    let id = `${c.source}-${c.target}`, j = 2;
    while (ids.has(id)) id = `${c.source}-${c.target}-${j++}`;
    b.update((d) => {
      d.definition.edges.push({ id, source: c.source!, target: c.target!, data: { label,
        condition: target.type === "endCall" ? "The caller has everything they need and is ready to end the call." : `The caller needs help with ${name.toLowerCase()}.` } });
    });
    b.select({ kind: "edge", id });
  }, [doc, b]);

  /* ---- adding steps: drag from the palette, or click to drop in the middle ---- */
  const add = (key: string, at: { x: number; y: number }) => {
    const [type, mode] = key.split(":") as [NodeType, string | undefined];
    addStep(b, type, at);
    if (mode === "scripted") b.update((d) => {
      const n = d.definition.nodes[d.definition.nodes.length - 1];
      Object.assign(n.data, { name: n.data.name === "New sub-agent" ? "New scripted page" : n.data.name, mode: "scripted", say: "", form: [{ name: "field_1", type: "text", ask: "" }] });
      delete n.data.prompt;
      if (focus) n.data.flow = focus;
    }, { noUndo: true });
    else if (focus && type !== "globalNode") b.update((d) => { d.definition.nodes[d.definition.nodes.length - 1].data.flow = focus; }, { noUndo: true });
  };
  const centre = () => {
    const r = wrap.current!.getBoundingClientRect();
    const p = rf.screenToFlowPosition({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
    const k = doc.definition.nodes.length % 4;
    return { x: p.x - 132 + k * 24, y: p.y - 60 + k * 24 };
  };
  const onDrop = (e: React.DragEvent) => {
    const raw = e.dataTransfer.getData("application/x-vp-step");
    if (!raw) return;
    e.preventDefault();
    const p = rf.screenToFlowPosition({ x: e.clientX, y: e.clientY });
    add(raw, { x: p.x - 132, y: p.y - 24 });
  };

  /* ---- tidy layout (dagre, top to bottom) ---- */
  const tidy = () => {
    const g = new dagre.graphlib.Graph();
    g.setGraph({ rankdir: "TB", nodesep: 56, ranksep: 90, marginx: 20, marginy: 20 });
    g.setDefaultEdgeLabel(() => ({}));
    const flow = doc.definition.nodes.filter((n) => n.type !== "globalNode" && onCanvas(n));
    for (const n of flow) {
      const m = rf.getNode(n.id)?.measured;
      g.setNode(n.id, { width: m?.width || 264, height: m?.height || 130 });
    }
    for (const e of doc.definition.edges) if (g.hasNode(e.source) && g.hasNode(e.target)) g.setEdge(e.source, e.target);
    dagre.layout(g);
    let right = 0;
    b.update((d) => {
      for (const n of d.definition.nodes) {
        const p = g.node(n.id);
        if (!p) continue;
        n.position = { x: Math.round(p.x - p.width / 2), y: Math.round(p.y - p.height / 2) };
        right = Math.max(right, n.position.x + p.width);
      }
      d.definition.nodes.filter((n) => n.type === "globalNode").forEach((n, i) => { n.position = { x: right + 100, y: 20 + i * 200 }; });
    });
    setTimeout(() => rf.fitView({ padding: 0.15, maxZoom: 1, duration: 300 }), 50);
  };

  const jump = (where: string) => {
    const m = /^(node|edge|tool|trigger) (\S+)/.exec(where || "");
    if (!m) { b.select(null); return; }
    if (m[1] === "tool") { const i = doc.tools.findIndex((t) => t.name === m[2]); if (i >= 0) b.select({ kind: "tool", id: String(i) }); return; }
    if (m[1] === "trigger") { const i = doc.triggers.findIndex((t) => t.id === m[2]); if (i >= 0) b.select({ kind: "trigger", id: String(i) }); return; }
    if (m[1] === "node" && doc.definition.nodes.some((n) => n.id === m[2] && !onCanvas(n))) { b.select({ kind: "hook", id: m[2] }); return; }
    const sel = { kind: m[1] as "node" | "edge", id: m[2] };
    if (!selExists(doc, sel)) return;
    b.select(sel);
    const nodeId = sel.kind === "node" ? sel.id : doc.definition.edges.find((e) => e.id === sel.id)?.source;
    const n = nodeId && rf.getNode(nodeId);
    if (n) rf.setCenter(n.position.x + (n.measured?.width || 264) / 2, n.position.y + (n.measured?.height || 120) / 2, { zoom: Math.max(rf.getZoom(), 0.8), duration: 300 });
  };

  /* ---- keyboard and leaving ---- */
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const mod = e.metaKey || e.ctrlKey, k = e.key.toLowerCase();
      if (mod && k === "s") { e.preventDefault(); b.save(); return; }
      const t = e.target as HTMLElement;
      if (t.closest("input, textarea, select, [contenteditable], [role=dialog]")) return;
      if (mod && k === "z") { e.preventDefault(); if (e.shiftKey) b.redo(); else b.undo(); }
      else if (mod && k === "y") { e.preventDefault(); b.redo(); }
      else if ((e.key === "Delete" || e.key === "Backspace") && b.sel) {
        e.preventDefault();
        if (!deleteSel(b, b.sel)) toast.error("Every agent needs its Start step.");
      } else if (e.key === "Escape" && b.sel) b.select(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [b]);
  useEffect(() => {
    const before = (e: BeforeUnloadEvent) => { if (b.dirty) { e.preventDefault(); e.returnValue = ""; } };
    const click = (e: MouseEvent) => {
      const a = (e.target as HTMLElement).closest?.('a[href^="#/"]');
      if (a && b.dirty && !confirm("Leave the builder? Your unsaved changes will be lost.")) { e.preventDefault(); e.stopPropagation(); }
    };
    window.addEventListener("beforeunload", before);
    document.addEventListener("click", click, true);
    return () => { window.removeEventListener("beforeunload", before); document.removeEventListener("click", click, true); };
  }, [b.dirty]);

  const errs = b.findings.filter((f) => f.severity === "error").length;
  const warns = b.findings.length - errs;
  const sel = selExists(doc, b.sel) ? b.sel : null;
  const panelKey = `${sel?.kind}-${sel?.id}-${b.rev}`;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="flex flex-wrap items-center gap-2 border-b bg-card px-3 py-2">
        <Tip content="Back to agents"><Button asChild variant="ghost" size="icon-sm" aria-label="Back to agents"><a href="#/agents"><ArrowLeft /></a></Button></Tip>
        <input aria-label="Agent name" value={doc.name} maxLength={80}
          onChange={(e) => b.update((d) => { d.name = e.target.value; }, { key: "agent:name" })}
          className="h-8 w-56 min-w-0 rounded-md border border-transparent bg-transparent px-2 text-[15px] font-semibold outline-none hover:border-border focus:border-ring focus:bg-card" />
        <span className="hidden font-mono text-xs text-muted-foreground lg:inline">agents/{slug}</span>
        <span className={cn("text-[13px]", b.dirty ? "font-medium text-warn" : "text-muted-foreground")} role="status">
          {b.saving ? "Saving…" : b.dirty ? "Unsaved changes" : b.savedAt ? `Saved at ${b.savedAt}` : "All changes saved"}
        </span>
        <div className="ml-auto flex flex-wrap items-center gap-1">
          <button type="button" onClick={() => b.select(null)} className="cursor-pointer" title="Show all problems">
            <Badge variant={errs ? "bad" : warns ? "warn" : "ok"}>
              {errs || warns ? <AlertTriangle /> : <CircleCheck />}
              {errs || warns ? [errs && `${errs} error${errs === 1 ? "" : "s"}`, warns && `${warns} warning${warns === 1 ? "" : "s"}`].filter(Boolean).join(", ") : "No problems"}
            </Badge>
          </button>
          <Tip content="Undo (⌘/Ctrl Z)"><Button variant="ghost" size="icon-sm" onClick={b.undo} disabled={!b.canUndo} aria-label="Undo"><Undo2 /></Button></Tip>
          <Tip content="Redo (⇧⌘/Ctrl Z)"><Button variant="ghost" size="icon-sm" onClick={b.redo} disabled={!b.canRedo} aria-label="Redo"><Redo2 /></Button></Tip>
          <Tip content="Tidy up the layout"><Button variant="ghost" size="icon-sm" onClick={tidy} aria-label="Tidy up the layout"><LayoutGrid /></Button></Tip>
          <Menu>
            <MenuTrigger asChild><Button variant="ghost" size="sm"><Download />Export<ChevronDown className="opacity-60" /></Button></MenuTrigger>
            <MenuContent>
              <MenuItem onSelect={() => exportTo("ces")}>CX Agent Studio app (.zip)</MenuItem>
              <MenuItem onSelect={() => exportTo("dfcx")}>Dialogflow CX agent, playbooks (.zip)</MenuItem>
            </MenuContent>
          </Menu>
          <Button variant="outline" size="sm" onClick={async () => { if (b.dirty && !(await b.save())) return; go(`#/test?agent=${encodeURIComponent(slug)}`); }}><AudioLines />Test</Button>
          <Button size="sm" onClick={() => b.save()} disabled={!b.dirty || b.saving}><Save />Save</Button>
        </div>
      </header>
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto md:flex-row md:overflow-hidden">
        <aside className="flex shrink-0 gap-2 overflow-x-auto border-b bg-sidebar p-3 md:w-56 md:flex-col md:overflow-y-auto md:border-b-0 md:border-r" aria-label="Add steps and tools">
          <p className="hidden text-xs font-medium text-muted-foreground md:block">Add to the canvas</p>
          {PALETTE.map((p) => (
            <button key={p.title} type="button" draggable data-type={p.type} style={{ ["--k" as string]: TYPES[p.type].color }}
              onDragStart={(e) => { e.dataTransfer.setData("application/x-vp-step", p.scripted ? `${p.type}:scripted` : p.type); e.dataTransfer.effectAllowed = "move"; }}
              onClick={() => add(p.scripted ? `${p.type}:scripted` : p.type, centre())}
              title="Drag onto the canvas, or click to add"
              className="flex shrink-0 cursor-grab items-center gap-2.5 rounded-lg border bg-card px-2.5 py-2 text-left shadow-xs transition-colors hover:border-[var(--k)] active:cursor-grabbing md:w-full">
              <span className="grid size-7 shrink-0 place-items-center rounded-md bg-[color-mix(in_srgb,var(--k)_14%,transparent)] text-[var(--k)]"><p.icon className="size-4" /></span>
              <span className="grid min-w-0"><span className="text-[13px] font-medium leading-tight">{p.title}</span><span className="hidden truncate text-[11.5px] text-muted-foreground md:block">{p.sub}</span></span>
            </button>
          ))}
          <p className="hidden text-xs leading-relaxed text-muted-foreground md:block">Drag onto the canvas, or click to add. Then drag from the dot under a card onto another card to connect them.</p>
          {flows(doc).length > 0 && (
            <>
              <div className="hidden h-px bg-border md:block" />
              <p className="hidden text-xs font-medium text-muted-foreground md:block">Flows</p>
              {flows(doc).map(([f, count]) => (
                <button key={f} type="button" onClick={() => setFocus(focus === f ? null : f)} aria-pressed={focus === f}
                  className={cn("flex shrink-0 cursor-pointer items-center gap-2 rounded-lg border bg-card px-3 py-1.5 text-left text-[12.5px] shadow-xs hover:bg-accent md:w-full", focus === f && "border-lamp ring-2 ring-lamp-soft")}>
                  <Layers className="size-3.5 shrink-0" style={{ color: flowColor(f) }} />
                  <span className="min-w-0 truncate">{f}</span><span className="ml-auto text-[11px] text-muted-foreground">{count}</span>
                </button>
              ))}
              {focus && <p className="hidden text-[11.5px] text-muted-foreground md:block">Showing {focus}; new steps join it. Click it again to show everything.</p>}
            </>
          )}
          <div className="hidden h-px bg-border md:block" />
          <p className="hidden text-xs font-medium text-muted-foreground md:block">Tools</p>
          {doc.tools.map((t, i) => {
            const f = findingsFor(b.findings, doc, "tool", String(i));
            const users = doc.definition.nodes.filter((n) => usesTool(n, t.name)).length;
            const on = sel?.kind === "tool" && +sel.id === i;
            return (
              <button key={i} type="button" onClick={() => b.select({ kind: "tool", id: String(i) })}
                className={cn("flex shrink-0 cursor-pointer items-center gap-2 rounded-lg border bg-card px-3 py-2 text-left text-[13px] shadow-xs hover:bg-accent md:w-full", on && "border-lamp ring-2 ring-lamp-soft")}>
                {toolKind(t) === "mcp" ? <Server className="size-4 shrink-0 text-k-tool" />
                  : toolKind(t) === "connector" ? (() => { const I = CONNECTORS[t.connector || ""]?.icon || Plug; return <I className="size-4 shrink-0 text-k-tool" />; })()
                  : <Plug className="size-4 shrink-0 text-k-tool" />}
                <span className="min-w-0 truncate font-mono text-[12.5px]">{t.name || "(no name)"}</span>
                {f.length ? <Badge variant={f.some((x) => x.severity === "error") ? "bad" : "warn"} className="ml-auto">{f.length}</Badge>
                  : <span className="ml-auto shrink-0 text-[11.5px] text-muted-foreground">{users ? `${users} step${users === 1 ? "" : "s"}` : "unused"}</span>}
              </button>
            );
          })}
          <Menu>
            <MenuTrigger asChild><Button variant="outline" size="sm" className="shrink-0 md:w-full"><Plus />New tool<ChevronDown className="ml-auto opacity-60" /></Button></MenuTrigger>
            <MenuContent align="start" className="w-64">
              <MenuItem onSelect={() => addTool(b, "http")}><Plug />HTTP request<span className="ml-auto text-[11px] text-muted-foreground">any API</span></MenuItem>
              <MenuItem onSelect={() => addTool(b, "mcp")}><Server />MCP server<span className="ml-auto text-[11px] text-muted-foreground">its tools</span></MenuItem>
              <MenuSeparator />
              {Object.entries(CONNECTORS).filter(([, v]) => LOCAL_SERVICES.includes(v.service)).map(([k, v]) => <MenuItem key={k} onSelect={() => addTool(b, k)}><v.icon />{v.label}<span className="ml-auto text-[11px] text-muted-foreground">this computer</span></MenuItem>)}
              <MenuSeparator />
              {Object.entries(CONNECTORS).filter(([, v]) => !LOCAL_SERVICES.includes(v.service)).map(([k, v]) => <MenuItem key={k} onSelect={() => addTool(b, k)}><v.icon />{v.label}</MenuItem>)}
            </MenuContent>
          </Menu>
          <div className="hidden h-px bg-border md:block" />
          <p className="hidden text-xs font-medium text-muted-foreground md:block">Triggers</p>
          {doc.triggers.map((t, i) => {
            const f = findingsFor(b.findings, doc, "trigger", String(i));
            const on = sel?.kind === "trigger" && +sel.id === i;
            const k = TRIGGER_KINDS.find((x) => x.key === (t.type === "message" ? `message:${t.channel}` : t.type));
            const Icon = k?.icon || Zap;
            return (
              <button key={`t${i}`} type="button" onClick={() => b.select({ kind: "trigger", id: String(i) })}
                className={cn("flex shrink-0 cursor-pointer items-center gap-2 rounded-lg border bg-card px-3 py-2 text-left text-[12.5px] shadow-xs hover:bg-accent md:w-full", on && "border-lamp ring-2 ring-lamp-soft", t.enabled === false && "opacity-60")}>
                <Icon className="size-4 shrink-0 text-lamp" />
                <span className="min-w-0 truncate">{triggerLabel(t)}</span>
                {f.length ? <Badge variant={f.some((x) => x.severity === "error") ? "bad" : "warn"} className="ml-auto">{f.length}</Badge>
                  : t.enabled === false ? <span className="ml-auto text-[11px] text-muted-foreground">off</span> : null}
              </button>
            );
          })}
          {doc.definition.nodes.filter((n) => !onCanvas(n) && (String(n.type) === "trigger" || String(n.type) === "webhook")).map((n) => {
            const on = sel?.kind === "hook" && sel.id === n.id;
            return (
              <button key={n.id} type="button" onClick={() => b.select({ kind: "hook", id: n.id })}
                className={cn("flex shrink-0 cursor-pointer items-center gap-2 rounded-lg border bg-card px-3 py-2 text-left text-[12.5px] shadow-xs hover:bg-accent md:w-full", on && "border-lamp ring-2 ring-lamp-soft", n.data.enabled === false && "opacity-60")}>
                {String(n.type) === "trigger" ? <Zap className="size-4 shrink-0 text-lamp" /> : <Webhook className="size-4 shrink-0 text-lamp" />}
                <span className="min-w-0 truncate">{String(n.type) === "trigger" ? "API trigger" : text(n.data.name) || "Webhook"}</span>
              </button>
            );
          })}
          <Menu>
            <MenuTrigger asChild><Button variant="outline" size="sm" className="shrink-0 md:w-full"><Plus />New trigger<ChevronDown className="ml-auto opacity-60" /></Button></MenuTrigger>
            <MenuContent align="start" className="w-72">
              {TRIGGER_KINDS.map((k, j) => (
                <div key={k.key}>
                  {(j === 3 || j === 5) && <MenuSeparator />}
                  <MenuItem onSelect={() => addTrigger(b, k.key)}>
                    <k.icon /><span className="grid"><span>{k.label}</span><span className="text-[11.5px] text-muted-foreground">{k.sub}</span></span>
                  </MenuItem>
                </div>
              ))}
            </MenuContent>
          </Menu>
        </aside>
        <div ref={wrap} className="relative h-[62vh] shrink-0 md:h-auto md:min-h-0 md:flex-1 md:shrink" onDragOver={(e) => { e.preventDefault(); e.dataTransfer.dropEffect = "move"; }} onDrop={onDrop}
          onClick={(e) => { const el = (e.target as HTMLElement).closest("[data-edge]") as HTMLElement | null; if (el) b.select({ kind: "edge", id: el.dataset.edge! }); }}>
          <ReactFlow<StepNode, TransferEdge>
            nodes={nodes} edges={edges} nodeTypes={nodeTypes} edgeTypes={edgeTypes}
            onNodesChange={onNodesChange} onConnect={onConnect} isValidConnection={isValidConnection}
            onConnectEnd={(event, state) => {
              // Dropping anywhere on a card connects to it, not only on its small top dot.
              if (state.isValid || !state.fromNode || state.fromHandle?.type !== "source") return;
              const pt = "changedTouches" in event ? event.changedTouches[0] : event;
              const el = document.elementFromPoint(pt.clientX, pt.clientY)?.closest(".react-flow__node");
              const target = el?.getAttribute("data-id");
              if (target && target !== state.fromNode.id) onConnect({ source: state.fromNode.id, target, sourceHandle: null, targetHandle: null });
            }}
            onNodeDragStart={() => b.checkpoint()}
            onNodeClick={(_, n) => b.select({ kind: "node", id: n.id })}
            onEdgeClick={(_, e) => b.select({ kind: "edge", id: e.id })}
            onPaneClick={() => b.select(null)}
            deleteKeyCode={null} selectionKeyCode={null} multiSelectionKeyCode={null}
            fitView fitViewOptions={{ padding: 0.12, maxZoom: 1, minZoom: 0.2 }} minZoom={0.15} maxZoom={2}
            snapToGrid snapGrid={[10, 10]} connectionRadius={36}
            defaultEdgeOptions={{ type: "transfer" }} proOptions={{ hideAttribution: true }}
            colorMode={document.documentElement.classList.contains("dark") ? "dark" : "light"}>
            <Background variant={BackgroundVariant.Dots} gap={20} size={1.4} color="var(--canvas-dot)" />
            <Controls showInteractive={false} position="bottom-left" />
            <MiniMap position="bottom-right" pannable zoomable nodeStrokeWidth={3} style={{ width: 168, height: 112 }}
              nodeColor={(n) => TYPES[(n as StepNode).data.def.type]?.color || "var(--muted-foreground)"} className="!hidden md:!block" />
          </ReactFlow>
        </div>
        <aside className="shrink-0 border-t bg-card md:min-h-0 md:w-[380px] md:overflow-y-auto md:border-l md:border-t-0" aria-label="Edit selection" key={panelKey}>
          {sel?.kind === "node" ? <NodePanel b={b} n={doc.definition.nodes.find((n) => n.id === sel.id)!} />
            : sel?.kind === "edge" ? <EdgePanel b={b} e={doc.definition.edges.find((e) => e.id === sel.id)!} />
            : sel?.kind === "tool" ? (isLocal(doc.tools[+sel.id]) ? <LocalToolPanel b={b} i={+sel.id} onDelete={() => deleteSel(b, sel)} />
              : toolKind(doc.tools[+sel.id]) === "connector" ? <ConnectorToolPanel b={b} i={+sel.id} status={status} onDelete={() => deleteSel(b, sel)} />
              : toolKind(doc.tools[+sel.id]) === "mcp" ? <McpToolPanel b={b} i={+sel.id} secrets={secrets} onDelete={() => deleteSel(b, sel)} />
              : <ToolPanel b={b} i={+sel.id} secrets={secrets} />)
            : sel?.kind === "trigger" ? <TriggerPanel b={b} i={+sel.id} slug={slug} status={status} onDelete={() => deleteSel(b, sel)} />
            : sel?.kind === "hook" ? <HookPanel b={b} id={sel.id} onDelete={() => deleteSel(b, sel)} />
            : <AgentPanel b={b} />}
          <Problems b={b} onJump={jump} />
        </aside>
      </div>
    </div>
  );
}
