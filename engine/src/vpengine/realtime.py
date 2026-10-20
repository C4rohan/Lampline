"""Speech to speech (experimental): one model that listens and talks.

A normal voice call is a chain: speech to text, the text model, then text to
speech. A speech-to-speech model takes the caller's audio and answers with
audio itself, so there's no wait for a transcript or a voice, and it hears how
things are said, not just the words.

This runs voice calls on any server that speaks the OpenAI Realtime protocol:
OpenAI's gpt-realtime models, or one you host yourself that implements the
same protocol (for example huggingface/speech-to-speech in its "realtime"
mode, which is a chain inside but presents the same interface). Steps,
transfers and tools work as before: each step's instructions and tools are
sent to the model as the call moves through the agent. Two things differ:

- Lines the agent must say word for word (Start's greeting, tool "say while
  running" lines, transfer speech) are asked of the model as "say exactly";
  the wording is the model's, so it can differ slightly.
- Scripted pages aren't supported: they need exact prompts, keypad entry and
  form filling from a transcript. Agents with scripted pages keep the normal
  chain, and the call page says why.

Extraction ("collect details" on a step) still uses the text model, so a text
model key is needed too. Off by default: VPE_VOICE_MODE=speech-to-speech.
"""

from __future__ import annotations

import asyncio
import logging

from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair

from .graph import Graph
from .services import Config, ConfigError
from .session import Session, _BotTap, _CallerTap, _LatencyTap, _recorder

log = logging.getLogger("vpengine.realtime")

RATE = 24000                   # the Realtime protocol's audio: 16-bit PCM at 24 kHz
READY_WAIT = 10.0              # seconds to wait for the model's session before the first line


def problem(graph: Graph, cfg: Config) -> str | None:
    """Why this agent can't run speech to speech now, or None."""
    if not cfg.realtime_api_key and "api.openai.com" in cfg.realtime_url:
        return "speech to speech needs an OpenAI API key (Integrations → Speech to speech)"
    scripted = [s.name for s in graph.steps.values() if getattr(s, "scripted", False)]
    if scripted:
        return (f"speech to speech doesn't run scripted pages yet ({', '.join(scripted[:3])}); "
                "this agent uses the normal voice chain")
    return None


def make_realtime_llm(cfg: Config):
    from pipecat.services.openai.realtime import events
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    turn = (events.SemanticTurnDetection(eagerness="auto") if cfg.realtime_turns == "semantic"
            else events.TurnDetection(silence_duration_ms=int(float(cfg.turn_pause or 0.5) * 1000)))
    props = events.SessionProperties(audio=events.AudioConfiguration(
        input=events.AudioInput(transcription=events.InputAudioTranscription(), turn_detection=turn),
        output=events.AudioOutput(voice=cfg.realtime_voice or None)))
    return OpenAIRealtimeLLMService(
        api_key=cfg.realtime_api_key or "none", base_url=cfg.realtime_url,
        settings=OpenAIRealtimeLLMService.Settings(model=cfg.realtime_model, session_properties=props))


class RealtimeVoiceSession(Session):
    """A call where one speech-to-speech model listens and talks; the flow controller still drives the steps."""

    voice = True

    def __init__(self, graph: Graph, cfg: Config, transport, *, autostart: bool = True, extract_llm=None, **kwargs):
        from .services import make_llm

        if kwargs.get("llm") is None:
            kwargs["llm"] = make_realtime_llm(cfg)
        super().__init__(graph, cfg, **kwargs)
        self.transport = transport
        self.autostart = autostart
        self.record_to = None
        self._begun = False
        try:                                   # extraction reads the transcript with the text model
            self.flow.extract_llm = extract_llm or make_llm(cfg)
        except ConfigError:
            self.flow.extract_llm = None
        self.aggregators = LLMContextAggregatorPair(self.context)

    async def on_started(self) -> None:
        if self.autostart:
            await self.begin()

    async def begin(self) -> None:
        if not self._begun:
            self._begun = True
            # Pipecat's realtime service takes its first context frame as the start of the conversation and
            # marks tool results already in it as sent. Our first context frame comes after Start's route is
            # taken, so give it the (still empty) context now: that route's result then reaches the model.
            if getattr(self.llm, "_context", False) is None:
                self.llm._context = self.context
            await self.flow.start()

    def task_params(self) -> PipelineParams:
        return PipelineParams(audio_in_sample_rate=RATE, audio_out_sample_rate=RATE, enable_metrics=True)

    def build(self) -> Pipeline:
        return Pipeline([
            self.transport.input(),
            self.aggregators.user(),
            _CallerTap(self),                  # the model's transcript of the caller comes back upstream from it
            self.llm,
            _BotTap(self),
            self.transport.output(),
            *([_recorder(self.record_to)] if self.record_to else []),
            _LatencyTap(self),
            self.aggregators.assistant(),
        ])

    async def _ready(self) -> bool:
        for _ in range(int(READY_WAIT / 0.05)):
            if getattr(self.llm, "_api_session_ready", True):
                return True
            await asyncio.sleep(0.05)
        return False

    async def _speak(self, text: str) -> None:
        from pipecat.services.openai.realtime import events

        if not text.strip() or not await self._ready():
            return
        await self.llm.send_client_event(events.ResponseCreateEvent(response=events.ResponseProperties(
            instructions=f"Say exactly this, word for word, and nothing else: {text}")))

    async def say(self, text: str) -> None:
        # bot_speech comes from the model's own transcript of what it said (_BotTap)
        await self._speak(text)

    async def announce(self, text: str) -> None:
        await self._speak(text)
