"""Parakeet: NVIDIA's Parakeet TDT 0.6B speech to text, on this computer (CC-BY-4.0 models).

Parakeet TDT tops open speech-recognition leaderboards for English and is fast
on a CPU. It runs here through onnx-asr (MIT) with the int8 ONNX exports from
istupakov on Hugging Face (about 660 MB, downloaded once). Like the built-in
Whisper it transcribes each turn when the caller stops.

Measured on a 2-CPU cloud machine, from the end of a 2 to 5 second turn to the
final text: Parakeet v2 0.25 to 0.45 s, against 0.69 to 0.86 s for Whisper
base.en and about 0.39 s for Whisper tiny.en, with Whisper base's accuracy or
better (numbers come back as digits, "555-0142").

v2 is English. v3 adds 24 more European languages and detects the language
itself.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import AsyncGenerator

import numpy as np
from pipecat.frames.frames import ErrorFrame, Frame, TranscriptionFrame
from pipecat.services.stt_service import SegmentedSTTService
from pipecat.utils.time import time_now_iso8601

RATE = 16000
MODELS = {"parakeet-tdt-0.6b-v2": 661, "parakeet-tdt-0.6b-v3": 670}     # MB to download (int8)
DEFAULT = "parakeet-tdt-0.6b-v2"
FILES = ["*.int8.onnx", "config.json", "nemo128.onnx", "vocab.txt"]
LEAD_SECS = 0.2              # Parakeet can drop the end of a turn that starts on the first sample; a little silence first fixes it

_lock = threading.Lock()
_models: dict[str, object] = {}


def installed() -> bool:
    try:
        import onnx_asr  # noqa: F401
    except ImportError:
        return False
    return True


def model_dir(name: str = DEFAULT) -> Path:
    from .builtin_speech import CACHE
    return CACHE / "parakeet" / name


def ready(name: str = DEFAULT) -> bool:
    d = model_dir(name)
    return name in MODELS and installed() and all(
        any(d.glob(p)) for p in ("encoder-model.int8.onnx", "decoder_joint-model.int8.onnx", "config.json", "vocab.txt"))


def download(name: str = DEFAULT) -> None:
    from huggingface_hub import snapshot_download
    snapshot_download(f"istupakov/{name}-onnx", local_dir=model_dir(name), allow_patterns=FILES)


def load(name: str = DEFAULT):
    with _lock:
        if name not in _models:
            if not ready(name):
                raise RuntimeError("the Parakeet model isn't downloaded yet (Integrations → Parakeet → Download)")
            import onnx_asr
            _models[name] = onnx_asr.load_model(f"nemo-{name}", model_dir(name), quantization="int8")
        return _models[name]


def transcribe(model, pcm16: bytes, rate: int) -> str:
    audio = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0
    if rate != RATE and audio.size:
        import soxr
        audio = soxr.resample(audio, rate, RATE).astype(np.float32)
    audio = np.concatenate([np.zeros(int(RATE * LEAD_SECS), np.float32), audio])
    return str(model.recognize(audio, sample_rate=RATE) or "").strip()


class Downloads:
    """Fetches a model in the background for the Integrations page."""

    def __init__(self):
        self.state, self.message = "idle", ""
        self._task: asyncio.Task | None = None

    def status(self, name: str = DEFAULT) -> dict:
        return {"state": self.state, "message": self.message, "installed": installed(), "ready": ready(name),
                "size_mb": MODELS.get(name, 0), "model": name}

    def start(self, name: str = DEFAULT) -> None:
        if not installed():
            raise RuntimeError("onnx-asr isn't installed (pip install onnx-asr)")
        if name not in MODELS:
            raise RuntimeError(f"unknown Parakeet model {name}")
        if self._task and not self._task.done():
            return
        self.state, self.message = "downloading", f"Downloading {name} ({MODELS[name]} MB)…"
        self._task = asyncio.create_task(self._run(name))

    async def _run(self, name: str) -> None:
        try:
            await asyncio.to_thread(download, name)
            self.message = "Loading the model…"
            await asyncio.to_thread(load, name)
            self.state, self.message = "idle", "Ready"
        except Exception as exc:  # noqa: BLE001 - shown in the UI
            self.state, self.message = "error", f"Download failed: {exc}"


class ParakeetSTTService(SegmentedSTTService):
    """Transcribes each turn when voice detection hears the caller stop."""

    def __init__(self, *, model: str = DEFAULT, **kwargs):
        super().__init__(**kwargs)
        self._model_name = model
        self._model = None

    @property
    def wants_wav_segments(self) -> bool:
        return False                     # raw 16-bit PCM

    def can_generate_metrics(self) -> bool:
        return True

    async def start(self, frame):
        await super().start(frame)
        self._model = await asyncio.to_thread(load, self._model_name)

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        if self._model is None:
            yield ErrorFrame(error="the Parakeet model isn't loaded")
            return
        await self.start_processing_metrics()
        text = await asyncio.to_thread(transcribe, self._model, audio, self.sample_rate or RATE)
        await self.stop_processing_metrics()
        if text:
            yield TranscriptionFrame(text, self._user_id, time_now_iso8601(), finalized=True)
