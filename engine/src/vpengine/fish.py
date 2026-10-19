"""Fish Audio beyond text to speech: speech to text (ASR) and the voice list.

Speaking uses Pipecat's FishAudioTTSService (streaming WebSocket). Listening
sends each spoken segment (a WAV file, cut by voice activity detection) to
POST https://api.fish.audio/v1/asr. Pricing on Fish's docs: $0.36 per audio hour.
"""

from __future__ import annotations

import re
from typing import AsyncGenerator

import httpx

from pipecat.frames.frames import ErrorFrame, TranscriptionFrame, TTSAudioRawFrame
from pipecat.services.settings import STTSettings, TTSSettings
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601

API = "https://api.fish.audio"
_MARKERS = re.compile(r"<\|[^|>]*\|>")          # <|speaker:0|> and similar


def clean_transcript(text: str) -> str:
    return re.sub(r"\s+", " ", _MARKERS.sub(" ", text or "")).strip()


NO_CREDIT = ("Fish Audio has no API credit for {what}. Add API credit at fish.audio/app/developers{hint}.")


class FishHTTPTTSService(TTSService):
    """Fish Audio speaking over POST /v1/tts, streamed back as 24 kHz PCM.

    Fish documents its free model (s2.1-pro-free) on this endpoint; its live
    WebSocket answered "402 no credit" for free accounts, so free calls use this.
    """

    RATE = 24000

    def __init__(self, *, api_key: str, model: str, voice: str = "", latency: str = "balanced",
                 normalize: bool = True, speed: float | None = None, volume: float | None = None,
                 temperature: float | None = None, top_p: float | None = None, base_url: str = API, **kwargs):
        super().__init__(sample_rate=self.RATE, push_start_frame=True, push_stop_frames=True,
                         settings=TTSSettings(model=model, voice=voice or None, language=None), **kwargs)
        self._key, self._model, self._url = api_key, model, base_url.rstrip("/") + "/v1/tts"
        body: dict = {"format": "pcm", "sample_rate": self.RATE, "latency": latency, "normalize": normalize}
        if voice:
            body["reference_id"] = voice
        if speed is not None or volume is not None:
            body["prosody"] = {"speed": 1.0 if speed is None else speed, "volume": 0 if volume is None else volume}
        if temperature is not None:
            body["temperature"] = temperature
        if top_p is not None:
            body["top_p"] = top_p
        self._body = body
        self._http: httpx.AsyncClient | None = None

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
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
        headers = {"Authorization": f"Bearer {self._key}", "model": self._model}
        try:
            async with self._http.stream("POST", self._url, headers=headers, json={**self._body, "text": text}) as r:
                if r.status_code != 200:
                    detail = (await r.aread()).decode("utf-8", "replace")[:200]
                    if r.status_code == 402:
                        yield ErrorFrame(error=NO_CREDIT.format(what=f"the {self._model} voice", hint=""))
                    elif r.status_code in (401, 403):
                        yield ErrorFrame(error="Fish Audio rejected the API key")
                    else:
                        yield ErrorFrame(error=f"Fish Audio speech answered {r.status_code}: {detail}")
                    return
                await self.start_tts_usage_metrics(text)
                carry = b""
                async for chunk in r.aiter_bytes(4800):
                    chunk = carry + chunk
                    usable = len(chunk) - (len(chunk) % 2)
                    carry = chunk[usable:]
                    if usable:
                        await self.stop_ttfb_metrics()
                        yield TTSAudioRawFrame(chunk[:usable], self.RATE, 1, context_id=context_id)
        except httpx.HTTPError as exc:
            yield ErrorFrame(error=f"cannot reach Fish Audio: {exc.__class__.__name__}")


class FishSTTService(SegmentedSTTService):
    """Fish Audio speech to text, one request per caller utterance."""

    def __init__(self, *, api_key: str, model: str = "transcribe-1", language: str = "en",
                 base_url: str = API, **kwargs):
        super().__init__(settings=STTSettings(model=model, language=None), ttfs_p99_latency=1.5, **kwargs)
        self._key = api_key
        self._model = model
        self._lang = language
        self._url = base_url.rstrip("/") + "/v1/asr"
        self._http: httpx.AsyncClient | None = None

    def can_generate_metrics(self) -> bool:
        return True

    def _form(self) -> dict:
        form = {"ignore_timestamps": "true"}
        if self._lang:                       # empty: let Fish detect the language
            form["language"] = self._lang
        if self._model == "transcribe-1-pro":
            form.update(tag_audio_events="false", diarize="false")   # one caller: no [laughter] cues or speaker labels
        return form

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

    async def run_stt(self, audio: bytes) -> AsyncGenerator:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))
        await self.start_processing_metrics()
        try:
            r = await self._http.post(
                self._url, headers={"Authorization": f"Bearer {self._key}", "model": self._model},
                files={"audio": ("speech.wav", audio, "audio/wav")},
                data=self._form())
        except httpx.HTTPError as exc:
            yield ErrorFrame(error=f"cannot reach Fish Audio speech to text: {exc.__class__.__name__}")
            return
        finally:
            await self.stop_processing_metrics()
        if r.status_code in (401, 403):
            yield ErrorFrame(error="Fish Audio rejected the API key")
            return
        if r.status_code == 402:
            yield ErrorFrame(error=NO_CREDIT.format(
                what="speech to text (it's paid; only speaking has a free model)",
                hint=", or switch Listening to Whisper on this computer in Integrations"))
            return
        if r.status_code >= 400:
            yield ErrorFrame(error=f"Fish Audio speech to text answered {r.status_code}: {r.text[:200]}")
            return
        text = clean_transcript((r.json() or {}).get("text", ""))
        if text:
            yield TranscriptionFrame(text, self._user_id, time_now_iso8601(), None)


async def list_voices(api_key: str, *, base_url: str = API, timeout: float = 10.0) -> dict:
    """The account's own voices, plus popular English voices to start from."""
    headers = {"Authorization": f"Bearer {api_key}"}

    def slim(item: dict, mine: bool) -> dict:
        return {"id": item.get("_id"), "title": item.get("title") or item.get("_id"),
                "languages": item.get("languages") or [], "mine": mine}

    async with httpx.AsyncClient(timeout=timeout, base_url=base_url) as http:
        own = await http.get("/model", headers=headers, params={"self": "true", "page_size": 50})
        if own.status_code in (401, 403):
            raise PermissionError("Fish Audio rejected the API key")
        own.raise_for_status()
        popular = await http.get("/model", headers=headers,
                                 params={"language": "en", "sort_by": "score", "page_size": 20})
        popular_items = popular.json().get("items", []) if popular.status_code < 400 else []
    return {"mine": [slim(i, True) for i in own.json().get("items", [])],
            "popular": [slim(i, False) for i in popular_items]}
