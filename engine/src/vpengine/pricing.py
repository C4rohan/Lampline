"""What a call cost, roughly: speech, model and phone line, from what the call used.

The engine counts what each call used (seconds of audio sent to speech to text,
the model's input and output tokens, characters spoken) and multiplies by a rate
per service. Rates you set in Integrations → Call history win; otherwise a list
price is used when there is one we have checked (costs/cost_model.py), and the
part is left out, and named in "missing", when there isn't. Services that run on
this computer cost nothing per call.

These are estimates for comparing agents and settings, not a bill: providers
round, discount, cache and charge minimums in their own ways.
"""

from __future__ import annotations

from typing import Any

LOCAL_STT = {"whisper", "moonshine", "parakeet", "whisper-server"}
LOCAL_TTS = {"kokoro", "kokoro-server", "qwen3-tts"}
LOCAL_LLM = {"ollama"}
PHONE_CHANNELS = {"twilio", "telnyx", "jambonz", "phone", "sip"}

# List prices (USD) checked 2026-10-17; see costs/cost_model.py for sources.
STT_PER_MIN = {"deepgram": 0.0077}
TTS_PER_MCHAR = {"fish": 15.0}
MODEL_PER_MTOK = (                     # (part of the model's name, input, output) per 1M tokens
    ("flash-lite", 0.10, 0.40),
    ("haiku-5", 0.10, 0.50),
)

RATE_FIELDS = ("price_stt_min", "price_llm_in", "price_llm_out", "price_tts_mchar", "price_phone_min")


def new_usage() -> dict[str, float]:
    return {"stt_seconds": 0.0, "llm_in": 0, "llm_out": 0, "llm_cached": 0, "tts_chars": 0}


def _rate(cfg, field: str, default: float | None) -> float | None:
    value = str(getattr(cfg, field, "") or "").strip()
    if value:
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _model_rates(model: str) -> tuple[float | None, float | None]:
    name = (model or "").lower()
    for part, rin, rout in MODEL_PER_MTOK:
        if part in name:
            return rin, rout
    return None, None


def estimate(usage: dict[str, Any] | None, *, cfg, minutes: float, channel: str, voice: bool,
             speech_to_speech: bool = False, model: str = "") -> dict[str, Any]:
    """{"total": dollars or None, "parts": {name: dollars}, "missing": [names without a rate]}."""
    u = {**new_usage(), **(usage or {})}
    parts: dict[str, float] = {}
    missing: list[str] = []

    def add(name: str, amount: float, rate: float | None) -> None:
        if amount <= 0:
            return
        if rate is None:
            missing.append(name)
        else:
            parts[name] = round(amount * rate, 6)

    if voice and not speech_to_speech:
        stt = getattr(cfg, "stt", "")
        stt_min = (u["stt_seconds"] / 60) if u["stt_seconds"] else minutes
        add("speech to text", stt_min, 0.0 if stt in LOCAL_STT else _rate(cfg, "price_stt_min", STT_PER_MIN.get(stt)))
        tts = getattr(cfg, "tts", "")
        add("voice", u["tts_chars"] / 1e6, 0.0 if tts in LOCAL_TTS else _rate(cfg, "price_tts_mchar", TTS_PER_MCHAR.get(tts)))

    llm = getattr(cfg, "llm", "")
    local = llm in LOCAL_LLM and not speech_to_speech
    rin, rout = (None, None) if speech_to_speech else _model_rates(model)
    name = "speech-to-speech model" if speech_to_speech else "model"
    add(f"{name} input", u["llm_in"] / 1e6, 0.0 if local else _rate(cfg, "price_llm_in", rin))
    add(f"{name} output", u["llm_out"] / 1e6, 0.0 if local else _rate(cfg, "price_llm_out", rout))

    if channel in PHONE_CHANNELS:
        add("phone line", minutes, _rate(cfg, "price_phone_min", None))

    total = round(sum(parts.values()), 6) if parts or not missing else None
    return {"total": total, "parts": parts, "missing": missing, "usage": {k: round(v, 3) for k, v in u.items()}}


def percentile(values: list[float], pct: float) -> int | None:
    """Nearest-rank percentile (p95 of 20 turns is the 19th fastest)."""
    vals = sorted(v for v in values if isinstance(v, (int, float)))
    if not vals:
        return None
    import math
    rank = max(1, math.ceil(pct / 100 * len(vals)))
    return int(round(vals[rank - 1]))
