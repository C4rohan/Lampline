import type { Finding } from "@/lib/api";

export type NodeType = "startCall" | "agentNode" | "endCall" | "globalNode";
export type StepData = {
  name?: string; prompt?: string; greeting?: string; add_global_prompt?: boolean; allow_interrupt?: boolean;
  extraction_enabled?: boolean; extraction_prompt?: string; extraction_variables?: { name: string; type?: string; prompt?: string }[];
  tools?: string[]; [k: string]: unknown;
};
export type DefNode = { id: string; type: NodeType; position: { x: number; y: number }; data: StepData; [k: string]: unknown };
export type EdgeData = { label?: string; condition?: string; transition_speech?: string; after_greeting?: boolean; [k: string]: unknown };
export type DefEdge = { id: string; source: string; target: string; data: EdgeData; [k: string]: unknown };
export type Param = { name: string; type?: string; description?: string; required?: boolean };
export type McpTool = { name: string; description?: string; input_schema?: { properties?: Record<string, { type?: string; description?: string }>; required?: string[] }; remote?: string };
export type Tool = {
  name: string; description?: string; method?: string; url?: string; headers?: Record<string, string>; body?: unknown;
  parameters?: Param[]; timeout_seconds?: number; say_while_running?: string; save?: Record<string, string>; mock?: unknown;
  connector?: string; with?: Record<string, any>; mcp?: { url?: string; headers?: Record<string, string>; tools?: McpTool[] }; [k: string]: unknown;
};
export type Send = { via?: string; to?: string; subject?: string; text?: string; url?: string; method?: string; headers?: Record<string, string>; payload?: unknown };
export type Trigger = {
  id: string; type: "message" | "after_call" | "schedule"; enabled?: boolean; description?: string;
  channel?: string; when?: string; channels?: string[]; send?: Send;
  cron?: string; action?: "call" | "message"; recipients?: { to: string; context?: Record<string, string> }[];
  recipients_from?: { url?: string; headers?: Record<string, string> }; text?: string; [k: string]: unknown;
};
export type Doc = { name: string; definition: { nodes: DefNode[]; edges: DefEdge[]; [k: string]: unknown }; context_variables: Record<string, string>; tools: Tool[]; triggers: Trigger[] };
/** node/edge: on the canvas; tool/trigger: by index; hook: an API Trigger or Webhook node (not drawn). */
export type Selection = { kind: "node" | "edge" | "tool" | "trigger" | "hook"; id: string } | null;
export const DETACHED = ["trigger", "webhook", "qa"];
export const onCanvas = (n: { type: string }) => !DETACHED.includes(n.type);
export const toolKind = (t: Tool) => (t.connector ? "connector" : t.mcp ? "mcp" : "http");

export const TYPES: Record<NodeType, { label: string; color: string; ins: boolean; outs: boolean }> = {
  startCall: { label: "Start", color: "var(--k-start)", ins: false, outs: true },
  agentNode: { label: "Sub-agent", color: "var(--k-agent)", ins: true, outs: true },
  endCall: { label: "End call", color: "var(--k-end)", ins: true, outs: false },
  globalNode: { label: "Global instructions", color: "var(--k-global)", ins: false, outs: false },
};

export const VAR_NAME = /^[A-Za-z_]\w{0,63}$/;
export const TOOL_NAME = /^[A-Za-z][A-Za-z0-9_]{0,63}$/;
export const text = (v: unknown) => (typeof v === "string" ? v : "");
export const toolName = (label: string) => (label || "").toLowerCase().replace(/[^a-z0-9]/g, "_");

export function rootId(doc: Doc): string | null {
  const start = doc.definition.nodes.find((n) => n.type === "startCall");
  if (!start) return null;
  const outs = doc.definition.edges.filter((e) => e.source === start.id);
  const t = outs.length === 1 ? doc.definition.nodes.find((n) => n.id === outs[0].target) : null;
  return t && t.type === "agentNode" ? t.id : null;
}
export function kindLabel(doc: Doc, n: DefNode) { return n.id === rootId(doc) ? "Root agent" : TYPES[n.type]?.label || "Step"; }

export function uniqueId(base: string, taken: string[]) {
  const ids = new Set(taken);
  if (!ids.has(base)) return base;
  let i = 2;
  while (ids.has(`${base}-${i}`)) i++;
  return `${base}-${i}`;
}
export function uniqueName(base: string, taken: string[]) {
  const names = new Set(taken.map((x) => x.trim().toLowerCase()));
  if (!names.has(base.toLowerCase())) return base;
  let i = 2;
  while (names.has(`${base} ${i}`.toLowerCase())) i++;
  return `${base} ${i}`;
}

export function findingsFor(findings: Finding[], doc: Doc, kind: "node" | "edge" | "tool" | "trigger", id: string) {
  const want = kind === "tool" ? doc.tools[+id]?.name : kind === "trigger" ? doc.triggers[+id]?.id : id;
  return findings.filter((f) => {
    const m = /^(node|edge|tool|trigger) (\S+)/.exec(f.where || "");
    return m && m[1] === kind && m[2] === want;
  });
}

export function selExists(doc: Doc, sel: Selection) {
  if (!sel) return false;
  if (sel.kind === "node") return doc.definition.nodes.some((n) => n.id === sel.id);
  if (sel.kind === "edge") return doc.definition.edges.some((e) => e.id === sel.id);
  if (sel.kind === "trigger") return !!doc.triggers[+sel.id];
  if (sel.kind === "hook") return doc.definition.nodes.some((n) => n.id === sel.id && !onCanvas(n));
  return !!doc.tools[+sel.id];
}

export function newStepData(type: NodeType, doc: Doc): StepData {
  const names = doc.definition.nodes.map((n) => text(n.data.name));
  if (type === "agentNode") return { name: uniqueName("New sub-agent", names), prompt: "Describe this agent's job: what it helps the caller with, what it asks, and what it must not do.", add_global_prompt: true, allow_interrupt: true };
  if (type === "endCall") return { name: uniqueName("Goodbye", names), prompt: "Thank the caller and say goodbye in one short sentence.", add_global_prompt: true };
  return { name: "Global instructions", prompt: "You are a friendly phone assistant. Keep answers short and natural; this is a phone call." };
}

/** Where a step names tools: the ones the model may call, and the ones a scripted page runs itself. */
export const TOOL_LISTS = ["tools", "on_entry", "on_done"] as const;
export const usesTool = (n: DefNode, name: string) => TOOL_LISTS.some((k) => Array.isArray(n.data[k]) && (n.data[k] as string[]).includes(name));
/** Rename a tool on a step, or remove it (to = null). */
export function renameTool(n: DefNode, from: string, to: string | null) {
  for (const k of TOOL_LISTS) {
    const cur = n.data[k];
    if (!Array.isArray(cur)) continue;
    const next = (cur as string[]).flatMap((y) => (y === from ? (to === null ? [] : [to]) : [y]));
    if (next.length) (n.data as Record<string, unknown>)[k] = next; else delete (n.data as Record<string, unknown>)[k];
  }
}
