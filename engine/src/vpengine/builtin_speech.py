"""Free speech that runs inside the engine: no Docker, no account, no key.

    Listening: Whisper via faster-whisper (CTranslate2, int8 on the CPU)
    Speaking:  Kokoro-82M via kokoro-onnx (ONNX Runtime)

Both are native on Apple Silicon. The models download once (Whisper from
Hugging Face, Kokoro from the kokoro-onnx GitHub release) into
~/.cache/vpengine, and are loaded once and shared by every call.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from pathlib import Path
from typing import AsyncGenerator

import httpx
import numpy as np

from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame
from pipecat.services.settings import TTSSettings
from pipecat.services.tts_service import TTSService

CACHE = Path(os.environ.get("VPE_MODEL_CACHE", "") or Path.home() / ".cache" / "vpengine")
KOKORO_FILES = {
    "kokoro-v1.0.onnx": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx",
    "voices-v1.0.bin": "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin",
}
KOKORO_SHA256 = {
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}
WHISPER_SIZES_MB = {"tiny.en": 75, "base.en": 145, "small.en": 485, "distil-small.en": 340}

_lock = threading.Lock()
_whisper: dict[str, object] = {}
_kokoro: object | None = None


def kokoro_dir() -> Path:
    return CACHE / "kokoro"


def whisper_dir() -> Path:
    return CACHE / "whisper"


def kokoro_ready() -> bool:
    return all((kokoro_dir() / name).is_file() for name in KOKORO_FILES)


def whisper_ready(model: str) -> bool:
    try:
        from faster_whisper.utils import download_model
        download_model(model, local_files_only=True, cache_dir=str(whisper_dir()))
        return True
    except Exception:  # noqa: BLE001 - not downloaded (or not installed)
        return False


def installed() -> str | None:
    """None if the libraries are present, else what's missing."""
    missing = []
    for mod in ("faster_whisper", "kokoro_onnx"):
        try:
            __import__(mod)
        except ImportError:
            missing.append(mod)
    return ", ".join(missing) or None


def load_whisper(model: str):
    with _lock:
        if model not in _whisper:
            from faster_whisper import WhisperModel
            _whisper[model] = WhisperModel(model, device="cpu", compute_type="int8",
                                           download_root=str(whisper_dir()))
        return _whisper[model]


def load_kokoro():
    global _kokoro
    with _lock:
        if _kokoro is None:
            from kokoro_onnx import Kokoro
            _kokoro = Kokoro(str(kokoro_dir() / "kokoro-v1.0.onnx"), str(kokoro_dir() / "voices-v1.0.bin"))
        return _kokoro


class Downloads:
    """Fetches the models in the background and reports progress to the UI."""

    def __init__(self):
        self.state = "idle"        # idle | downloading | error
        self.message = ""
        self.done_bytes = 0
        self.total_bytes = 0
        self._task: asyncio.Task | None = None

    def status(self, whisper_model: str) -> dict:
        return {"state": self.state, "message": self.message, "installed": installed() is None,
                "missing": installed(), "whisper_ready": whisper_ready(whisper_model),
                "kokoro_ready": kokoro_ready(), "whisper_model": whisper_model,
                "progress": round(self.done_bytes / self.total_bytes, 3) if self.total_bytes else None,
                "size_mb": WHISPER_SIZES_MB.get(whisper_model, 300) + 340}

    def start(self, whisper_model: str) -> None:
        if installed():
            raise RuntimeError(f"built-in speech libraries aren't installed ({installed()}); "
                               "restart with scripts/engine-up.sh")
        if self._task and not self._task.done():
            return
        self.state, self.message, self.done_bytes, self.total_bytes = "downloading", "Starting the download…", 0, 0
        self._task = asyncio.create_task(self._run(whisper_model))

    async def _run(self, whisper_model: str) -> None:
        try:
            if not kokoro_ready():
                await self._kokoro()
            if not whisper_ready(whisper_model):
                self.message = f"Downloading the Whisper model {whisper_model}…"
                from faster_whisper.utils import download_model
                await asyncio.to_thread(download_model, whisper_model, cache_dir=str(whisper_dir()))
            self.message = "Loading the models…"
            await asyncio.to_thread(load_kokoro)
            await asyncio.to_thread(load_whisper, whisper_model)
            self.state, self.message = "idle", "Ready"
        except Exception as exc:  # noqa: BLE001 - shown in the UI
            self.state, self.message = "error", f"Download failed: {exc}"

    async def _kokoro(self) -> None:
        kokoro_dir().mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(60.0, connect=10.0)) as http:
            sizes = {}
            for name, url in KOKORO_FILES.items():
                r = await http.head(url)
                sizes[name] = int(r.headers.get("content-length") or 0)
            self.total_bytes = sum(sizes.values())
            for name, url in KOKORO_FILES.items():
                dest = kokoro_dir() / name
                if dest.is_file() and dest.stat().st_size == sizes[name]:
                    self.done_bytes += sizes[name]
                    continue
                self.message = f"Downloading the Kokoro voice model ({name})…"
                part = dest.with_suffix(dest.suffix + ".part")
                digest = hashlib.sha256()
                async with http.stream("GET", url) as r:
                    r.raise_for_status()
                    with open(part, "wb") as f:
                        async for chunk in r.aiter_bytes(1 << 16):
                            f.write(chunk)
                            digest.update(chunk)
                            self.done_bytes += len(chunk)
                if digest.hexdigest() != KOKORO_SHA256[name]:
                    part.unlink(missing_ok=True)
                    raise RuntimeError(f"{name} didn't match its expected checksum; not using it")
                os.replace(part, dest)


class BuiltinKokoroTTS(TTSService):
    """Kokoro inside the engine. Audio is resampled by the output transport."""

    def __init__(self, *, voice: str = "af_heart", lang: str = "en-us", speed: float = 1.0, **kwargs):
        super().__init__(sample_rate=24000, push_start_frame=True, push_stop_frames=True,
                         settings=TTSSettings(model="kokoro-v1.0", voice=voice, language=None), **kwargs)
        self._voice, self._lang, self._speed = voice, lang, speed

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator:
        if not kokoro_ready():
            yield ErrorFrame(error="the Kokoro model isn't downloaded yet (Integrations → Download models)")
            return
        try:
            kokoro = await asyncio.to_thread(load_kokoro)
            if self._voice not in kokoro.voices:
                yield ErrorFrame(error=f"Kokoro has no voice named {self._voice!r}")
                return
            await self.start_tts_usage_metrics(text)
            async for samples, rate in kokoro.create_stream(text, voice=self._voice, lang=self._lang, speed=self._speed):
                await self.stop_ttfb_metrics()
                pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()
                yield TTSAudioRawFrame(audio=pcm, sample_rate=rate, num_channels=1, context_id=context_id)
        except Exception as exc:  # noqa: BLE001
            yield ErrorFrame(error=f"Kokoro failed: {exc}")


WHISPER_RATE = 16000


def make_builtin_stt(model: str):
    """Whisper inside the engine, sharing one loaded model across calls."""
    from pipecat.services.whisper.stt import WhisperSTTService

    class BuiltinWhisperSTT(WhisperSTTService):
        def _load(self):
            if not whisper_ready(model):
                raise RuntimeError("the Whisper model isn't downloaded yet (Integrations → Download models)")
            self._model = load_whisper(model)

        async def run_stt(self, audio: bytes):
            # Whisper reads 16 kHz audio, but each caller's turn arrives at the call's rate: 8 kHz on a
            # phone call. Unconverted, phone audio plays twice as fast to Whisper and comes out as nonsense.
            if self.sample_rate and self.sample_rate != WHISPER_RATE and audio:
                import numpy as np
                import soxr

                pcm = np.frombuffer(audio, dtype=np.int16)
                audio = soxr.resample(pcm, self.sample_rate, WHISPER_RATE).astype(np.int16).tobytes()
            async for frame in super().run_stt(audio):
                yield frame

    return BuiltinWhisperSTT(settings=WhisperSTTService.Settings(model=model), device="cpu", compute_type="int8")


def warm(whisper_model: str) -> None:
    """Load both models ahead of the first call (called from a thread)."""
    started = time.monotonic()
    if kokoro_ready():
        load_kokoro()
    if whisper_ready(whisper_model):
        load_whisper(whisper_model)
    return time.monotonic() - started
