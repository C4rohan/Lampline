import { useEffect, useRef, useState } from "react";
import { CheckCircle2, Loader2, Sparkles, Square, XCircle } from "lucide-react";
import { toast } from "sonner";
import { api, type AgentSummary } from "@/lib/api";
import { Notice } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NativeSelect } from "@/components/ui/input";
import { Field } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/switch";

type Case = { id?: string; name: string; goal: string; criteria: string[] };
type Criterion = { criterion: string; passed: boolean; reason: string };
type CaseResult = { case_id: string; name: string; verdict: string; results: { goal_met: boolean; criteria: Criterion[] } };
type Score = { cases: number; passed: number; checks: number; checks_total: number };
type Edit = { step_id: string; name: string; before: string; prompt: string; why: string };
type Job = {
  id: string; agent: string; status: "running" | "done" | "error" | "cancelled"; stage: string; log: string[];
  baseline: { score: Score; results: CaseResult[] } | null; best: { score: Score; results: CaseResult[] } | null;
  attempts: { round: number; kept: boolean; notes: string; score: Score | null; edits: Edit[] }[];
  edits: Edit[]; improved: boolean; error: string; applied: boolean;
};

const MAX_CASES = 10;
const scoreText = (s: Score) => `${s.passed} of ${s.cases} case${s.cases === 1 ? "" : "s"} pass · ${s.checks} of ${s.checks_total} checks`;

/* Improve an agent from its test cases: the engine runs them as text chats against copies of the agent, asks the
   model for prompt changes where checks failed, and keeps a change only if more checks pass and nothing breaks. */
export function ImproveAgent({ agents, cases }: { agents: AgentSummary[]; cases: Case[] }) {
  const targets = agents.filter((a) => !/test.?caller/i.test(a.slug + " " + a.name));
  const [agent, setAgent] = useState("");
  const [picked, setPicked] = useState<Record<string, boolean>>({});
  const [rounds, setRounds] = useState("2");
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const poll = useRef<number | undefined>(undefined);
  const slug = agent && targets.some((a) => a.slug === agent) ? agent : (targets[0]?.slug || "");
  const chosen = cases.filter((c) => picked[c.id!] ?? true).slice(0, MAX_CASES);
  const running = job?.status === "running";

  useEffect(() => () => window.clearInterval(poll.current), []);

  function follow(id: string) {
    window.clearInterval(poll.current);
    poll.current = window.setInterval(async () => {
      try {
        const j = await api<Job>("GET", `/v1/improve/${id}`);
        setJob(j);
        if (j.status !== "running") window.clearInterval(poll.current);
      } catch { /* try again on the next tick */ }
    }, 1000);
  }

  async function start() {
    setBusy(true);
    try {
      const j = await api<Job>("POST", `/v1/agents/${slug}/improve`, { case_ids: chosen.map((c) => c.id), rounds: Number(rounds) });
      setJob(j);
      follow(j.id);
    } catch (e) { toast.error((e as Error).message); } finally { setBusy(false); }
  }

  async function stop() {
    if (!job) return;
    try { setJob(await api<Job>("POST", `/v1/improve/${job.id}/stop`)); } catch (e) { toast.error((e as Error).message); }
  }

  async function apply() {
    if (!job) return;
    setBusy(true);
    try {
      await api("POST", `/v1/improve/${job.id}/apply`);
      setJob({ ...job, applied: true });
      toast.success("Saved. The previous version is in the agent's history.");
    } catch (e) { toast.error((e as Error).message); } finally { setBusy(false); }
  }

  const before = Object.fromEntries((job?.baseline?.results || []).map((r) => [r.case_id, r.verdict]));

  return (
    <Card className="overflow-hidden">
      <div className="flex items-center gap-2 border-b px-4 py-3">
        <h3 className="flex items-center gap-2 text-sm font-semibold"><Sparkles className="size-4 text-muted-foreground" />Improve an agent</h3>
        <Badge variant="lamp" className="ml-auto">New</Badge>
      </div>
      <div className="grid gap-4 p-4">
        <p className="text-[13px] text-muted-foreground">
          Runs your test cases as text chats against a copy of the agent, then asks your model to rewrite the prompts of the steps
          where checks failed. A change is kept only if more checks pass and no passing case breaks. Tools are simulated, and nothing
          is saved until you apply it.
        </p>
        {!cases.length
          ? <Notice>Add test cases first (Import test scripts, above). Each needs who is calling, what they want, and checks.</Notice>
          : (
            <>
              <div className="grid gap-4 sm:grid-cols-[1fr_140px]">
                <Field label="Agent" htmlFor="im-agent">
                  <NativeSelect id="im-agent" value={slug} onChange={(e) => setAgent(e.target.value)} disabled={running}>
                    {targets.map((a) => <option key={a.slug} value={a.slug}>{a.name}</option>)}
                  </NativeSelect>
                </Field>
                <Field label="Rounds" htmlFor="im-rounds">
                  <NativeSelect id="im-rounds" value={rounds} onChange={(e) => setRounds(e.target.value)} disabled={running}>
                    {["1", "2", "3"].map((n) => <option key={n} value={n}>{n}</option>)}
                  </NativeSelect>
                </Field>
              </div>
              <fieldset className="grid gap-1.5" disabled={running}>
                <legend className="mb-1.5 text-sm font-medium">Test cases <span className="font-normal text-muted-foreground">(up to {MAX_CASES})</span></legend>
                <div className="grid max-h-40 gap-1.5 overflow-y-auto">
                  {cases.map((c) => (
                    <label key={c.id} className="flex items-center gap-2 text-sm">
                      <Checkbox checked={picked[c.id!] ?? true} onCheckedChange={(v) => setPicked((p) => ({ ...p, [c.id!]: !!v }))} />
                      <span className="truncate">{c.name}</span>
                    </label>
                  ))}
                </div>
                {cases.filter((c) => picked[c.id!] ?? true).length > MAX_CASES && (
                  <p className="text-[12.5px] text-muted-foreground">Only the first {MAX_CASES} chosen cases run.</p>)}
              </fieldset>
              {running
                ? <Button variant="outline" onClick={stop}><Square />Stop</Button>
                : <Button onClick={start} disabled={busy || !slug || !chosen.length}>{busy ? <Loader2 className="animate-spin" /> : <Sparkles />}Improve</Button>}
            </>
          )}

        {job && (
          <div className="grid gap-3 border-t pt-4" aria-live="polite">
            <p className="flex items-center gap-2 text-sm">
              {running && <Loader2 className="size-4 animate-spin text-muted-foreground" />}
              <span>{job.stage}</span>
            </p>
            {job.error && <Notice tone="bad">{job.error}</Notice>}
            {job.baseline && (
              <div className="grid gap-1 text-sm">
                <p><strong className="font-medium">As it is:</strong> {scoreText(job.baseline.score)}</p>
                {job.improved && job.best && <p><strong className="font-medium">With the changes:</strong> {scoreText(job.best.score)}</p>}
              </div>
            )}
            {job.best && job.status !== "running" && (
              <ul className="grid gap-1 text-sm">
                {job.best.results.map((r) => (
                  <li key={r.case_id} className="flex items-center gap-2">
                    {r.verdict === "passed" ? <CheckCircle2 className="size-4 shrink-0 text-ok" /> : <XCircle className="size-4 shrink-0 text-bad" />}
                    <span className="truncate">{r.name}</span>
                    {job.improved && before[r.case_id] !== r.verdict && <Badge variant="ok">fixed</Badge>}
                  </li>
                ))}
              </ul>
            )}
            {job.improved && job.edits.map((e) => (
              <div key={e.step_id} className="grid gap-2 rounded-lg border p-3">
                <p className="text-sm font-medium">Step: {e.name}</p>
                {e.why && <p className="text-[12.5px] text-muted-foreground">{e.why}</p>}
                <div className="grid gap-2 md:grid-cols-2">
                  <pre className="whitespace-pre-wrap rounded-md font-sans border border-bad/30 bg-bad/5 p-2.5 text-[12.5px] leading-relaxed"><span className="mb-1 block text-[11px] font-medium uppercase tracking-wide text-muted-foreground">Before</span>{e.before || "(no prompt)"}</pre>
                  <pre className="whitespace-pre-wrap rounded-md font-sans border border-ok/30 bg-ok/5 p-2.5 text-[12.5px] leading-relaxed"><span className="mb-1 block text-[11px] font-medium uppercase tracking-wide text-muted-foreground">After</span>{e.prompt}</pre>
                </div>
              </div>
            ))}
            {job.status === "done" && job.improved && (
              job.applied
                ? <p className="flex items-center gap-2 text-sm text-ok"><CheckCircle2 className="size-4" />Applied to the agent.</p>
                : <Button onClick={apply} disabled={busy}>{busy ? <Loader2 className="animate-spin" /> : <CheckCircle2 />}Apply the changes</Button>
            )}
            {job.attempts.some((a) => !a.kept) && (
              <details className="text-[12.5px] text-muted-foreground">
                <summary className="cursor-pointer">Changes tried and not kept</summary>
                <ul className="mt-2 grid gap-1">
                  {job.attempts.filter((a) => !a.kept).map((a) => (
                    <li key={a.round}>Round {a.round}: {a.edits.length ? `${a.edits.map((e) => e.name).join(", ")}${a.score ? ` (${a.score.checks} of ${a.score.checks_total} checks)` : ""}` : "no usable change suggested"}</li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        )}
      </div>
    </Card>
  );
}
