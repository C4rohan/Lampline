import { local } from "./utils";

export type Config = {
  llm: string; model: string; llm_ready: boolean; stt: string; tts: string; stt_ready: boolean; tts_ready: boolean;
  voice_ready: boolean; tts_voice?: string; phone_ready?: boolean; twilio_ready?: boolean; telnyx_ready?: boolean; jambonz_ready?: boolean;
  llm_label?: string; [k: string]: unknown;
};
export type Health = { ok: boolean; version: string; auth_required: boolean; container?: boolean; brand: string; phone_webhook: string | null;
  telnyx_webhook: string | null; jambonz_webhook?: string | null; config: Config };
export type GraphStep = { id?: string; name: string; kind: string; greeting?: boolean; extracts?: boolean;
  routes: { to: string; label: string; condition: string }[] };
export type AgentSummary = { slug: string; name: string; graph: GraphStep[]; has_persona?: boolean; tests: number; error?: string };
export type Finding = { code: string; severity: "error" | "warning"; message: string; where: string };
export type FieldInfo = { label: string; source: string; env: string; choices?: string[]; value?: string; set?: boolean; hint?: string };
export type LLMProviderInfo = { name: string; label: string; tag: string; blurb: string; model: string; key: string; fields: string[]; key_hint: string };
export type Settings = { file: string; fields: Record<string, FieldInfo>; status: Record<string, any>; llm_providers?: LLMProviderInfo[] };

let key = local.get("vpe-key") || "";
// A link like http://localhost:7860/#/?key=… (printed by scripts/docker-up.sh) signs this browser in once.
// The key is in the fragment, so it never reaches the server's logs; it's removed from the address right away.
function keyFromAddress(): boolean {
  const m = location.hash.match(/[?&]key=([^&]+)/);
  if (!m) return false;
  key = decodeURIComponent(m[1]);
  local.set("vpe-key", key);
  history.replaceState(null, "", location.pathname + location.search + location.hash.replace(/[?&]key=[^&]+/, "").replace(/\?$/, ""));
  return true;
}
keyFromAddress();
window.addEventListener("hashchange", () => { if (keyFromAddress()) window.dispatchEvent(new Event("vpe-key")); });
let onAuth: (() => void) | null = null;
export const auth = {
  get key() { return key; },
  set(k: string) { key = k; local.set("vpe-key", k || null); },
  onMissing(fn: () => void) { onAuth = fn; },
};

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) { super(message); this.status = status; }
}

export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const raw = body instanceof Blob;
  if (body !== undefined) headers["Content-Type"] = raw ? "application/zip" : "application/json";
  if (key) headers["X-API-Key"] = key;
  const r = await fetch(path, { method, headers, body: body === undefined ? undefined : raw ? body : JSON.stringify(body) });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401) { onAuth?.(); throw new ApiError("This engine needs its API key.", 401); }
  if (!r.ok) throw new ApiError(typeof data.detail === "string" ? data.detail : `${r.status} ${r.statusText}`, r.status);
  return data as T;
}

/** Fallback names; the engine reports the label of the provider in use (config.llm_label). */
export const LLM_LABEL: Record<string, string> = { anthropic: "Claude (Anthropic)", openai: "OpenAI-compatible" };
export const STT_LABEL: Record<string, string> = { deepgram: "Deepgram", fish: "Fish Audio", whisper: "Whisper on this computer (free)", moonshine: "Moonshine on this computer (free, streaming)", "whisper-server": "Whisper server (advanced)" };
export const TTS_LABEL: Record<string, string> = { deepgram: "Deepgram", fish: "Fish Audio", kokoro: "Kokoro on this computer (free)", "kokoro-server": "Kokoro server (advanced)" };

/** Download a file from the engine (sends the API key, which a plain link can't). */
export async function download(path: string, fallbackName: string): Promise<string | null> {
  const headers: Record<string, string> = {};
  if (key) headers["X-API-Key"] = key;
  const r = await fetch(path, { headers });
  if (!r.ok) {
    const data = await r.json().catch(() => ({}));
    throw new ApiError(typeof data.detail === "string" ? data.detail : `${r.status} ${r.statusText}`, r.status);
  }
  const name = /filename="([^"]+)"/.exec(r.headers.get("content-disposition") || "")?.[1] || fallbackName;
  const url = URL.createObjectURL(await r.blob());
  const a = Object.assign(document.createElement("a"), { href: url, download: name });
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 5000);
  return r.headers.get("x-export-notes");
}
