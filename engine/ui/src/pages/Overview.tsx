import { ArrowUpRight, AudioLines, BrainCircuit, Cloud, Ear, FileInput, Phone, Server, Workflow } from "lucide-react";
import { api, LLM_LABEL, STT_LABEL, TTS_LABEL, type AgentSummary } from "@/lib/api";
import { useApp, useLoad } from "@/lib/app";
import { Page } from "@/components/shell";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { NewAgentButton } from "./NewAgent";
import { DescribeBox } from "./Describe";

function Status({ icon: Icon, title, ready, readyText, todo, href, action }: {
  icon: typeof Ear; title: string; ready: boolean; readyText: string; todo: string; href: string; action: string;
}) {
  return (
    <Card className="flex flex-col gap-3 p-5">
      <div className="flex items-center gap-3">
        <span className="grid size-9 place-items-center rounded-lg bg-muted text-muted-foreground"><Icon className="size-[18px]" /></span>
        <h3 className="font-semibold">{title}</h3>
        <Badge className="ml-auto" variant={ready ? "ok" : "warn"}>{ready ? "Ready" : "Needs setup"}</Badge>
      </div>
      <p className="flex-1 text-sm text-muted-foreground">{ready ? readyText : todo}</p>
      <Button asChild variant="outline" size="sm" className="self-start"><a href={href}>{ready ? "Change" : action}</a></Button>
    </Card>
  );
}

export function Overview() {
  const { health } = useApp();
  const { data } = useLoad(() => api<{ agents: AgentSummary[] }>("GET", "/v1/agents"));
  const c = health?.config;
  const agents = (data?.agents || []).filter((a) => !a.error);
  return (
    <Page title={`Welcome to ${health?.brand || "Lampline"}`} description="Build voice agents, test them, and take calls, all on your own machine."
      actions={<NewAgentButton variant="outline" />}>
      <section className="mb-8 rounded-2xl border bg-[linear-gradient(180deg,var(--lamp-soft),transparent_70%)] p-6 md:p-8">
        <h2 className="text-xl font-semibold tracking-tight">Describe a voice agent, and we'll build it</h2>
        <p className="mb-4 mt-1 max-w-[65ch] text-sm text-muted-foreground">Say what callers need and what the agent should do. You get a ready-to-test agent with a root agent, sub-agents, transfers and tools, which you can change in the builder.</p>
        <DescribeBox />
        <div className="mt-5 flex flex-wrap items-center gap-x-3 gap-y-2 border-t pt-4">
          <FileInput className="size-4 text-muted-foreground" />
          <span className="text-sm text-muted-foreground">Coming from <span className="font-medium text-foreground">Dialogflow CX</span> or <span className="font-medium text-foreground">CX Agent Studio</span>? Bring your agent over: flows, pages, playbooks and tools.</span>
          <NewAgentButton variant="outline" tab="import"><FileInput />Import an agent</NewAgentButton>
        </div>
      </section>
      {c && (
        <div className="grid gap-4 md:grid-cols-3">
          <Status icon={BrainCircuit} title="Model" ready={c.llm_ready} readyText={`${c.llm_label || LLM_LABEL[c.llm] || c.llm} · ${c.model}`}
            todo="Pick a model provider (Claude, Gemini, OpenAI, Bedrock, Groq, Ollama…) so agents can think." href="#/integrations" action="Set up" />
          <Status icon={Ear} title="Voice" ready={c.voice_ready} readyText={`Listening: ${STT_LABEL[c.stt] || c.stt}. Speaking: ${TTS_LABEL[c.tts] || c.tts}.`}
            todo="Pick how agents listen and speak. Chat tests work without it." href="#/integrations" action="Set up" />
          <Status icon={Phone} title="Phone numbers" ready={!!c.phone_ready} readyText="A phone provider is connected."
            todo="Optional: answer calls to a Twilio or Telnyx number, or over SIP through jambonz." href="#/phone" action="Connect" />
        </div>
      )}
      <section className="mt-8">
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-base font-semibold">Your agents</h2>
          <a href="#/agents" className="text-sm text-muted-foreground hover:text-foreground">See all</a>
        </div>
        <Card className="divide-y overflow-hidden">
          {agents.length === 0 && (
            <div className="grid justify-items-start gap-3 p-6">
              <p className="text-sm text-muted-foreground">No agents yet. Start from a template or a blank canvas.</p>
              <NewAgentButton variant="outline" />
            </div>
          )}
          {agents.slice(0, 6).map((a) => (
            <div key={a.slug} className="flex flex-wrap items-center gap-3 px-5 py-3.5">
              <span className="grid size-9 place-items-center rounded-lg bg-k-agent/12 text-k-agent"><Workflow className="size-[18px]" /></span>
              <a href={`#/build/${encodeURIComponent(a.slug)}`} className="grid min-w-0 flex-1 gap-0.5 hover:underline-offset-4">
                <span className="truncate font-medium">{a.name}</span>
                <span className="truncate font-mono text-xs text-muted-foreground">agents/{a.slug}</span>
              </a>
              <span className="text-xs text-muted-foreground">{a.graph.length} steps</span>
              <Button asChild variant="ghost" size="sm"><a href={`#/test?agent=${encodeURIComponent(a.slug)}`}><AudioLines />Test</a></Button>
              <Button asChild variant="outline" size="sm"><a href={`#/build/${encodeURIComponent(a.slug)}`}>Open<ArrowUpRight /></a></Button>
            </div>
          ))}
        </Card>
      </section>
      <section className="mt-8" aria-labelledby="where-it-runs">
        <h2 id="where-it-runs" className="mb-3 text-base font-semibold">Where it runs</h2>
        <div className="grid gap-4 md:grid-cols-2">
          <Card className="flex items-start gap-3 p-5">
            <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground"><Server className="size-[18px]" /></span>
            <div className="grid gap-1">
              <div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">Local and on-prem</h3><Badge variant="ok">Available</Badge></div>
              <p className="text-sm text-muted-foreground">
                {health?.container ? "Running in Docker on this machine. The same container runs on any server you control" : "Running on this machine. The same engine runs on any server you control, natively or as one Docker container"}, so calls and data stay on your own hardware.
              </p>
            </div>
          </Card>
          <Card className="flex items-start gap-3 p-5">
            <span className="grid size-9 shrink-0 place-items-center rounded-lg bg-muted text-muted-foreground"><Cloud className="size-[18px]" /></span>
            <div className="grid gap-1">
              <div className="flex flex-wrap items-center gap-2"><h3 className="font-semibold">Google Cloud</h3><Badge variant="outline">Coming soon</Badge></div>
              <p className="text-sm text-muted-foreground">A one-command deployment to your own Google Cloud project. Contact-center connections (Genesys, Avaya, Cisco and more) are coming too: see <a className="underline underline-offset-4 hover:text-foreground" href="#/integrations">Integrations</a>.</p>
            </div>
          </Card>
        </div>
      </section>
    </Page>
  );
}
