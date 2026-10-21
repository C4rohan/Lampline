"""A tougher Test Caller: background noise, another voice, interruptions, long pauses, and the agent's
response time measured from the caller's side.

An eval run can ask its Test Caller to make the call harder for the agent being tested:

    noise          room tone under the caller's voice: "room" (a busy room), "street" or "car", at a
                   level: "low", "medium" or "high" (about 25, 15 and 8 dB below the voice)
    voice          another voice for the caller, in the text-to-speech service you use (an accent, a
                   different age or pitch): a Kokoro voice like bf_emma, a Deepgram voice, a Fish Audio id
    style          how the caller talks: "hesitant", "terse", "rambling" or "non_native"
    interrupt      "sometimes" or "often": the caller cuts in while the agent is still talking
    pauses         "once" or "several": the caller goes quiet for 6 to 10 seconds before answering

All of it is decided up front from a seed, so a run can be played the same way again.

Whatever the options, the caller times the agent: from the moment the caller stops speaking to the moment
it hears the agent start, which is what a real caller waits, phone network included. Each one is an
"agent_latency" event, and the run keeps them all.
"""

from __future__ import annotations

import asyncio
import math
import random
import time
from dataclasses import dataclass
from typing import Any

import numpy as np
from pipecat.frames.frames import (BotStoppedSpeakingFrame, Frame, LLMFullResponseStartFrame, OutputAudioRawFrame,
                                   TTSSpeakFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

NOISES = ("room", "street", "car")
LEVELS = {"low": -25.0, "medium": -15.0, "high": -8.0}        # noise level relative to the voice, dB
STYLES = {
    "hesitant": "You hesitate: start some answers with \"um\" or \"uh\", and now and then begin a sentence, stop, "
                "and start it again.",
    "terse": "You give the shortest answers you can, often a word or two, and don't volunteer anything.",
    "rambling": "You give more detail than asked and sometimes wander off the point before getting to it.",
    "non_native": "English isn't your first language: use simple, sometimes slightly wrong grammar, and now and "
                  "then ask the agent to repeat or say it more simply.",
}
INTERRUPT = {"sometimes": 0.35, "often": 0.8}                  # chance per agent turn, after its first
PAUSES = {"once": 1, "several": 3}
INTERJECTIONS = ("Sorry, sorry, can I just say", "Wait, hang on", "Sorry to cut in, but", "Hold on a second",
                 "Sorry, quick question")
VAD_START = 0.2                                               # Silero's start delay: subtract it from what we heard


class StressError(ValueError):
    pass


@dataclass
class Stress:
    noise: str = ""
    level: str = "medium"
    voice: str = ""
    style: str = ""
    interrupt: str = ""
    pauses: str = ""
    seed: int = 0

    @classmethod
    def parse(cls, raw: Any) -> "Stress":
        """Checked options from an API body (unknown values are refused, not ignored)."""
        raw = raw if isinstance(raw, dict) else {}

        def pick(name: str, allowed) -> str:
            v = str(raw.get(name) or "").strip()
            if v and v not in allowed:
                raise StressError(f"{name} is one of {', '.join(allowed)}")
            return v

        voice = str(raw.get("voice") or "").strip()
        if voice and (len(voice) > 80 or not all(ch.isalnum() or ch in "-_." for ch in voice)):
            raise StressError("voice is a voice name or id, like bf_emma")
        seed = raw.get("seed")
        if not isinstance(seed, int) or isinstance(seed, bool):
            seed = random.SystemRandom().randrange(1, 2**31)
        return cls(noise=pick("noise", NOISES), level=pick("level", tuple(LEVELS)) or "medium", voice=voice,
                   style=pick("style", tuple(STYLES)), interrupt=pick("interrupt", tuple(INTERRUPT)),
                   pauses=pick("pauses", tuple(PAUSES)), seed=seed)

    def as_dict(self) -> dict[str, Any]:
        return {"noise": self.noise, "level": self.level, "voice": self.voice, "style": self.style,
                "interrupt": self.interrupt, "pauses": self.pauses, "seed": self.seed}

    @property
    def active(self) -> bool:
        return bool(self.noise or self.voice or self.style or self.interrupt or self.pauses)

    def persona_note(self) -> str:
        return f"How you speak: {STYLES[self.style]}" if self.style else ""

    def pause_turns(self, turns: int = 12) -> dict[int, float]:
        """Which of the caller's replies (1 = its first answer) wait, and for how long."""
        n = PAUSES.get(self.pauses, 0)
        if not n:
            return {}
        rng = random.Random(self.seed * 7 + 1)
        chosen = sorted(rng.sample(range(2, turns + 1), n)) if n > 1 else [rng.randint(2, 4)]
        return {t: round(rng.uniform(6.0, 10.0), 1) for t in chosen}


# ---------------------------------------------------------------------- noise

def make_noise(kind: str, seconds: float, rate: int, seed: int = 0) -> np.ndarray:
    """Room tone at unit RMS: a busy room (pink noise, swelling), a street (brown rumble with passing swells),
    or a car (a low engine hum over rumble)."""
    n = max(1, int(seconds * rate))
    rng = np.random.default_rng(seed)
    white = rng.standard_normal(n)
    t = np.arange(n) / rate
    if kind == "room":                         # pink: white noise shaped to 1/f, with slow swells like voices
        spec = np.fft.rfft(white)
        f = np.fft.rfftfreq(n, 1 / rate)
        spec *= 1 / np.sqrt(np.maximum(f, 20.0))
        x = np.fft.irfft(spec, n) * (1 + 0.4 * np.sin(2 * math.pi * 0.3 * t))
    elif kind == "street":                     # brown: integrated white, passing swells every few seconds
        x = np.cumsum(white)
        x -= np.convolve(x, np.ones(400) / 400, mode="same")    # keep it from drifting
        x *= 1 + 0.8 * np.clip(np.sin(2 * math.pi * 0.15 * t), 0, None)
    else:                                      # car: engine hum and its harmonics over rumble
        rumble = np.cumsum(white)
        rumble -= np.convolve(rumble, np.ones(400) / 400, mode="same")
        hum = sum(np.sin(2 * math.pi * f * t) / (i + 1) for i, f in enumerate((55, 110, 165)))
        x = rumble / (np.std(rumble) or 1) + 0.6 * hum
    return (x / (np.sqrt(np.mean(x ** 2)) or 1)).astype(np.float32)


class NoiseMixer(FrameProcessor):
    """Sits after text-to-speech: mixes room tone under the caller's voice, at a level relative to the voice."""

    LOOP_SECONDS = 6.0

    def __init__(self, stress: Stress):
        super().__init__(name="noise-mixer")
        self._kind, self._db, self._seed = stress.noise, LEVELS[stress.level], stress.seed
        self._loops: dict[int, np.ndarray] = {}
        self._pos = 0

    def _loop(self, rate: int) -> np.ndarray:
        if rate not in self._loops:
            # numpy work on a few seconds of audio: fast enough, done once per sample rate
            self._loops[rate] = make_noise(self._kind, self.LOOP_SECONDS, rate, self._seed)
        return self._loops[rate]

    def mix(self, pcm: bytes, rate: int, channels: int = 1) -> bytes:
        voice = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        if not voice.size:
            return pcm
        rms = float(np.sqrt(np.mean(voice ** 2))) or 1000.0
        rms = max(rms, 1000.0)                 # quiet stretches still get the room, at a speaking-level reference
        gain = rms * 10 ** (self._db / 20)
        loop = self._loop(rate)
        frames = voice.size // channels
        idx = (self._pos + np.arange(frames)) % loop.size
        self._pos = (self._pos + frames) % loop.size
        noise = np.repeat(loop[idx], channels) * gain
        return np.clip(voice + noise, -32768, 32767).astype(np.int16).tobytes()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputAudioRawFrame) and direction == FrameDirection.DOWNSTREAM and frame.audio:
            frame.audio = self.mix(frame.audio, frame.sample_rate, frame.num_channels)
        await self.push_frame(frame, direction)


# ---------------------------------------------------------------------- pauses

class PauseGate(FrameProcessor):
    """Sits before text-to-speech: on chosen replies the caller goes quiet for a while before answering.
    If the agent speaks up meanwhile ("are you still there?"), the caller's reply is interrupted as usual."""

    def __init__(self, session, stress: Stress):
        super().__init__(name="pause-gate")
        self._session = session
        self._plan = stress.pause_turns()
        self._replies = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMFullResponseStartFrame) and direction == FrameDirection.DOWNSTREAM:
            self._replies += 1
            wait = self._plan.get(self._replies)
            if wait:
                self._session.emit({"type": "stress", "kind": "pause", "seconds": wait})
                await asyncio.sleep(wait)
        await self.push_frame(frame, direction)


# ---------------------------------------------------------------------- the caller's clock, and cutting in

class CallerClock(FrameProcessor):
    """Sits at the end of the caller's pipeline. Times the agent from the caller's side, and cuts in on it.

    agent_latency: the caller stops speaking (its last audio played), then hears the agent start
    (voice detection's start, minus its own start delay).
    """

    def __init__(self, session, stress: Stress | None):
        super().__init__(name="caller-clock")
        self._session = session
        self._stress = stress
        self._rng = random.Random((stress.seed if stress else 0) * 13 + 5)
        self._caller_done: float | None = None
        self._agent_turns = 0
        self._cut_in: asyncio.Task | None = None

    def _plan_cut_in(self) -> None:
        s = self._stress
        if not s or not s.interrupt or self._agent_turns < 2:      # never on the agent's greeting
            return
        if self._rng.random() >= INTERRUPT[s.interrupt]:
            return
        delay = self._rng.uniform(1.0, 2.5)
        line = self._rng.choice(INTERJECTIONS)
        self._cut_in = asyncio.get_running_loop().create_task(self._cut(delay, line))

    async def _cut(self, delay: float, line: str) -> None:
        await asyncio.sleep(delay)
        self._session.emit({"type": "stress", "kind": "interruption", "text": line})
        self._session.emit({"type": "bot_speech", "text": line, "interruption": True,
                            "node_name": self._session.flow.current.name})
        await self._session.queue([TTSSpeakFrame(line)])

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, BotStoppedSpeakingFrame) and direction == FrameDirection.DOWNSTREAM:
            self._caller_done = time.monotonic()
        elif isinstance(frame, VADUserStartedSpeakingFrame):
            self._agent_turns += 1
            heard = time.monotonic() - float(getattr(frame, "start_secs", 0) or VAD_START)
            if self._caller_done is not None:
                ms = round((heard - self._caller_done) * 1000)
                if ms >= 0:
                    self._session.emit({"type": "agent_latency", "ms": ms})
                self._caller_done = None
            self._plan_cut_in()
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            if self._cut_in is not None and not self._cut_in.done():
                self._cut_in.cancel()          # the agent stopped before the caller got a word in
            self._cut_in = None
        await self.push_frame(frame, direction)

    async def cleanup(self):
        if self._cut_in is not None:
            self._cut_in.cancel()
        await super().cleanup()
