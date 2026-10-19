import { useEffect, useState } from "react";
import { Sparkles } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/api";
import { useApp } from "@/lib/app";
import { go } from "@/lib/route";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input, Textarea } from "@/components/ui/input";

const EXAMPLES = [
  { label: "Dental front desk", text: "A front desk for a small dental clinic. It books, moves and cancels cleanings and check-ups, answers questions about hours, location and insurance we accept, and takes a message for the billing team." },
  { label: "Restaurant bookings", text: "A phone host for a neighborhood restaurant. It takes table reservations (party size, date, time, name), answers questions about the menu, hours and parking, and handles large-group requests by taking details for the manager." },
  { label: "Order status line", text: "An order-status line for an online garden shop. It looks up an order by order number and tells the caller where it is, handles missing-package reports, and explains the return policy." },
  { label: "IT helpdesk", text: "An after-hours IT helpdesk for a small company. It walks callers through password resets and VPN problems, creates a ticket for anything else, and tells callers when someone will follow up." },
];
const STAGES = ["Reading your description…", "Drafting the root agent and sub-agents…", "Writing instructions and transfers…", "Adding tools and details to collect…", "Checking the result…"];

/** Describe it, AI builds it: the friendly way to start an agent. */
export function DescribeBox({ compact, onDone }: { compact?: boolean; onDone?: () => void }) {
  const { health } = useApp();
  const [text, setText] = useState("");
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [stage, setStage] = useState(0);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!busy) return;
    setStage(0);
    const t = setInterval(() => setStage((s) => Math.min(s + 1, STAGES.length - 1)), 3500);
    return () => clearInterval(t);
  }, [busy]);
  const ready = !!health?.config.llm_ready;
  async function build(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true); setError("");
    try {
      const a = await api<{ slug: string; name: string }>("POST", "/v1/agents/generate", { description: text, name });
      toast.success(`Drafted ${a.name}`, { description: "Review the instructions, then press Test.", duration: 6000 });
      onDone?.();
      go(`#/build/${encodeURIComponent(a.slug)}`);
    } catch (err) { setError((err as Error).message); } finally { setBusy(false); }
  }
  return (
    <form onSubmit={build} className="grid gap-3">
      <Textarea aria-label="Describe your agent" rows={compact ? 5 : 4} value={text} onChange={(e) => setText(e.target.value)} disabled={busy}
        placeholder="What should your agent do? For example: answer calls for a small dental clinic, book and move appointments, and answer questions about hours and insurance."
        className={cn("text-[15px]", !compact && "min-h-28 bg-card")} />
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs text-muted-foreground">Try:</span>
        {EXAMPLES.map((x) => (
          <button key={x.label} type="button" disabled={busy} onClick={() => setText(x.text)}
            className="cursor-pointer rounded-full border bg-card px-2.5 py-1 text-xs hover:border-ring/60 hover:bg-accent disabled:opacity-50">{x.label}</button>
        ))}
      </div>
      <div className="flex flex-wrap items-center gap-3">
        {compact && <Input aria-label="Agent name (optional)" placeholder="Name (optional)" className="w-56" value={name} onChange={(e) => setName(e.target.value)} disabled={busy} maxLength={80} />}
        <Button type="submit" disabled={busy || text.trim().length < 12 || !ready} variant={compact ? "default" : "lamp"} size={compact ? "default" : "lg"}>
          <Sparkles className={cn(busy && "animate-pulse")} />{busy ? "Building…" : "Build my agent"}
        </Button>
        <span className="text-[13px] text-muted-foreground" role="status">
          {busy ? STAGES[stage] : !ready ? <>Add a model key in <a className="underline" href="#/integrations">Integrations</a> first.</> : "Your model drafts it; you review and test it in the builder."}
        </span>
      </div>
      {error && <p className="text-sm text-bad">{error}</p>}
    </form>
  );
}
