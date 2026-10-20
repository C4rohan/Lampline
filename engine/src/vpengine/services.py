"""Model and speech services.

Settings come from three places, later ones winning: built-in defaults, the
environment (or .env via scripts/engine-up.sh), and the Integrations page,
which saves to a file outside the repo (see settings.py). Keys are never read
from or written to agent folders.

    Thinking (LLM):   anthropic (Claude) | openai (any OpenAI-compatible endpoint) | google (Gemini) |
                      vertex | azure | bedrock | groq | mistral | deepseek | xai | together | fireworks |
                      openrouter | cerebras | nvidia | qwen | sambanova | nebius | novita | ollama
                      (LLM_PROVIDERS below; each runs through Pipecat's own service for it)
    Listening (STT):  deepgram | fish (Fish Audio) | whisper (built in, free) | whisper-server (an OpenAI-style server)
    Speaking (TTS):   deepgram | fish (Fish Audio) | kokoro (built in, free) | kokoro-server (Kokoro-FastAPI)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields
from typing import Any, AsyncGenerator

from pipecat.services.llm_service import LLMService
from pipecat.services.tts_service import TTSService


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class FieldSpec:
    name: str
    env: str
    label: str
    secret: bool = False
    choices: tuple[str, ...] = ()


# Fish Audio text-to-speech models (from Fish's API schema and price list, October 2026).
FISH_TTS_MODELS = ("s2.1-pro-free", "s2.1-pro", "s2-pro", "drama-3", "s1")

# Numeric settings: (minimum, maximum). Empty means the provider's default.
RANGES = {"turn_pause": (0.2, 6.0), "fish_speed": (0.5, 2.0), "fish_volume": (-20.0, 20.0), "fish_temperature": (0.0, 1.0),
          "fish_top_p": (0.0, 1.0)}


@dataclass(frozen=True)
class LLMProvider:
    """A model provider: how to build it and what it needs. The UI lists these as tiles."""
    name: str                       # VPE_LLM value
    label: str
    tag: str                        # one line under the name
    blurb: str
    model: str                      # default model when none is chosen
    module: str = ""                # pipecat module and class (OpenAI-style providers)
    cls: str = ""
    key: str = ""                   # Config field holding the API key
    fields: tuple[str, ...] = ()    # other Config fields the tile shows
    base_url: str = ""              # OpenAI-style endpoint, used by "Test connection"
    key_hint: str = ""


LLM_PROVIDERS: tuple[LLMProvider, ...] = (
    LLMProvider("anthropic", "Claude", "Anthropic", "Claude models. Haiku is fast and inexpensive for calls.",
                "claude-haiku-5-5", key="anthropic_key"),
    LLMProvider("openai", "OpenAI-compatible", "OpenAI or any compatible endpoint",
                "OpenAI, a gateway, or a model running on this computer.", "gpt-4.1-mini",
                key="openai_key", fields=("base_url",)),
    LLMProvider("google", "Gemini", "Google AI Studio", "Gemini models with an API key from Google AI Studio.",
                "gemini-3.6-flash", key="gemini_key", key_hint="aistudio.google.com → Get API key."),
    LLMProvider("vertex", "Gemini on Vertex AI", "Google Cloud", "Gemini through your Google Cloud project.",
                "gemini-3.6-flash", fields=("vertex_project", "vertex_location", "vertex_credentials")),
    LLMProvider("azure", "Azure OpenAI", "Microsoft Azure", "OpenAI models deployed in your Azure resource.",
                "gpt-4.1", key="azure_key", fields=("azure_endpoint",),
                key_hint="Azure portal → your OpenAI resource → Keys and Endpoint. The model is your deployment's name."),
    LLMProvider("bedrock", "Amazon Bedrock", "AWS", "Claude, Nova, Llama and more through your AWS account.",
                "us.amazon.nova-lite-v1:0", key="aws_secret_access_key", fields=("aws_access_key_id", "aws_region"),
                key_hint="An IAM user's keys, or leave both empty to use this computer's AWS credentials."),
    LLMProvider("groq", "Groq", "Fast open models", "Open models on Groq's very fast hardware.",
                "openai/gpt-oss-120b", "pipecat.services.groq.llm", "GroqLLMService", "groq_key",
                base_url="https://api.groq.com/openai/v1", key_hint="console.groq.com → API keys."),
    LLMProvider("mistral", "Mistral", "Mistral AI", "Mistral's models, from small and fast to large.",
                "mistral-small-latest", "pipecat.services.mistral.llm", "MistralLLMService", "mistral_key",
                base_url="https://api.mistral.ai/v1", key_hint="console.mistral.ai → API keys."),
    LLMProvider("deepseek", "DeepSeek", "DeepSeek", "DeepSeek's chat models.",
                "deepseek-flash", "pipecat.services.deepseek.llm", "DeepSeekLLMService", "deepseek_key",
                base_url="https://api.deepseek.com/v1", key_hint="platform.deepseek.com → API keys."),
    LLMProvider("xai", "Grok", "xAI", "xAI's Grok models.",
                "grok-4.20-non-reasoning", "pipecat.services.xai.llm", "GrokLLMService", "xai_key",
                base_url="https://api.x.ai/v1", key_hint="console.x.ai → API keys."),
    LLMProvider("together", "Together AI", "Open models", "Hundreds of open models behind one API.",
                "zai-org/GLM-5.2", "pipecat.services.together.llm", "TogetherLLMService", "together_key",
                base_url="https://api.together.xyz/v1"),
    LLMProvider("fireworks", "Fireworks AI", "Open models", "Fast inference for open models.",
                "accounts/fireworks/models/nemotron-3-ultra-nvfp4", "pipecat.services.fireworks.llm",
                "FireworksLLMService", "fireworks_key", base_url="https://api.fireworks.ai/inference/v1"),
    LLMProvider("openrouter", "OpenRouter", "Many providers, one key", "Route to models from many providers with one key.",
                "openai/gpt-4.1", "pipecat.services.openrouter.llm", "OpenRouterLLMService", "openrouter_key",
                base_url="https://openrouter.ai/api/v1"),
    LLMProvider("cerebras", "Cerebras", "Fast open models", "Open models on Cerebras' wafer-scale chips.",
                "gpt-oss-120b", "pipecat.services.cerebras.llm", "CerebrasLLMService", "cerebras_key",
                base_url="https://api.cerebras.ai/v1"),
    LLMProvider("nvidia", "NVIDIA NIM", "NVIDIA", "Models hosted on NVIDIA's API catalog.",
                "nvidia/nemotron-3-super-120b-a12b", "pipecat.services.nvidia.llm", "NvidiaLLMService", "nvidia_key",
                base_url="https://integrate.api.nvidia.com/v1", key_hint="build.nvidia.com → Get API key."),
    LLMProvider("qwen", "Qwen", "Alibaba Cloud Model Studio", "Alibaba's Qwen models.",
                "qwen-plus", "pipecat.services.qwen.llm", "QwenLLMService", "qwen_key",
                base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
    LLMProvider("sambanova", "SambaNova", "Fast open models", "Open models on SambaNova's chips.",
                "Meta-Llama-3.3-70B-Instruct", "pipecat.services.sambanova.llm", "SambaNovaLLMService", "sambanova_key",
                base_url="https://api.sambanova.ai/v1"),
    LLMProvider("nebius", "Nebius", "Nebius Token Factory", "Open models hosted by Nebius.",
                "Qwen/Qwen3-30B-A3B-Instruct-2507", "pipecat.services.nebius.llm", "NebiusLLMService", "nebius_key",
                base_url="https://api.tokenfactory.nebius.com/v1"),
    LLMProvider("novita", "Novita AI", "Open models", "Open models hosted by Novita.",
                "moonshotai/kimi-k2.5", "pipecat.services.novita.llm", "NovitaLLMService", "novita_key",
                base_url="https://api.novita.ai/openai"),
    LLMProvider("ollama", "Ollama", "On this computer · free", "Open models running on this computer or your own server.",
                "qwen3:8b", "pipecat.services.ollama.llm", "OLLamaLLMService", fields=("ollama_url",)),
)
LLM_BY_NAME = {p.name: p for p in LLM_PROVIDERS}


def default_model(llm: str) -> str:
    p = LLM_BY_NAME.get(llm)
    return p.model if p else ""


# Every setting, the environment variable it can come from, and how the UI labels it.
SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("llm", "VPE_LLM", "Model provider", choices=tuple(p.name for p in LLM_PROVIDERS)),
    FieldSpec("model", "VPE_LLM_MODEL", "Model"),
    FieldSpec("anthropic_key", "ANTHROPIC_API_KEY", "Anthropic API key", secret=True),
    FieldSpec("openai_key", "OPENAI_API_KEY", "API key", secret=True),
    FieldSpec("base_url", "VPE_LLM_BASE_URL", "Base URL"),
    FieldSpec("gemini_key", "GEMINI_API_KEY", "Gemini API key", secret=True),
    FieldSpec("vertex_project", "GOOGLE_CLOUD_PROJECT", "Google Cloud project ID"),
    FieldSpec("vertex_location", "VPE_VERTEX_LOCATION", "Vertex AI location"),
    FieldSpec("vertex_credentials", "VPE_VERTEX_CREDENTIALS", "Service account key (JSON)", secret=True),
    FieldSpec("azure_key", "AZURE_OPENAI_API_KEY", "Azure OpenAI API key", secret=True),
    FieldSpec("azure_endpoint", "AZURE_OPENAI_ENDPOINT", "Endpoint"),
    FieldSpec("aws_access_key_id", "AWS_ACCESS_KEY_ID", "AWS access key ID"),
    FieldSpec("aws_secret_access_key", "AWS_SECRET_ACCESS_KEY", "AWS secret access key", secret=True),
    FieldSpec("aws_region", "AWS_REGION", "AWS region"),
    FieldSpec("groq_key", "GROQ_API_KEY", "Groq API key", secret=True),
    FieldSpec("mistral_key", "MISTRAL_API_KEY", "Mistral API key", secret=True),
    FieldSpec("deepseek_key", "DEEPSEEK_API_KEY", "DeepSeek API key", secret=True),
    FieldSpec("xai_key", "XAI_API_KEY", "xAI API key", secret=True),
    FieldSpec("together_key", "TOGETHER_API_KEY", "Together AI API key", secret=True),
    FieldSpec("fireworks_key", "FIREWORKS_API_KEY", "Fireworks AI API key", secret=True),
    FieldSpec("openrouter_key", "OPENROUTER_API_KEY", "OpenRouter API key", secret=True),
    FieldSpec("cerebras_key", "CEREBRAS_API_KEY", "Cerebras API key", secret=True),
    FieldSpec("nvidia_key", "NVIDIA_API_KEY", "NVIDIA API key", secret=True),
    FieldSpec("qwen_key", "DASHSCOPE_API_KEY", "Model Studio (DashScope) API key", secret=True),
    FieldSpec("sambanova_key", "SAMBANOVA_API_KEY", "SambaNova API key", secret=True),
    FieldSpec("nebius_key", "NEBIUS_API_KEY", "Nebius API key", secret=True),
    FieldSpec("novita_key", "NOVITA_API_KEY", "Novita AI API key", secret=True),
    FieldSpec("ollama_url", "OLLAMA_BASE_URL", "Ollama address"),
    FieldSpec("temperature", "VPE_LLM_TEMPERATURE", "Temperature"),
    FieldSpec("stt", "VPE_STT", "Speech to text", choices=("deepgram", "fish", "whisper", "moonshine", "whisper-server")),
    FieldSpec("deepgram_key", "DEEPGRAM_API_KEY", "Deepgram API key", secret=True),
    FieldSpec("stt_model", "VPE_STT_MODEL", "Deepgram model"),
    FieldSpec("whisper_url", "VPE_WHISPER_URL", "Whisper server URL"),
    FieldSpec("whisper_model", "VPE_WHISPER_MODEL", "Whisper server model"),
    FieldSpec("moonshine_model", "VPE_MOONSHINE_MODEL", "Moonshine model", choices=("small-streaming", "tiny-streaming")),
    FieldSpec("local_whisper_model", "VPE_LOCAL_WHISPER_MODEL", "Whisper model", choices=("tiny.en", "base.en", "small.en", "distil-small.en")),
    FieldSpec("turn_detection", "VPE_TURN_DETECTION", "How the agent knows the caller has finished", choices=("smart", "pause")),
    FieldSpec("turn_pause", "VPE_TURN_PAUSE", "Pause in seconds"),
    FieldSpec("tts", "VPE_TTS", "Text to speech", choices=("deepgram", "fish", "kokoro", "kokoro-server")),
    FieldSpec("tts_voice", "VPE_TTS_VOICE", "Deepgram voice"),
    FieldSpec("fish_key", "FISH_API_KEY", "Fish Audio API key", secret=True),
    FieldSpec("fish_voice", "VPE_FISH_VOICE", "Fish Audio voice ID"),
    FieldSpec("fish_model", "VPE_FISH_MODEL", "Voice model", choices=FISH_TTS_MODELS),
    FieldSpec("fish_latency", "VPE_FISH_LATENCY", "Latency", choices=("balanced", "normal", "low")),
    FieldSpec("fish_connection", "VPE_FISH_CONNECTION", "Connection", choices=("auto", "live", "http")),
    FieldSpec("fish_speed", "VPE_FISH_SPEED", "Speed"),
    FieldSpec("fish_volume", "VPE_FISH_VOLUME", "Volume (dB)"),
    FieldSpec("fish_temperature", "VPE_FISH_TEMPERATURE", "Temperature"),
    FieldSpec("fish_top_p", "VPE_FISH_TOP_P", "Top P"),
    FieldSpec("fish_normalize", "VPE_FISH_NORMALIZE", "Read numbers and dates as words", choices=("on", "off")),
    FieldSpec("fish_stt_model", "VPE_FISH_STT_MODEL", "Transcription model", choices=("transcribe-1", "transcribe-1-pro")),
    FieldSpec("fish_stt_language", "VPE_FISH_STT_LANGUAGE", "Caller language"),
    FieldSpec("kokoro_url", "VPE_KOKORO_URL", "Kokoro server URL"),
    FieldSpec("kokoro_voice", "VPE_KOKORO_VOICE", "Kokoro voice"),
    FieldSpec("twilio_auth_token", "TWILIO_AUTH_TOKEN", "Twilio auth token", secret=True),
    FieldSpec("telnyx_api_key", "TELNYX_API_KEY", "Telnyx API key", secret=True),
    FieldSpec("telnyx_public_key", "TELNYX_PUBLIC_KEY", "Telnyx public key"),
    FieldSpec("twilio_account_sid", "TWILIO_ACCOUNT_SID", "Twilio account SID"),
    FieldSpec("twilio_from_number", "TWILIO_FROM_NUMBER", "Twilio number for outbound calls"),
    FieldSpec("telnyx_texml_app_id", "TELNYX_TEXML_APP_ID", "Telnyx TeXML application ID"),
    FieldSpec("telnyx_from_number", "TELNYX_FROM_NUMBER", "Telnyx number for outbound calls"),
    FieldSpec("jambonz_webhook_secret", "JAMBONZ_WEBHOOK_SECRET", "jambonz webhook secret", secret=True),
    FieldSpec("jambonz_sample_rate", "VPE_JAMBONZ_SAMPLE_RATE", "Call audio", choices=("8000", "16000")),
    # messaging and email
    FieldSpec("telegram_bot_token", "TELEGRAM_BOT_TOKEN", "Telegram bot token", secret=True),
    FieldSpec("whatsapp_token", "WHATSAPP_ACCESS_TOKEN", "WhatsApp access token", secret=True),
    FieldSpec("whatsapp_phone_number_id", "WHATSAPP_PHONE_NUMBER_ID", "WhatsApp phone number ID"),
    FieldSpec("whatsapp_app_secret", "WHATSAPP_APP_SECRET", "Meta app secret", secret=True),
    FieldSpec("whatsapp_verify_token", "WHATSAPP_VERIFY_TOKEN", "Webhook verify token", secret=True),
    FieldSpec("email_provider", "VPE_EMAIL_PROVIDER", "Send and read email with", choices=("smtp", "gmail")),
    FieldSpec("email_address", "VPE_EMAIL_ADDRESS", "Email address"),
    FieldSpec("email_password", "VPE_EMAIL_PASSWORD", "Email password or app password", secret=True),
    FieldSpec("smtp_host", "VPE_SMTP_HOST", "SMTP server"),
    FieldSpec("smtp_port", "VPE_SMTP_PORT", "SMTP port"),
    FieldSpec("imap_host", "VPE_IMAP_HOST", "IMAP server"),
    # Google (Calendar and Gmail): your own OAuth client, "Desktop app" type
    FieldSpec("google_client_id", "GOOGLE_CLIENT_ID", "Google OAuth client ID"),
    FieldSpec("google_client_secret", "GOOGLE_CLIENT_SECRET", "Google OAuth client secret", secret=True),
    FieldSpec("google_refresh_token", "GOOGLE_REFRESH_TOKEN", "Google refresh token", secret=True),
    FieldSpec("google_account", "GOOGLE_ACCOUNT", "Connected Google account"),
    FieldSpec("calendar_id", "VPE_CALENDAR_ID", "Calendar"),
    FieldSpec("calendar_hours", "VPE_CALENDAR_HOURS", "Opening hours"),
    FieldSpec("calendar_days", "VPE_CALENDAR_DAYS", "Open days"),
    # automation safety
    FieldSpec("timezone", "VPE_TIMEZONE", "Time zone"),
    FieldSpec("outbound", "VPE_OUTBOUND", "Outbound calls and messages", choices=("off", "on")),
    FieldSpec("quiet_hours", "VPE_QUIET_HOURS", "Quiet hours (no outbound)"),
    FieldSpec("outbound_daily_cap", "VPE_OUTBOUND_DAILY_CAP", "Outbound limit per agent per day"),
    # knowledge search
    FieldSpec("embeddings", "VPE_EMBEDDINGS", "Embeddings for knowledge search", choices=("local", "ollama", "off")),
    FieldSpec("embeddings_url", "VPE_EMBEDDINGS_URL", "Embeddings server (OpenAI-compatible)"),
    FieldSpec("embeddings_model", "VPE_EMBEDDINGS_MODEL", "Embeddings model"),
    FieldSpec("brand_name", "BRAND_NAME", "Workspace name"),
)
SPEC_BY_NAME = {s.name: s for s in SPECS}


@dataclass
class Config:
    llm: str = "anthropic"
    model: str = ""
    anthropic_key: str = ""
    openai_key: str = ""
    base_url: str = ""
    gemini_key: str = ""
    vertex_project: str = ""
    vertex_location: str = "global"
    vertex_credentials: str = ""
    azure_key: str = ""
    azure_endpoint: str = ""
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_region: str = "us-east-1"
    groq_key: str = ""
    mistral_key: str = ""
    deepseek_key: str = ""
    xai_key: str = ""
    together_key: str = ""
    fireworks_key: str = ""
    openrouter_key: str = ""
    cerebras_key: str = ""
    nvidia_key: str = ""
    qwen_key: str = ""
    sambanova_key: str = ""
    nebius_key: str = ""
    novita_key: str = ""
    ollama_url: str = "http://localhost:11434/v1"
    temperature: float | None = None
    stt: str = "deepgram"
    deepgram_key: str = ""
    stt_model: str = "nova-3-general"
    whisper_url: str = "http://localhost:8001/v1"
    whisper_model: str = "Systran/faster-whisper-small"
    local_whisper_model: str = "base.en"
    moonshine_model: str = "small-streaming"
    turn_detection: str = "smart"
    turn_pause: str = ""          # smart: longest pause before answering anyway (3s); pause: silence that ends a turn (0.8s)
    tts: str = "deepgram"
    tts_voice: str = "aura-2-helena-en"
    fish_key: str = ""
    fish_voice: str = ""
    fish_model: str = "s2.1-pro-free"
    fish_stt_model: str = "transcribe-1"
    fish_latency: str = "balanced"
    fish_connection: str = "auto"
    fish_speed: str = ""
    fish_volume: str = ""
    fish_temperature: str = ""
    fish_top_p: str = ""
    fish_normalize: str = "on"
    fish_stt_language: str = "en"
    kokoro_url: str = "http://localhost:8880/v1"
    kokoro_voice: str = "af_heart"
    twilio_auth_token: str = ""
    telnyx_api_key: str = ""
    telnyx_public_key: str = ""
    twilio_account_sid: str = ""
    twilio_from_number: str = ""
    telnyx_texml_app_id: str = ""
    telnyx_from_number: str = ""
    jambonz_webhook_secret: str = ""
    jambonz_sample_rate: str = "8000"   # 16000: HD voice, when the trunk or phone system carries wideband audio
    telegram_bot_token: str = ""
    whatsapp_token: str = ""
    whatsapp_phone_number_id: str = ""
    whatsapp_app_secret: str = ""
    whatsapp_verify_token: str = ""
    email_provider: str = "smtp"
    email_address: str = ""
    email_password: str = ""
    smtp_host: str = ""
    smtp_port: str = "587"
    imap_host: str = ""
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_account: str = ""
    calendar_id: str = "primary"
    calendar_hours: str = "09:00-17:00"
    calendar_days: str = "mon-fri"
    timezone: str = ""
    outbound: str = "off"
    quiet_hours: str = "21:00-08:00"
    outbound_daily_cap: str = "50"
    embeddings: str = "local"
    embeddings_url: str = "http://localhost:11434/v1"
    embeddings_model: str = "nomic-embed-text"
    brand_name: str = "Lampline"
    language: str = "en"
    sources: dict[str, str] = field(default_factory=dict)   # field -> "env" | "saved"

    def __post_init__(self):
        if not self.model:
            self.model = default_model(self.llm)

    @classmethod
    def load(cls, env: dict | None = None, saved: dict | None = None) -> "Config":
        """Defaults, then the environment, then values saved from the Integrations page."""
        e = os.environ if env is None else env
        values: dict[str, Any] = {}
        sources: dict[str, str] = {}
        for spec in SPECS:
            raw = (e.get(spec.env) or "").strip().strip('"').strip("'")
            if raw:
                values[spec.name], sources[spec.name] = raw, "env"
        for name, raw in (saved or {}).items():
            if name in SPEC_BY_NAME and raw not in (None, ""):
                values[name], sources[name] = str(raw).strip(), "saved"
        for name, (lo, hi) in RANGES.items():
            if values.get(name):
                try:
                    number = float(values[name])
                except ValueError as exc:
                    raise ConfigError(f"{SPEC_BY_NAME[name].label} must be a number") from exc
                if not lo <= number <= hi:
                    raise ConfigError(f"{SPEC_BY_NAME[name].label} must be between {lo:g} and {hi:g}")
        for name, label in (("calendar_hours", "Opening hours"), ("quiet_hours", "Quiet hours")):
            if values.get(name) and not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d", values[name]):
                raise ConfigError(f"{label} must look like 09:00-17:00")
        if values.get("calendar_days") and not re.fullmatch(r"(mon|tue|wed|thu|fri|sat|sun)(-(mon|tue|wed|thu|fri|sat|sun))?"
                                                            r"(,(mon|tue|wed|thu|fri|sat|sun)(-(mon|tue|wed|thu|fri|sat|sun))?)*",
                                                            values["calendar_days"].lower()):
            raise ConfigError("Open days must look like mon-fri or mon-fri,sat")
        for name, label, hi in (("smtp_port", "SMTP port", 65535), ("outbound_daily_cap", "Outbound limit", 10000)):
            if values.get(name) and (not values[name].isdigit() or not 1 <= int(values[name]) <= hi):
                raise ConfigError(f"{label} must be a whole number from 1 to {hi}")
        for name, example in (("embeddings_url", "http://localhost:11434/v1"), ("ollama_url", "http://localhost:11434/v1"),
                              ("azure_endpoint", "https://my-resource.openai.azure.com"),
                              ("base_url", "https://api.openai.com/v1")):
            if values.get(name) and not re.match(r"^https?://[^\s/]+", values[name]):
                raise ConfigError(f"{SPEC_BY_NAME[name].label} must be an http(s) address, like {example}")
        if values.get("vertex_credentials"):
            import json as _json
            try:
                if _json.loads(values["vertex_credentials"]).get("type") != "service_account":
                    raise ValueError
            except (ValueError, AttributeError) as exc:
                raise ConfigError("the Vertex AI key must be a service account's JSON key file, pasted whole") from exc
        if values.get("timezone"):
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                ZoneInfo(values["timezone"])
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ConfigError("Time zone must be a name like America/New_York") from exc
        for name in ("twilio_from_number", "telnyx_from_number"):
            if values.get(name) and not re.fullmatch(r"\+[1-9]\d{6,14}", values[name]):
                raise ConfigError("Phone numbers must be in +15551234567 form")
        if values.get("fish_stt_language") and not re.fullmatch(r"[a-z]{2}", values["fish_stt_language"]):
            raise ConfigError("Caller language must be a two-letter code like en, or empty to detect it")
        for spec in SPECS:
            if spec.choices and spec.name in values:
                values[spec.name] = values[spec.name].lower()
                if values[spec.name] not in spec.choices:
                    raise ConfigError(f"{spec.env} must be one of {', '.join(spec.choices)}")
        temp = values.pop("temperature", None)
        cfg = cls(**values, sources=sources)
        if temp is not None:
            try:
                cfg.temperature = float(temp)
            except ValueError as exc:
                raise ConfigError("temperature must be a number") from exc
        if "model" not in values:   # provider changed: pick that provider's default model
            cfg.model = default_model(cfg.llm)
        return cfg

    @classmethod
    def from_env(cls, env: dict | None = None) -> "Config":
        return cls.load(env)

    # -- readiness ---------------------------------------------------------------

    @property
    def llm_ready(self) -> bool:
        p = LLM_BY_NAME[self.llm]
        if self.llm == "openai":
            return bool(self.base_url or self.openai_key)
        if self.llm == "vertex":
            return bool(self.vertex_project)
        if self.llm == "azure":
            return bool(self.azure_key and self.azure_endpoint and self.model)
        if self.llm == "bedrock":
            return bool(self.aws_region)       # keys may come from this computer's AWS credentials
        if self.llm == "ollama":
            return bool(self.ollama_url)
        return bool(getattr(self, p.key))

    @property
    def stt_ready(self) -> bool:
        if self.stt == "deepgram":
            return bool(self.deepgram_key)
        if self.stt == "fish":
            return bool(self.fish_key)
        if self.stt == "whisper":
            from .builtin_speech import whisper_ready
            return whisper_ready(self.local_whisper_model)
        if self.stt == "moonshine":
            from . import moonshine_stt
            return moonshine_stt.ready(self.moonshine_model)
        return bool(self.whisper_url)

    @property
    def tts_ready(self) -> bool:
        if self.tts == "deepgram":
            return bool(self.deepgram_key)
        if self.tts == "fish":
            return bool(self.fish_key)
        if self.tts == "kokoro":
            from .builtin_speech import kokoro_ready
            return kokoro_ready()
        return bool(self.kokoro_url)

    @property
    def voice_ready(self) -> bool:
        return self.stt_ready and self.tts_ready

    def describe(self) -> dict:
        """Safe to show: no keys."""
        voice = {"deepgram": self.tts_voice, "fish": f"{self.fish_model}, {self.fish_voice or 'default voice'}, {'live' if self.fish_connection == 'live' else 'HTTP'}", "kokoro": self.kokoro_voice,
                 "kokoro-server": self.kokoro_voice}
        return {"llm": self.llm, "llm_label": LLM_BY_NAME[self.llm].label, "model": self.model,
                "base_url": self.base_url or None,
                "llm_ready": self.llm_ready, "voice_ready": self.voice_ready,
                "turn_detection": self.turn_detection,
                "stt": self.stt, "stt_ready": self.stt_ready, "tts": self.tts, "tts_ready": self.tts_ready,
                "stt_model": {"deepgram": self.stt_model, "whisper": self.local_whisper_model,
                              "fish": self.fish_stt_model, "moonshine": self.moonshine_model}.get(self.stt, self.whisper_model),
                "tts_voice": voice[self.tts], "twilio_ready": bool(self.twilio_auth_token),
                "telnyx_ready": bool(self.telnyx_public_key),
                "jambonz_ready": bool(self.jambonz_webhook_secret),
                "phone_ready": bool(self.twilio_auth_token or self.telnyx_public_key or self.jambonz_webhook_secret),
                "outbound_call_ready": bool(self.twilio_auth_token and self.twilio_account_sid and self.twilio_from_number)
                or bool(self.telnyx_api_key and self.telnyx_texml_app_id and self.telnyx_from_number),
                "telegram_ready": bool(self.telegram_bot_token),
                "whatsapp_ready": bool(self.whatsapp_token and self.whatsapp_phone_number_id),
                "email_ready": self.email_ready, "google_ready": self.google_ready,
                "outbound": self.outbound}

    @property
    def google_ready(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret and self.google_refresh_token)

    @property
    def email_ready(self) -> bool:
        if self.email_provider == "gmail":
            return self.google_ready
        return bool(self.email_address and self.email_password and self.smtp_host)

    @property
    def zone(self):
        """The business's time zone (Settings), else this computer's."""
        from zoneinfo import ZoneInfo
        if self.timezone:
            return ZoneInfo(self.timezone)
        import datetime as _dt
        return _dt.datetime.now().astimezone().tzinfo

    def values(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self) if f.name in SPEC_BY_NAME}


# -- thinking ------------------------------------------------------------------

def make_llm(cfg: Config) -> LLMService:
    p = LLM_BY_NAME.get(cfg.llm)
    if p is None:
        raise ConfigError(f"unknown model provider {cfg.llm!r}")

    def settings_for(service, **extra):
        settings = service.Settings(model=cfg.model, **extra)
        if cfg.temperature is not None:
            settings.temperature = cfg.temperature
        return settings

    if cfg.llm == "anthropic":
        if not cfg.anthropic_key:
            raise ConfigError("no Anthropic API key (Integrations, or ANTHROPIC_API_KEY)")
        from pipecat.services.anthropic.llm import AnthropicLLMService, AnthropicThinkingConfig

        return AnthropicLLMService(api_key=cfg.anthropic_key, settings=settings_for(
            AnthropicLLMService, thinking=AnthropicThinkingConfig(type="disabled"), enable_prompt_caching=True))
    if cfg.llm == "openai":
        if not (cfg.base_url or cfg.openai_key):
            raise ConfigError("set an API key, or a base URL for a local endpoint")
        from pipecat.services.openai.llm import OpenAILLMService

        kwargs = {"api_key": cfg.openai_key or "not-needed"}
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        return OpenAILLMService(settings=settings_for(OpenAILLMService), **kwargs)
    if cfg.llm == "google":
        if not cfg.gemini_key:
            raise ConfigError("no Gemini API key (Integrations, or GEMINI_API_KEY)")
        from pipecat.services.google.llm import GoogleLLMService

        return GoogleLLMService(api_key=cfg.gemini_key, settings=settings_for(GoogleLLMService))
    if cfg.llm == "vertex":
        if not cfg.vertex_project:
            raise ConfigError("set your Google Cloud project ID for Vertex AI")
        from pipecat.services.google.vertex.llm import GoogleVertexLLMService

        try:
            return GoogleVertexLLMService(project_id=cfg.vertex_project, location=cfg.vertex_location or "global",
                                          credentials=cfg.vertex_credentials or None,
                                          settings=settings_for(GoogleVertexLLMService))
        except Exception as exc:  # noqa: BLE001 - no credentials found, or a bad key
            raise ConfigError(f"Vertex AI couldn't sign in: {exc}") from exc
    if cfg.llm == "azure":
        if not (cfg.azure_key and cfg.azure_endpoint):
            raise ConfigError("Azure OpenAI needs an API key and the resource's endpoint")
        from pipecat.services.azure.llm import AzureLLMService

        return AzureLLMService(api_key=cfg.azure_key, endpoint=cfg.azure_endpoint, settings=settings_for(AzureLLMService))
    if cfg.llm == "bedrock":
        from pipecat.services.aws.llm import AWSBedrockLLMService

        return AWSBedrockLLMService(aws_access_key=cfg.aws_access_key_id or None,
                                    aws_secret_key=cfg.aws_secret_access_key or None,
                                    aws_region=cfg.aws_region or None, settings=settings_for(AWSBedrockLLMService))
    import importlib

    service = getattr(importlib.import_module(p.module), p.cls)
    if cfg.llm == "ollama":
        if not cfg.ollama_url:
            raise ConfigError("set the Ollama address, like http://localhost:11434/v1")
        return service(base_url=cfg.ollama_url, settings=settings_for(service))
    key = getattr(cfg, p.key)
    if not key:
        raise ConfigError(f"no {p.label} API key (Integrations, or {SPEC_BY_NAME[p.key].env})")
    return service(api_key=key, settings=settings_for(service))


# -- listening -----------------------------------------------------------------

def make_turn_strategies(cfg: Config):
    """How a voice call decides the caller has finished speaking.

    smart: Pipecat's smart-turn model listens to how the caller sounds, and
    answers once they have finished, or after the pause limit at most.
    pause: answer after a fixed silence, for lines where the model struggles.
    """
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy, TurnAnalyzerUserTurnStopStrategy
    from pipecat.turns.user_turn_strategies import UserTurnStrategies

    pause = float(cfg.turn_pause) if cfg.turn_pause else None
    if cfg.turn_detection == "pause":
        return UserTurnStrategies(stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=pause or 0.8)])
    from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
    from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3

    analyzer = LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=pause or 3.0))
    return UserTurnStrategies(stop=[TurnAnalyzerUserTurnStopStrategy(turn_analyzer=analyzer)])


def make_stt(cfg: Config):
    if cfg.stt == "fish":
        if not cfg.fish_key:
            raise ConfigError("no Fish Audio API key")
        from .fish import FishSTTService
        return FishSTTService(api_key=cfg.fish_key, model=cfg.fish_stt_model, language=cfg.fish_stt_language)
    if cfg.stt == "whisper":
        from .builtin_speech import make_builtin_stt, whisper_ready
        if not whisper_ready(cfg.local_whisper_model):
            raise ConfigError("the built-in Whisper model isn't downloaded yet: Integrations → Download models")
        return make_builtin_stt(cfg.local_whisper_model)
    if cfg.stt == "moonshine":
        from . import moonshine_stt
        if not moonshine_stt.installed():
            raise ConfigError("Moonshine isn't installed on this computer (it needs Linux or an Apple-silicon Mac)")
        if not moonshine_stt.ready(cfg.moonshine_model):
            raise ConfigError("the Moonshine model isn't downloaded yet: Integrations → Moonshine → Download")
        return moonshine_stt.MoonshineSTTService(model=cfg.moonshine_model)
    if cfg.stt == "whisper-server":
        if not cfg.whisper_url:
            raise ConfigError("no Whisper server URL")
        from pipecat.services.openai.stt import OpenAISTTService

        return OpenAISTTService(base_url=cfg.whisper_url, api_key="not-needed",
                                settings=OpenAISTTService.Settings(model=cfg.whisper_model))
    if not cfg.deepgram_key:
        raise ConfigError("voice calls need a Deepgram API key, or Whisper for listening")
    from pipecat.services.deepgram.stt import DeepgramSTTService

    return DeepgramSTTService(api_key=cfg.deepgram_key, settings=DeepgramSTTService.Settings(
        model=cfg.stt_model, language=cfg.language, smart_format=True, punctuate=True, interim_results=True))


# -- speaking ------------------------------------------------------------------

def make_tts(cfg: Config, sample_rate: int | None = None):
    if cfg.tts == "fish":
        if not cfg.fish_key:
            raise ConfigError("no Fish Audio API key")
        if cfg.fish_connection != "live":      # auto and http: Fish's documented POST /v1/tts
            from .fish import FishHTTPTTSService
            num = lambda v: float(v) if v else None  # noqa: E731
            return FishHTTPTTSService(api_key=cfg.fish_key, model=cfg.fish_model, voice=cfg.fish_voice,
                                      latency=cfg.fish_latency, normalize=cfg.fish_normalize != "off",
                                      speed=num(cfg.fish_speed), volume=num(cfg.fish_volume),
                                      temperature=num(cfg.fish_temperature), top_p=num(cfg.fish_top_p))
        from pipecat.services.fish.tts import FishAudioTTSService

        settings = FishAudioTTSService.Settings(model=cfg.fish_model or None, latency=cfg.fish_latency,
                                                normalize=cfg.fish_normalize != "off")
        if cfg.fish_voice:
            settings.voice = cfg.fish_voice
        if cfg.fish_speed:
            settings.prosody_speed = float(cfg.fish_speed)
        if cfg.fish_volume:
            settings.prosody_volume = round(float(cfg.fish_volume))
        if cfg.fish_temperature:
            settings.temperature = float(cfg.fish_temperature)
        if cfg.fish_top_p:
            settings.top_p = float(cfg.fish_top_p)
        return FishAudioTTSService(api_key=cfg.fish_key, sample_rate=sample_rate, settings=settings)
    if cfg.tts == "kokoro":
        from .builtin_speech import BuiltinKokoroTTS, kokoro_ready
        if not kokoro_ready():
            raise ConfigError("the built-in Kokoro model isn't downloaded yet: Integrations → Download models")
        return BuiltinKokoroTTS(voice=cfg.kokoro_voice)
    if cfg.tts == "kokoro-server":
        if not cfg.kokoro_url:
            raise ConfigError("no Kokoro server URL")
        return SpeechEndpointTTSService(base_url=cfg.kokoro_url, voice=cfg.kokoro_voice, model="kokoro")
    if not cfg.deepgram_key:
        raise ConfigError("voice calls need a Deepgram API key, or Fish Audio / Kokoro for speaking")
    from pipecat.services.deepgram.tts import DeepgramTTSService

    return DeepgramTTSService(api_key=cfg.deepgram_key, sample_rate=sample_rate,
                              settings=DeepgramTTSService.Settings(voice=cfg.tts_voice))


class SpeechEndpointTTSService(TTSService):
    """Any OpenAI-style /audio/speech server (Kokoro-FastAPI, Speaches, ...).

    Pipecat's OpenAI TTS only accepts OpenAI's own voice names, so local voices
    like Kokoro's `af_heart` go through this instead. Audio is 24 kHz 16-bit PCM;
    the output transport resamples it for phone calls.
    """

    RATE = 24000

    def __init__(self, *, base_url: str, voice: str, model: str = "kokoro", **kwargs):
        from pipecat.services.settings import TTSSettings

        super().__init__(sample_rate=self.RATE, push_start_frame=True, push_stop_frames=True,
                         settings=TTSSettings(model=model, voice=voice, language=None), **kwargs)
        self._url = base_url.rstrip("/") + "/audio/speech"
        self._voice = voice
        self._model = model
        self._http = None

    def can_generate_metrics(self) -> bool:
        return True

    async def stop(self, frame):
        await super().stop(frame)
        await self._close()

    async def cancel(self, frame):
        await super().cancel(frame)
        await self._close()

    async def _close(self):
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator:
        import httpx
        from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame

        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
        body = {"model": self._model, "input": text, "voice": self._voice, "response_format": "pcm"}
        try:
            async with self._http.stream("POST", self._url, json=body) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode("utf-8", "replace")[:200]
                    yield ErrorFrame(error=f"speech server returned {r.status_code}: {detail}")
                    return
                await self.start_tts_usage_metrics(text)
                carry = b""
                async for chunk in r.aiter_bytes(4800):
                    chunk = carry + chunk
                    usable = len(chunk) - (len(chunk) % 2)      # whole 16-bit samples only
                    carry = chunk[usable:]
                    if usable:
                        yield TTSAudioRawFrame(chunk[:usable], self.RATE, 1, context_id=context_id)
        except httpx.HTTPError as exc:
            yield ErrorFrame(error=f"cannot reach the speech server at {self._url}: {exc}")
