import { useEffect, useState } from "react";
import { AudioWaveform, BookOpen, Ear, KeyRound, Server, Tag, Braces, Mail, ShieldCheck, Workflow } from "lucide-react";
import { api } from "@/lib/api";
import { cn } from "@/lib/utils";

/* Each service gets an original monogram badge. To show a company's official logo instead,
   put its file in ~/.config/vpengine/logos/ named after the id (twilio.svg, deepgram.png…). */
type Mono = { text?: string; icon?: typeof Ear; hue: string };
const MONO: Record<string, Mono> = {
  anthropic: { text: "Cl", hue: "#b4583a" },
  openai: { icon: Braces, hue: "#3d5a6c" },
  deepgram: { text: "Dg", hue: "#1f7a6d" },
  fish: { text: "Fi", hue: "#2f6fb0" },
  whisper: { icon: Ear, hue: "#2f8a5c" },
  "whisper-server": { icon: Server, hue: "#55707f" },
  kokoro: { icon: AudioWaveform, hue: "#7457b0" },
  "kokoro-server": { icon: Server, hue: "#55707f" },
  twilio: { text: "Tw", hue: "#b23a48" },
  telnyx: { text: "Tx", hue: "#1d8f6a" },
  secrets: { icon: KeyRound, hue: "#a8720c" },
  workspace: { icon: Tag, hue: "#55707f" },
  telegram: { text: "Tg", hue: "#2a87c9" },
  whatsapp: { text: "Wa", hue: "#1f9d55" },
  email: { icon: Mail, hue: "#7a5c2e" },
  google: { text: "G", hue: "#3b6fd8" },
  mcp: { icon: Workflow, hue: "#6b4fa8" },
  automation: { icon: ShieldCheck, hue: "#a8720c" },
  knowledge: { icon: BookOpen, hue: "#3f7a5a" },
  // more model providers
  vertex: { text: "Vx", hue: "#3b6fd8" }, azure: { text: "Az", hue: "#2f6fb0" }, bedrock: { text: "Br", hue: "#b06a1f" },
  groq: { text: "Gq", hue: "#b4583a" }, mistral: { text: "Mi", hue: "#c2621d" }, deepseek: { text: "Ds", hue: "#3d5fb8" },
  xai: { text: "xA", hue: "#3d4a55" }, together: { text: "To", hue: "#2f6fb0" }, fireworks: { text: "Fw", hue: "#7457b0" },
  openrouter: { text: "Or", hue: "#55707f" }, cerebras: { text: "Cb", hue: "#b23a48" }, nvidia: { text: "Nv", hue: "#5f8a1f" },
  qwen: { text: "Qw", hue: "#6b4fa8" }, sambanova: { text: "Sn", hue: "#a8720c" }, nebius: { text: "Nb", hue: "#1f7a6d" },
  novita: { text: "Nt", hue: "#7457b0" }, ollama: { icon: Server, hue: "#3d4a55" },
  // SIP and contact centers
  jambonz: { text: "Jz", hue: "#1d8f6a" }, genesys: { text: "Gn", hue: "#b4583a" }, avaya: { text: "Av", hue: "#b23a48" },
  "cisco-wxcc": { text: "Wx", hue: "#2a87c9" }, "cisco-ucce": { text: "Uc", hue: "#2a87c9" }, "amazon-connect": { text: "Ac", hue: "#b06a1f" },
  nice: { text: "Nc", hue: "#3d4a55" }, five9: { text: "F9", hue: "#2f6fb0" }, "twilio-flex": { text: "Fx", hue: "#b23a48" },
};

let logos: Promise<string[]> | null = null;
function loadLogos() {
  logos ||= api<{ logos: string[] }>("GET", "/v1/logos").then((r) => r.logos).catch(() => []);
  return logos;
}

export function ProviderIcon({ id, size = "md", className }: { id: string; size?: "sm" | "md" | "lg"; className?: string }) {
  const [file, setFile] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    loadLogos().then((names) => { if (live) setFile(names.find((n) => n.replace(/\.(svg|png|webp)$/, "") === id) || null); });
    return () => { live = false; };
  }, [id]);
  const box = size === "lg" ? "size-12 rounded-xl text-base" : size === "sm" ? "size-7 rounded-md text-[11px]" : "size-10 rounded-lg text-sm";
  if (file) {
    return <span className={cn("grid shrink-0 place-items-center border bg-white p-1.5", box, className)}><img src={`/v1/logos/${file}`} alt="" className="max-h-full max-w-full object-contain" /></span>;
  }
  const m = MONO[id] || { text: id.slice(0, 2), hue: "#55707f" };
  const Icon = m.icon;
  return (
    <span aria-hidden="true" style={{ ["--h" as string]: m.hue }}
      className={cn("grid shrink-0 place-items-center font-semibold tracking-tight text-[var(--h)] ring-1 ring-inset ring-[color-mix(in_srgb,var(--h)_30%,transparent)] bg-[color-mix(in_srgb,var(--h)_12%,var(--card))] dark:text-[color-mix(in_srgb,var(--h)_45%,white)]", box, className)}>
      {Icon ? <Icon className={size === "lg" ? "size-6" : size === "sm" ? "size-3.5" : "size-5"} /> : m.text}
    </span>
  );
}
