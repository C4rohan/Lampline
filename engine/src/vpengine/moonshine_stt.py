"""Moonshine: streaming speech to text on this computer (MIT; https://github.com/moonshine-ai/moonshine).

Whisper transcribes a turn only after the caller stops, so the caller waits for
the whole transcription. Moonshine's streaming models transcribe while the
caller talks: audio goes in 20 ms at a time, the text updates a few times a
second, and when voice detection hears silence one last update finishes the
line. On a laptop CPU that final update takes milliseconds for a short turn.

Measured on a 2-CPU cloud machine, from the end of speech to the final text:
Whisper base.en 0.66 s, Whisper tiny.en 0.35 s, Moonshine small 0.30 s,
Moonshine tiny 0.17 s. The cost is CPU while the caller talks (about half a
core per call for small, a third for tiny), so a small machine handles fewer
calls at once.

English only here: Moonshine's English models are MIT licensed; some other
languages' models are under a non-commercial licence.

One model is loaded per engine and shared; each call gets its own stream.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path
from typing import AsyncGenerator

import numpy as np
from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.stt_service import STTService
from pipecat.utils.time import time_now_iso8601

log = logging.getLogger("vpengine.moonshine")

RATE = 16000                 # Moonshine listens at 16 kHz
MODELS = {"small-streaming": 140, "tiny-streaming": 46}   # English streaming models, MB to download
DEFAULT = "small-streaming"
UPDATE_SECS = 0.25           # how often the text refreshes while the caller talks

_lock = threading.Lock()     # one model at a time, used by one call at a time
_transcribers: dict[str, object] = {}


def installed() -> bool:
    try:
        import moonshine_voice  # noqa: F401
    except ImportError:
        return False
    return True


def model_dir() -> Path:
    from .builtin_speech import CACHE
    return CACHE / "moonshine"


def _group(arch: str) -> dict:
    """The model's files, as Moonshine's manifest lists them (no network needed)."""
    from moonshine_voice.download import _primary_stt_group, find_model_info  # pinned version: see pyproject
    from moonshine_voice.moonshine_api import string_to_model_arch

    return _primary_stt_group(find_model_info("en", string_to_model_arch(arch)))


def _root(group: dict) -> Path:
    return model_dir() / group["base_url"].replace("https://", "")


def ready(arch: str = DEFAULT) -> bool:
    if not installed() or arch not in MODELS:
        return False
    try:
        group = _group(arch)
    except Exception:  # noqa: BLE001 - an unexpected manifest means "not ready", not a crash
        return False
    root = _root(group)
    return all((root / f["name"]).is_file() for f in group.get("files", []))


def download(arch: str = DEFAULT, on_progress=None) -> None:
    """Fetch a model once; later calls load it from disk."""
    from moonshine_voice import get_model_for_language
    from moonshine_voice.moonshine_api import string_to_model_arch

    get_model_for_language("en", string_to_model_arch(arch), cache_root=model_dir(), on_progress=on_progress)


def load(arch: str = DEFAULT):
    """The shared transcriber for this model, loaded on first use."""
    with _lock:
        if arch not in _transcribers:
            if not ready(arch):
                raise RuntimeError("the Moonshine model isn't downloaded yet (Integrations → Moonshine → Download)")
            from moonshine_voice import Transcriber
            from moonshine_voice.moonshine_api import string_to_model_arch

            _transcribers[arch] = Transcriber(str(_root(_group(arch))), string_to_model_arch(arch),
                                              update_interval=UPDATE_SECS)
        return _transcribers[arch]


class Downloads:
    """Fetches the model in the background and reports progress to the Integrations page."""

    def __init__(self):
        self.state = "idle"            # idle | downloading | error
        self.message = ""
        self.progress: float | None = None
        self._task: asyncio.Task | None = None

    def status(self, arch: str = DEFAULT) -> dict:
        return {"state": self.state, "message": self.message, "installed": installed(), "ready": ready(arch),
                "progress": self.progress, "size_mb": MODELS.get(arch, 0), "model": arch}

    def start(self, arch: str = DEFAULT) -> None:
        if not installed():
            raise RuntimeError("Moonshine isn't installed on this computer (it needs Linux or an Apple-silicon Mac)")
        if self._task and not self._task.done():
            return
        self.state, self.message, self.progress = "downloading", "Downloading the Moonshine model…", 0.0
        self._task = asyncio.create_task(self._run(arch))

    async def _run(self, arch: str) -> None:
        def progress(fraction, *_):
            self.progress = round(float(fraction), 3)

        try:
            await asyncio.to_thread(download, arch, progress)
            self.message = "Loading the model…"
            await asyncio.to_thread(load, arch)
            self.state, self.message, self.progress = "idle", "Ready", None
        except Exception as exc:  # noqa: BLE001 - shown in the UI
            self.state, self.message = "error", f"Download failed: {exc}"


class MoonshineSTTService(STTService):
    """Streaming Moonshine inside a Pipecat pipeline.

    Interim text goes out as InterimTranscriptionFrame while the caller talks.
    A line Moonshine completes on its own goes out as a TranscriptionFrame; when
    voice detection hears the caller stop, the line in progress is finished at
    once and sent as a finalized TranscriptionFrame, so the turn never waits
    for Moonshine's own end-of-line timing.
    """

    def __init__(self, *, model: str = DEFAULT, **kwargs):
        super().__init__(**kwargs)
        self._model = model
        self._stream = None
        self._events: list = []
        self._sent: dict[int, str] = {}       # line id -> text already sent as final
        self._work = asyncio.Lock()           # one Moonshine call at a time for this stream
        self._resampler = create_stream_resampler()

    def can_generate_metrics(self) -> bool:
        return True

    # -- lifecycle -------------------------------------------------------------

    async def start(self, frame: StartFrame):
        await super().start(frame)
        transcriber = await asyncio.to_thread(load, self._model)

        def open_stream():
            with _lock:
                stream = transcriber.create_stream(update_interval=UPDATE_SECS)
                stream.add_listener(self._events.append)
                stream.start()
                return stream

        self._stream = await asyncio.to_thread(open_stream)

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._close()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._close()

    async def _close(self):
        stream, self._stream = self._stream, None
        if stream is None:
            return

        def close():
            with _lock:
                try:
                    stream.stop()
                finally:
                    stream.close()

        async with self._work:
            await asyncio.to_thread(close)

    # -- audio in, text out ----------------------------------------------------

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame, None]:
        if self._stream is None:
            yield None
            return
        audio = await self._resampler.resample(audio, self.sample_rate, RATE)
        if not audio:
            yield None
            return
        samples = (np.frombuffer(audio, dtype=np.int16).astype(np.float32) / 32768.0).tolist()

        def feed():
            with _lock:
                self._stream.add_audio(samples, RATE)

        async with self._work:
            if self._stream is not None:
                await asyncio.to_thread(feed)
        for frame in self._drain():
            yield frame

    def _drain(self) -> list[Frame]:
        from moonshine_voice.transcriber import LineCompleted, LineTextChanged

        frames: list[Frame] = []
        events, self._events[:] = list(self._events), []
        for event in events:
            line = event.line
            text = (line.text or "").strip()
            if not text:
                continue
            if isinstance(event, LineCompleted):
                rest = self._unsent(line.line_id, text)
                if rest:
                    frames.append(TranscriptionFrame(rest, self._user_id, time_now_iso8601()))
            elif isinstance(event, LineTextChanged) and line.line_id not in self._sent:
                frames.append(InterimTranscriptionFrame(text, self._user_id, time_now_iso8601()))
        return frames

    def _unsent(self, line_id: int, text: str) -> str:
        """What hasn't gone out yet for this line (we may have finished it early)."""
        before = self._sent.get(line_id, "")
        self._sent[line_id] = text
        if not before:
            return text
        if text.lower().startswith(before.lower()):
            return text[len(before):].strip(" ,.")
        return ""          # Moonshine revised a line we already sent: keep what the agent heard

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStoppedSpeakingFrame) and self._stream is not None:
            await self._finish_line()

    async def _finish_line(self) -> None:
        """The caller paused: finish the line in progress now instead of waiting for Moonshine."""
        from moonshine_voice.transcriber import MOONSHINE_FLAG_FORCE_UPDATE

        def update():
            with _lock:
                return self._stream.update_transcription(MOONSHINE_FLAG_FORCE_UPDATE)

        async with self._work:
            if self._stream is None:
                return
            transcript = await asyncio.to_thread(update)
        for frame in self._drain():          # anything the update completed on its own
            await self.push_frame(frame)
        for line in getattr(transcript, "lines", None) or []:
            text = (line.text or "").strip()
            if text and not line.is_complete:
                rest = self._unsent(line.line_id, text)
                if rest:
                    final = TranscriptionFrame(rest, self._user_id, time_now_iso8601())
                    final.finalized = True
                    await self.push_frame(final)
