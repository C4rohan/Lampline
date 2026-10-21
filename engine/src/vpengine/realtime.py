"""Speech to speech (experimental): one model that listens and talks.

A normal voice call is a chain: speech to text, the text model, then text to
speech. A speech-to-speech model takes the caller's audio and answers with
audio itself, so there's no wait for a transcript or a voice, and it hears how
things are said, not just the words.

Two kinds of model: Google's Gemini Live (VPE_REALTIME_PROVIDER=gemini, gemini_live.py), or any server
that speaks the OpenAI Realtime protocol:
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


def problem(graph: Graph, cfg: Config) -> str | None:
    """Why this agent can't run speech to speech now, or None."""
    if cfg.realtime_provider == "gemini":
        if not cfg.gemini_key:
            return "Gemini Live needs a Gemini API key (Integrations → Gemini)"
    elif not cfg.realtime_api_key and cfg.realtime_is_openai:
        return "speech to speech needs an OpenAI API key (Integrations → Speech to speech)"
    scripted = [s.name for s in graph.steps.values() if getattr(s, "scripted", False)]
    if scripted:
        return (f"speech to speech doesn't run scripted pages yet ({', '.join(scripted[:3])}); "
                "this agent uses the normal voice chain")
    return None


def make_realtime_llm(cfg: Config):
    if cfg.realtime_provider == "gemini":
        from .gemini_live import make_gemini_live
        return make_gemini_live(cfg)
    from pipecat.services.openai.realtime import events
    from pipecat.services.openai.realtime.llm import OpenAIRealtimeLLMService

    class LamplineRealtime(OpenAIRealtimeLLMService):
        """Pipecat's OpenAI Realtime service, run one response at a time for a flow of steps.

        The Realtime API rejects a new response while one is playing, but a step change can ask for two in a
        row (a transition's line, then the next step's turn). So responses we start wait their turn here, and
        each is tagged ("say", "run" or "auto" when the server answered the caller itself), so the call only
        ends after the end step's own reply, not after a line said on the way to it.
        """

        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.lp_busy = False
            self.lp_kind = "auto"
            self._lp_waiting: list[tuple[str, str]] = []
            self.lp_run_pending = False

        async def _lp_begin(self, kind: str, text: str = "") -> None:
            if self.lp_busy or not self._api_session_ready:
                self._lp_waiting.append((kind, text))
                return
            self.lp_busy, self.lp_kind = True, kind
            if kind == "say":
                await self.send_client_event(events.ResponseCreateEvent(response=events.ResponseProperties(
                    instructions=f"Say exactly this, word for word, and nothing else: {text}")))
            else:
                await super()._create_response()

        async def _lp_drain(self) -> None:
            if self._lp_waiting and not self.lp_busy:
                kind, text = self._lp_waiting.pop(0)
                await self._lp_begin(kind, text)

        async def say_exactly(self, text: str) -> None:
            await self._lp_begin("say", text)

        async def _create_response(self):
            await self._lp_begin("run")

        async def _handle_context(self, context):
            # A step change asks the model to go on (LLMRunFrame). Pipecat answers only when a new tool result
            # went out with it; entering a step by a rule or after a greeting has none, so start the turn here.
            before = len(self._completed_tool_calls)
            await super()._handle_context(context)
            if self.lp_run_pending and len(self._completed_tool_calls) == before:
                await self._lp_begin("run")
            self.lp_run_pending = False

        async def _handle_evt_conversation_item_added(self, evt):
            if getattr(evt.item, "role", None) == "assistant" and not self.lp_busy:
                self.lp_busy, self.lp_kind = True, "auto"            # the server answered the caller on its own
            await super()._handle_evt_conversation_item_added(evt)

        async def _handle_evt_session_updated(self, evt):
            await super()._handle_evt_session_updated(evt)
            await self._lp_drain()

        async def send_client_event(self, event):
            # A step without tools must clear the last step's: Pipecat leaves tools out of the update when
            # the list is empty, and the server then keeps the old ones.
            if isinstance(event, events.SessionUpdateEvent) and self._context is not None and event.session.tools is None:
                event.session.tools = []
            await super().send_client_event(event)

        async def push_frame(self, frame, *args, **kwargs):
            stamp(frame, self.lp_kind)
            await super().push_frame(frame, *args, **kwargs)

        async def _handle_evt_response_done(self, evt):
            await super()._handle_evt_response_done(evt)
            self.lp_busy = False
            await self._lp_drain()

    turn = (events.SemanticTurnDetection(eagerness="auto") if cfg.realtime_turns == "semantic"
            else events.TurnDetection(silence_duration_ms=int(float(cfg.turn_pause or 0.5) * 1000)))
    props = events.SessionProperties(audio=events.AudioConfiguration(
        input=events.AudioInput(transcription=events.InputAudioTranscription(), turn_detection=turn),
        output=events.AudioOutput(voice=cfg.realtime_voice or None)))
    return LamplineRealtime(
        api_key=cfg.realtime_api_key or "none", base_url=cfg.realtime_url,
        settings=OpenAIRealtimeLLMService.Settings(model=cfg.realtime_model, session_properties=props))


def stamp(frame, kind: str) -> None:
    """Tag a response's start and end frames with what kind of response it was ("say", "run" or "auto") as they
    leave the model. Read later, downstream, the model may already be on its next response."""
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame

    if isinstance(frame, (LLMFullResponseStartFrame, LLMFullResponseEndFrame)):
        frame.lp_kind = kind


class _RealtimeBotTap(_BotTap):
    """Lines said word for word ("say") are reported, but only the model's own turns count as replies,
    so a transition's line into an end step doesn't end the call before the goodbye."""

    async def process_frame(self, frame, direction):
        from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame
        from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

        llm = self._session.llm
        kind = getattr(frame, "lp_kind", None) or getattr(llm, "lp_kind", "run")   # as it left the model
        if direction == FrameDirection.DOWNSTREAM and kind == "say" \
                and isinstance(frame, (LLMFullResponseStartFrame, LLMFullResponseEndFrame)):
            await FrameProcessor.process_frame(self, frame, direction)
            if isinstance(frame, LLMFullResponseStartFrame):
                self._parts = []
            else:
                text = "".join(self._parts).strip()
                self._parts = []
                if text:
                    self._session.emit({"type": "bot_speech", "text": text, "node_name": self._session.flow.current.name})
            await self.push_frame(frame, direction)
            return
        await super().process_frame(frame, direction)


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
            if hasattr(self.llm, "lp_connect"):
                # Gemini Live: connect once the first step is known (after Start's greeting and hand-over)
                from .gemini_live import all_tools
                await self.flow.start()
                await self.llm.lp_connect(self.flow, self.context, all_tools(self.graph, self.flow))
                return
            if getattr(self.llm, "_context", False) is None:
                self.llm._context = self.context
                self.llm._llm_needs_conversation_setup = False     # and it has nothing earlier to replay
            await self.flow.start()

    def task_params(self) -> PipelineParams:
        return PipelineParams(audio_in_sample_rate=RATE, audio_out_sample_rate=RATE, enable_metrics=True,
                              enable_usage_metrics=True)

    def build(self) -> Pipeline:
        return Pipeline([
            self.transport.input(),
            self.aggregators.user(),
            _CallerTap(self),                  # the model's transcript of the caller comes back upstream from it
            self.llm,
            _RealtimeBotTap(self),
            self.transport.output(),
            *([_recorder(self.record_to)] if self.record_to else []),
            _LatencyTap(self),
            self.aggregators.assistant(),
        ])

    async def queue(self, frames: list) -> None:
        from pipecat.frames.frames import LLMRunFrame

        if any(isinstance(f, LLMRunFrame) for f in frames) and hasattr(self.llm, "lp_run_pending"):
            self.llm.lp_run_pending = True
        await super().queue(frames)

    async def _speak(self, text: str) -> None:
        if text.strip():
            await self.llm.say_exactly(text)

    async def say(self, text: str) -> None:
        # bot_speech comes from the model's own transcript of what it said (_RealtimeBotTap)
        await self._speak(text)

    async def announce(self, text: str) -> None:
        await self._speak(text)
