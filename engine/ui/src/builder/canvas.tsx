import { memo } from "react";
import { BaseEdge, EdgeLabelRenderer, getSmoothStepPath, Handle, Position, useConnection, type EdgeProps, type NodeProps, type Node, type Edge } from "@xyflow/react";
import { Bot, CircleStop, Globe, Play, Plug, AlertTriangle, Zap, ListChecks, Scale } from "lucide-react";
import { cn } from "@/lib/utils";
import type { DefNode, NodeType } from "./model";
import { text, TYPES } from "./model";
import { flowColor } from "./script";

export type StepNodeData = { def: DefNode; kind: string; isRoot: boolean; errors: number; warnings: number; problems: string; transfers: number; dimmed?: boolean };
export type StepNode = Node<StepNodeData, "step">;
export type TransferData = { label: string; condition: string; errors: number; warnings: number; afterGreeting: boolean; shift: number; rule?: string; dimmed?: boolean };
export type TransferEdge = Edge<TransferData, "transfer">;

const ICONS: Record<NodeType, typeof Bot> = { startCall: Play, agentNode: Bot, endCall: CircleStop, globalNode: Globe };

export const StepCard = memo(function StepCard({ id, data, selected }: NodeProps<StepNode>) {
  const { def } = data;
  // While a transfer is being dragged, the whole card accepts the drop, not just its top dot.
  const connecting = useConnection((c) => c.inProgress && c.fromNode?.id !== id);
  const t = TYPES[def.type] || TYPES.agentNode;
  const Icon = ICONS[def.type] || Bot;
  const d = def.data;
  const greeting = def.type === "startCall" ? text(d.greeting).trim() : "";
  const tools = Array.isArray(d.tools) ? d.tools : [];
  const details = d.extraction_enabled ? (d.extraction_variables || []).length : 0;
  const scripted = d.mode === "scripted";
  const form = scripted && Array.isArray(d.form) ? (d.form as { name: string; ask?: string }[]) : [];
  const flow = text(d.flow).trim();
  return (
    <div style={{ ["--k" as string]: t.color, ["--f" as string]: flow ? flowColor(flow) : "transparent" }}
      className={cn("group w-[264px] rounded-xl border bg-card text-card-foreground shadow-sm transition-[box-shadow,opacity]", data.dimmed && "opacity-30",
        flow && "border-l-4 border-l-[var(--f)]",
        selected ? "border-lamp shadow-[0_0_0_3px_var(--lamp-soft)]" : "hover:shadow-md",
        data.isRoot && !selected && "border-[color-mix(in_srgb,var(--k)_45%,var(--border))]")}>
      {t.ins && <Handle type="target" position={Position.Top} className="!size-3 !border-2 !border-[var(--k)] !bg-card" />}
      {t.ins && connecting && (
        <div className="pointer-events-none absolute -inset-1 rounded-[14px] border-2 border-dashed border-[var(--k)] bg-[color-mix(in_srgb,var(--k)_6%,transparent)]" />
      )}
      <div className="flex items-center gap-2 rounded-t-xl border-b bg-[color-mix(in_srgb,var(--k)_8%,transparent)] px-3 py-2">
        <span className="grid size-6 shrink-0 place-items-center rounded-md bg-[var(--k)] text-white dark:text-[#10171e]"><Icon className="size-3.5" /></span>
        <span className="shrink-0 whitespace-nowrap text-xs font-semibold text-[var(--k)]">{data.kind}</span>
        {scripted && <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-[color-mix(in_srgb,var(--k)_40%,transparent)] px-1.5 text-[10.5px] font-semibold text-[var(--k)]" title="Scripted: exact lines, form fields and rules">
          <ListChecks className="size-3" />Scripted</span>}
        {flow && <span className="min-w-0 truncate text-[10.5px] font-medium text-[var(--f)]" title={`Flow: ${flow}`}>{flow}</span>}
        {(data.errors > 0 || data.warnings > 0) && (
          <span title={data.problems} className={cn("ml-auto inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[11px] font-semibold",
            data.errors ? "bg-bad text-white" : "bg-warn text-[#1b1406]")}>
            <AlertTriangle className="size-3" />{data.errors + data.warnings}
          </span>
        )}
      </div>
      <div className="grid gap-1.5 px-3 pb-3 pt-2.5">
        <div className="text-[15px] font-semibold leading-snug break-words">{text(d.name) || def.id}</div>
        <p className={cn("line-clamp-3 text-[12.5px] leading-relaxed text-muted-foreground break-words", greeting && "italic")}>
          {greeting ? `“${greeting}”` : scripted ? (text(d.say).trim() ? `“${text(d.say).trim()}”` : form.length ? "" : "Nothing to say yet") : text(d.prompt).trim() || "No instructions yet"}
        </p>
        {form.length > 0 && (
          <ol className="grid min-w-0 grid-cols-[minmax(0,1fr)] gap-0.5 text-[11.5px]">
            {form.slice(0, 4).map((f, i) => (
              <li key={i} className="flex min-w-0 gap-1.5"><span className="font-mono text-muted-foreground">{i + 1}.</span><span className="min-w-0 truncate"><span className="font-mono">{f.name}</span>{f.ask ? <span className="text-muted-foreground"> · {f.ask}</span> : null}</span></li>
            ))}
            {form.length > 4 && <li className="text-muted-foreground">+{form.length - 4} more</li>}
          </ol>
        )}
        {def.type !== "globalNode" && (tools.length > 0 || details > 0 || data.transfers > 0) && (
          <div className="mt-0.5 flex flex-wrap gap-1">
            {tools.map((x) => (
              <span key={x} className="inline-flex max-w-full items-center gap-1 rounded-md border border-k-tool/40 bg-lamp-soft px-1.5 py-0.5 font-mono text-[11px]">
                <Plug className="size-3 shrink-0 text-k-tool" /><span className="truncate">{x}</span>
              </span>
            ))}
            {details > 0 && <span className="rounded-md border px-1.5 py-0.5 text-[11px] text-muted-foreground">Collects {details} detail{details === 1 ? "" : "s"}</span>}
            {data.transfers > 0 && <span className="rounded-md border px-1.5 py-0.5 text-[11px] text-muted-foreground">{data.transfers} transfer{data.transfers === 1 ? "" : "s"}</span>}
          </div>
        )}
      </div>
      {t.outs && <Handle type="source" position={Position.Bottom} className="!size-3.5 !border-2 !border-[var(--k)] !bg-card hover:!bg-[var(--k)]" title="Drag onto another step to add a transfer" />}
    </div>
  );
});

export function TransferLine(props: EdgeProps<TransferEdge>) {
  const { id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, data, selected, markerEnd } = props;
  const [path, labelX, labelY] = getSmoothStepPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, borderRadius: 14, offset: 28 });
  const tone = data?.errors ? "var(--bad)" : data?.warnings ? "var(--warn)" : undefined;
  const rule = data?.rule;
  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} interactionWidth={18}
        style={{ stroke: selected ? "var(--lamp)" : tone || "var(--muted-foreground)", strokeWidth: selected ? 2.4 : 1.6,
          opacity: data?.dimmed ? 0.15 : selected || tone ? 1 : 0.7, strokeDasharray: rule ? "6 4" : undefined }} />
      <EdgeLabelRenderer>
        <div className="nodrag nopan absolute" style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY + (data?.shift || 0) * 26}px)`, pointerEvents: "all" }}>
          <button type="button" data-edge={id} title={rule ? `${rule}: ${data?.condition}` : data?.condition} style={{ opacity: data?.dimmed ? 0.25 : 1 }}
            className={cn("inline-flex max-w-[200px] cursor-pointer items-center gap-1 truncate rounded-full border bg-card px-2.5 py-0.5 text-[11.5px] font-medium shadow-xs hover:border-lamp",
              selected && "border-lamp bg-lamp-soft", data?.errors ? "border-bad text-bad" : data?.warnings ? "border-warn text-warn" : "")}>
            {data?.afterGreeting && <Zap className="size-3 shrink-0 text-k-start" />}
            {rule && <Scale className="size-3 shrink-0 text-muted-foreground" aria-label="rule" />}
            <span className="truncate">{data?.label || "no label"}</span>
          </button>
        </div>
      </EdgeLabelRenderer>
    </>
  );
}
