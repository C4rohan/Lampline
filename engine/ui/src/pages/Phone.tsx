import { useState } from "react";
import { Check, Copy, Info, Phone } from "lucide-react";
import { api, type AgentSummary, type Settings } from "@/lib/api";
import { useApp, useLoad } from "@/lib/app";
import { Notice, Page } from "@/components/shell";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/label";
import { f, SettingsCard } from "./settings";

function Webhook({ label, base, agent, who }: { label: string; base: string | null; agent: string; who: string }) {
  const [copied, setCopied] = useState(false);
  const url = base ? `${base}?agent=${agent}` : null;
  return (
    <Field label={label} hint={url ? "Change the agent at the end to any folder name." : undefined}>
      {url ? (
        <div className="flex items-center gap-2 rounded-md border bg-muted/50 py-1 pl-3 pr-1">
          <code className="min-w-0 flex-1 truncate text-[13px]">{url}</code>
          <Button size="sm" variant="outline" onClick={async () => { try { await navigator.clipboard.writeText(url); setCopied(true); } catch { /* ignore */ } }}>
            {copied ? <Check /> : <Copy />}{copied ? "Copied" : "Copy"}
          </Button>
        </div>
      ) : (
        <Notice><Info className="mt-0.5 size-4 shrink-0" /><span>Start the engine with <code className="text-foreground">scripts/engine-up.sh --tunnel</code> to get a public address {who} can reach.</span></Notice>
      )}
    </Field>
  );
}

function Steps({ items }: { items: React.ReactNode[] }) {
  return (
    <ol className="grid gap-2.5">
      {items.map((it, i) => (
        <li key={i} className="flex gap-3 text-sm">
          <span className="grid size-5 shrink-0 place-items-center rounded-full bg-muted text-[11px] font-semibold text-muted-foreground">{i + 1}</span>
          <span className="leading-relaxed">{it}</span>
        </li>
      ))}
    </ol>
  );
}

export function PhoneNumbers() {
  const { health } = useApp();
  const { data: settings, setData } = useLoad(() => api<Settings>("GET", "/v1/settings"));
  const { data: agents } = useLoad(() => api<{ agents: AgentSummary[] }>("GET", "/v1/agents"));
  const first = agents?.agents.find((a) => !a.error)?.slug || "front-desk";
  if (!settings || !health) return <Page title="Phone numbers"><p className="text-sm text-muted-foreground">Loading…</p></Page>;
  return (
    <Page title="Phone numbers" description="Take real phone calls with your agents: a Twilio or Telnyx number, or any SIP trunk, phone system or SBC through jambonz.">
      <div className="grid items-start gap-6 xl:grid-cols-2">
        <SettingsCard settings={settings} onSaved={setData} icon={Phone} title="Twilio" lead="Answer calls to a Twilio number with any of your agents."
          ready={(s) => s.twilio_ready} groups={() => [f("twilio_auth_token", { hint: "Twilio Console → Account info → Auth token. Used to check that calls really come from Twilio, and to hang up." })]}
          extra={<div className="grid gap-5">
            <Webhook label="Webhook for “A call comes in” (HTTP POST)" base={health.phone_webhook} agent={first} who="Twilio" />
            <Steps items={[
              "Buy a voice number in the Twilio Console (US: $1.15/month, plus per-minute charges).",
              "Paste the auth token above and save.",
              <>Run <code>scripts/engine-up.sh --tunnel</code> and copy the webhook.</>,
              "In Twilio: Phone Numbers → your number → Voice → “A call comes in” → Webhook, paste it, HTTP POST, save.",
              "Call the number.",
            ]} />
          </div>} />
        <SettingsCard settings={settings} onSaved={setData} icon={Phone} title="Telnyx" lead="Answer calls to a Telnyx number through a TeXML application."
          ready={(s) => s.telnyx_ready} groups={() => [
            f("telnyx_public_key", { hint: "Mission Control Portal → Keys & Credentials → Public Key. Used to check that calls really come from Telnyx." }),
            f("telnyx_api_key", { hint: "Optional: lets the engine hang up when the agent finishes. Without it the caller hangs up." })]}
          extra={<div className="grid gap-5">
            <Webhook label="Telnyx voice URL (HTTP POST)" base={health.telnyx_webhook} agent={first} who="Telnyx" />
            <Steps items={[
              "Buy a voice number in the Telnyx Mission Control Portal.",
              "Paste the public key (and optionally an API key) above and save.",
              <>Run <code>scripts/engine-up.sh --tunnel</code> and copy the voice URL.</>,
              "Create a TeXML Application, set its voice URL to the address above with method POST, and assign your number to it.",
              "Call the number.",
            ]} />
          </div>} />
        <SettingsCard settings={settings} onSaved={setData} icon={Phone} title="jambonz (SIP)" lead="Answer calls from any SIP trunk, PBX or session border controller through jambonz, the open-source voice gateway."
          ready={(s) => s.jambonz_ready} groups={() => [
            f("jambonz_webhook_secret", { hint: "In jambonz: Account → Webhook secret. Used to check that calls really come from your jambonz." })]}
          extra={<div className="grid gap-5">
            <Webhook label="Calling webhook (HTTP POST)" base={health.jambonz_webhook ?? null} agent={first} who="jambonz" />
            <Steps items={[
              "Run jambonz (self-hosted or jambonz.cloud) and add your SIP trunk, phone system or SBC as a carrier.",
              "Paste the account's webhook secret above and save.",
              <>Run <code>scripts/engine-up.sh --tunnel</code>, or set <code>VPE_PUBLIC_URL</code> to an address jambonz can reach, and copy the calling webhook.</>,
              "In jambonz, create an application: calling webhook = the address above (POST), call status webhook = the same address ending in /v1/jambonz/done instead of /v1/jambonz/call. Route your number to it.",
              "Call the number. When the agent ends the call, jambonz hangs up.",
            ]} />
          </div>} />
      </div>
    </Page>
  );
}
