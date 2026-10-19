import { AudioLines, Copy, FlaskConical, MoreHorizontal, PencilRuler, Search, Workflow } from "lucide-react";
import { useState } from "react";
import { toast } from "sonner";
import { api, type AgentSummary } from "@/lib/api";
import { useLoad } from "@/lib/app";
import { go } from "@/lib/route";
import { Page } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Menu, MenuContent, MenuItem, MenuTrigger } from "@/components/ui/menu";
import { NewAgentButton } from "./NewAgent";

const KIND_DOT: Record<string, string> = { start: "bg-k-start", agent: "bg-k-agent", end: "bg-k-end", global: "bg-k-global" };

function Flow({ graph }: { graph: AgentSummary["graph"] }) {
  return (
    <div className="flex min-w-0 flex-wrap items-center gap-1.5">
      {graph.filter((s) => s.kind !== "global").slice(0, 6).map((s, i) => (
        <span key={i} className="inline-flex max-w-40 items-center gap-1.5 rounded-md border bg-card px-2 py-0.5 text-xs">
          <span className={`size-1.5 shrink-0 rounded-full ${KIND_DOT[s.kind] || "bg-muted-foreground"}`} /><span className="truncate">{s.name}</span>
        </span>
      ))}
      {graph.length > 7 && <span className="text-xs text-muted-foreground">+{graph.length - 7} more</span>}
    </div>
  );
}

export function Agents() {
  const { data, error } = useLoad(() => api<{ agents: AgentSummary[]; dir: string }>("GET", "/v1/agents"));
  const [q, setQ] = useState("");
  const agents = (data?.agents || []).filter((a) => !q || `${a.name} ${a.slug}`.toLowerCase().includes(q.toLowerCase()));
  return (
    <Page title="Voice agents" description={data ? <>Folders in <code className="text-foreground">{data.dir}</code>. Changes apply from an agent's next call.</> : "Loading…"}
      actions={<NewAgentButton />}>
      {error && <p className="text-sm text-bad">{error}</p>}
      <div className="mb-3 flex items-center gap-2">
        <div className="relative w-full max-w-xs">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input className="pl-8" placeholder="Search agents" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Search agents" />
        </div>
      </div>
      <Card className="overflow-hidden">
        <div className="hidden grid-cols-[minmax(0,1.4fr)_minmax(0,2fr)_110px_120px] gap-4 border-b bg-muted/40 px-5 py-2.5 text-xs font-medium text-muted-foreground md:grid">
          <span>Agent</span><span>Steps</span><span>Tests</span><span className="text-right">Actions</span>
        </div>
        {data && agents.length === 0 && (
          <div className="grid justify-items-start gap-3 p-6 text-sm text-muted-foreground">
            {q ? "No agent matches that search." : "No agents yet. Create one, or start from a template."}
            {!q && <NewAgentButton variant="outline" />}
          </div>
        )}
        {agents.map((a) => (
          <div key={a.slug} className="grid cursor-pointer gap-3 border-b px-5 py-4 last:border-0 hover:bg-muted/40 md:grid-cols-[minmax(0,1.4fr)_minmax(0,2fr)_110px_120px] md:items-center md:gap-4"
            onClick={() => !a.error && go(`#/build/${encodeURIComponent(a.slug)}`)}>
            <div className="flex min-w-0 items-center gap-3">
              <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-k-agent/12 text-k-agent"><Workflow className="size-[18px]" /></span>
              <div className="grid min-w-0 gap-0.5">
                <span className="truncate font-medium">{a.error ? a.slug : a.name}</span>
                <span className="truncate font-mono text-xs text-muted-foreground">agents/{a.slug}</span>
              </div>
            </div>
            {a.error ? <p className="text-sm text-bad md:col-span-2">{a.error}</p> : <Flow graph={a.graph} />}
            {!a.error && (
              <div className="flex gap-1.5">
                <Badge variant={a.tests ? "ok" : "outline"}><FlaskConical />{a.tests} test{a.tests === 1 ? "" : "s"}</Badge>
              </div>
            )}
            <div className="flex justify-end gap-1" onClick={(e) => e.stopPropagation()}>
              {!a.error && (
                <>
                  <Button asChild variant="outline" size="sm"><a href={`#/build/${encodeURIComponent(a.slug)}`}><PencilRuler />Edit</a></Button>
                  <Menu>
                    <MenuTrigger asChild><Button variant="ghost" size="icon-sm" aria-label={`More for ${a.name}`}><MoreHorizontal /></Button></MenuTrigger>
                    <MenuContent>
                      <MenuItem onSelect={() => go(`#/test?agent=${encodeURIComponent(a.slug)}`)}><AudioLines />Test call</MenuItem>
                      <MenuItem onSelect={async () => {
                        const cmd = `vk test agents/${a.slug}`;
                        try { await navigator.clipboard.writeText(cmd); toast.success("Copied", { description: cmd }); } catch { toast(cmd); }
                      }}><Copy />Copy the vk test command</MenuItem>
                    </MenuContent>
                  </Menu>
                </>
              )}
            </div>
          </div>
        ))}
      </Card>
    </Page>
  );
}
