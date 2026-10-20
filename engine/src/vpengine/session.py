"""One running conversation: a Pipecat pipeline plus our flow controller.

TextSession: typed turns, no audio (test chats and `vk test --engine`).
VoiceSession: any Pipecat transport (browser WebRTC, Twilio, SIP, AudioHook)
with Deepgram speech in and out.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    EndFrame,
    ErrorFrame,
    Frame,
    InputDTMFFrame,
    LLMContextFrame,
    LLMFullResponseEndFrame,
    LLMFullResponseStartFrame,
    LLMMessagesAppendFrame,
    LLMTextFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.metrics.metrics import ProcessingMetricsData, TTFBMetricsData, TurnMetricsData
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.workers.runner import WorkerRunner
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from .flow import FlowController
from .graph import Graph
from .services import Config, make_llm, make_stt, make_tts, make_turn_strategies

log = logging.getLogger("vpengine.session")

IDLE_SETTLE = 0.3   # seconds the agent must stay quiet before a text turn is complete


class _BotTap(FrameProcessor):
    """Sits after the LLM: tracks responses and reports what the agent said."""

    def __init__(self, session: "Session"):
        super().__init__(name="bot-tap")
        self._session = session
        self._parts: list[str] = []

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            if isinstance(frame, LLMFullResponseStartFrame):
                self._parts = []
                self._session.flow.on_response_started()
            elif isinstance(frame, LLMTextFrame):
                self._parts.append(frame.text)
            elif isinstance(frame, LLMFullResponseEndFrame):
                text = "".join(self._parts).strip()
                self._parts = []
                if text:
                    self._session.emit({"type": "bot_speech", "text": text,
                                        "node_name": self._session.flow.current.name})
                await self.push_frame(frame, direction)
                await self._session.flow.on_response_ended()
                return
        await self.push_frame(frame, direction)


class _CallerTap(FrameProcessor):
    """Sits after speech-to-text: reports what the caller said, and hands keypad presses to the flow."""

    def __init__(self, session: "Session"):
        super().__init__(name="caller-tap")
        self._session = session

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            self._session.emit({"type": "user_speech", "text": frame.text.strip()})
        elif isinstance(frame, InputDTMFFrame):
            key = getattr(frame.button, "value", str(frame.button))
            asyncio.create_task(self._session.flow.keypad(str(key)))
        await self.push_frame(frame, direction)


class _LatencyTap(FrameProcessor):
    """Sits at the end of the pipeline: one turn_metrics event per agent reply.

    response_ms runs from the caller's last word (the moment voice detection
    heard silence, minus its own wait) to the agent's first sound. The parts
    are what Pipecat measured on the way: the smart-turn decision, the time
    to first result from listening, the model and the voice.
    """

    def __init__(self, session: "Session"):
        super().__init__(name="latency-tap")
        self._session = session
        self._speech_end: float | None = None
        self._parts: dict[str, int] = {}

    def _kind(self, processor: str) -> str | None:
        s = self._session
        for kind, svc in (("listening", getattr(s, "stt", None)), ("model", s.llm), ("speaking", getattr(s, "tts", None))):
            if svc is not None and processor == getattr(svc, "name", None):
                return kind
        return None

    def _report(self, response_ms: int | None) -> None:
        if response_ms is None and not self._parts:
            return
        self._session.emit({"type": "turn_metrics", "response_ms": response_ms, **self._parts})
        self._parts = {}
        self._speech_end = None

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, MetricsFrame):
            for d in frame.data:
                if isinstance(d, TurnMetricsData):
                    self._parts["turn_ms"] = round(d.e2e_processing_time_ms)
                elif isinstance(d, (TTFBMetricsData, ProcessingMetricsData)) and d.value > 0:
                    kind = self._kind(d.processor)
                    if kind:
                        self._parts.setdefault(f"{kind}_ms", round(d.value * 1000))
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            # a new caller turn: start measuring from here
            self._parts = {}
            self._speech_end = time.monotonic() - float(getattr(frame, "stop_secs", 0) or 0)
        elif isinstance(frame, BotStartedSpeakingFrame) and direction == FrameDirection.DOWNSTREAM:
            if self._speech_end is not None:
                self._report(round((time.monotonic() - self._speech_end) * 1000))
            else:
                self._parts = {}          # the greeting, or the agent speaking unprompted
        elif isinstance(frame, LLMFullResponseEndFrame) and not hasattr(self._session, "tts"):
            self._report(None)            # text chat: the model's time is the whole story
        await self.push_frame(frame, direction)


def _last_user_text(context: LLMContext) -> str:
    for m in reversed(context.get_messages()):
        if isinstance(m, dict) and m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if isinstance(p, dict)).strip()
            return str(c or "").strip()
    return ""


class _ScriptGate(FrameProcessor):
    """Sits before the LLM: on a scripted page the caller's turn goes to the script, not the model."""

    def __init__(self, session: "Session"):
        super().__init__(name="script-gate")
        self._session = session

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        flow = self._session.flow
        if (direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMContextFrame)
                and flow.current is not None and flow.current.scripted):
            if not getattr(frame, "speculation", False):
                text = _last_user_text(frame.context)
                if text:
                    asyncio.create_task(flow.caller_turn(text))
                else:
                    flow.awaiting_response = False
            return
        await self.push_frame(frame, direction)


class Session:
    """Base class. Subclasses build the pipeline and say things."""

    def __init__(self, graph: Graph, cfg: Config, *, variables: dict[str, Any] | None = None,
                 on_event: Callable[[dict], None] | None = None, llm=None, tools=None, meta: dict | None = None):
        self.id = uuid.uuid4().hex[:16]
        self.meta: dict[str, Any] = dict(meta or {})     # agent folder, channel, contact: for after-call triggers
        self.graph = graph
        self.cfg = cfg
        self.created = time.time()
        self.events: list[dict] = []
        self._listeners: list[Callable[[dict], None]] = [on_event] if on_event else []
        self.llm = llm or make_llm(cfg)
        self.context = LLMContext()
        self.flow = FlowController(graph, self.context, self.llm, self, variables, tools)
        self.task: PipelineWorker | None = None
        self._runner_task: asyncio.Task | None = None
        self.finished = asyncio.Event()
        self.error: str | None = None
        self.transfer_to = None                     # the Transfer, once the call is handed to a person
        self.on_transfer = None                     # async (Transfer) -> None: set by the phone handlers

    # -- Host protocol for FlowController ---------------------------------

    def emit(self, event: dict) -> None:
        event = {**event, "t": round(time.time() - self.created, 3)}
        self.events.append(event)
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:  # noqa: BLE001
                log.exception("event listener failed")

    def subscribe(self, fn: Callable[[dict], None]) -> Callable[[], None]:
        self._listeners.append(fn)
        return lambda: self._listeners.remove(fn) if fn in self._listeners else None

    async def queue(self, frames: list) -> None:
        assert self.task is not None
        await self.task.queue_frames(frames)

    async def end(self, reason: str) -> None:
        if self.task is not None:
            await self.task.queue_frame(EndFrame())

    async def say(self, text: str) -> None:
        raise NotImplementedError

    async def transfer(self, transfer) -> None:
        """Remember the hand-off; a phone call's carrier acts on it (on_transfer), a test chat just reports it."""
        self.transfer_to = transfer
        if self.on_transfer is not None:
            await self.on_transfer(transfer)

    # -- lifecycle ---------------------------------------------------------

    def build(self) -> Pipeline:
        raise NotImplementedError

    def task_params(self) -> PipelineParams:
        return PipelineParams()

    async def run(self) -> None:
        """Run until the call ends (voice sessions await this directly)."""
        self.task = PipelineWorker(self.build(), params=self.task_params(), idle_timeout_secs=None,
                                 enable_rtvi=False, check_dangling_tasks=False)

        @self.task.event_handler("on_pipeline_error")
        async def _on_error(task, frame: ErrorFrame):
            self.error = str(frame.error)
            self.flow.awaiting_response = False
            self.flow.responding = False
            self.emit({"type": "execution_error", "message": str(frame.error)})

        @self.task.event_handler("on_pipeline_started")
        async def _on_started(task, *args):
            await self.on_started()

        runner = WorkerRunner(handle_sigint=False, check_dangling_tasks=False)
        try:
            await runner.add_workers(self.task)
            await runner.run()
        finally:
            try:
                await self.flow.runner.aclose()
            except Exception:  # noqa: BLE001 - closing MCP sessions is best effort
                pass
            if not self.flow.ended:
                self.flow.ended = True
                self.emit({"type": "call_ended", "reason": "disconnected",
                           "node_name": self.flow.current.name if self.flow.current else None,
                           "variables": dict(self.flow.variables)})
            self.finished.set()

    async def on_started(self) -> None:
        await self.flow.start()

    async def stop(self) -> None:
        if self.task is not None and not self.finished.is_set():
            await self.task.cancel()

    def summary(self) -> dict:
        names = [self.graph.steps[i].name for i in self.flow.visited]
        return {"session_id": self.id, "agent": self.graph.name, "ended": self.flow.ended,
                "current_step": self.flow.current.name if self.flow.current else None,
                "steps": names, "variables": dict(self.flow.variables)}


class TextSession(Session):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.aggregators = LLMContextAggregatorPair(self.context)
        self._started = asyncio.Event()

    def task_params(self) -> PipelineParams:
        return PipelineParams(enable_metrics=True)

    def build(self) -> Pipeline:
        return Pipeline([self.aggregators.user(), _ScriptGate(self), self.llm, _BotTap(self), _LatencyTap(self),
                         self.aggregators.assistant()])

    async def say(self, text: str) -> None:
        self.context.add_message({"role": "assistant", "content": text})
        self.emit({"type": "bot_speech", "text": text, "node_name": self.flow.current.name})

    async def announce(self, text: str) -> None:
        self.emit({"type": "bot_speech", "text": text, "node_name": self.flow.current.name, "filler": True})

    async def on_started(self) -> None:
        await super().on_started()
        self._started.set()

    async def open(self, timeout: float = 60.0) -> list[dict]:
        """Start the pipeline and return the opening events (greeting)."""
        self._runner_task = asyncio.create_task(self.run())
        await asyncio.wait_for(self._wait_started(), timeout)
        return await self._settle(0, timeout)

    async def _wait_started(self) -> None:
        start = asyncio.create_task(self._started.wait())
        done = asyncio.create_task(self.finished.wait())
        await asyncio.wait({start, done}, return_when=asyncio.FIRST_COMPLETED)
        for t in (start, done):
            t.cancel()
        if not self._started.is_set():
            raise RuntimeError(self.error or "session stopped before it started")

    async def send(self, text: str, timeout: float = 90.0) -> list[dict]:
        if self.flow.ended:
            raise RuntimeError("the conversation has ended")
        mark = len(self.events)
        self.emit({"type": "user_speech", "text": text})
        self.error = None
        self.flow.awaiting_response = True
        await self.queue([LLMMessagesAppendFrame(messages=[{"role": "user", "content": text}], run_llm=True)])
        return await self._settle(mark, timeout)

    async def _settle(self, mark: int, timeout: float) -> list[dict]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        quiet_since: float | None = None
        while True:
            now = loop.time()
            if self.flow.ended or self.finished.is_set():
                break
            if self.flow.busy:
                quiet_since = None
            else:
                quiet_since = quiet_since or now
                if now - quiet_since >= IDLE_SETTLE:
                    break
            if now > deadline:
                self.emit({"type": "execution_error", "message": f"no reply within {timeout:.0f}s"})
                break
            await asyncio.sleep(0.02)
        if self.flow.ended:
            await asyncio.wait({asyncio.create_task(self.finished.wait())}, timeout=5)
        return self.events[mark:]

    async def close(self) -> None:
        if not self.finished.is_set():
            await self.stop()
        if self._runner_task is not None:
            await asyncio.wait({self._runner_task}, timeout=5)


class VoiceSession(Session):
    """A call over any Pipecat transport (its input()/output() processors)."""

    def __init__(self, graph: Graph, cfg: Config, transport, *, audio_in_rate: int = 16000,
                 audio_out_rate: int = 24000, vad=None, autostart: bool = True, stt=None, tts=None, **kwargs):
        super().__init__(graph, cfg, **kwargs)
        self.transport = transport
        self.autostart = autostart   # False: call begin() once the caller is connected
        self.stt = stt or make_stt(cfg)
        self.tts = tts or make_tts(cfg)
        self._begun = False
        self.audio_in_rate = audio_in_rate
        self.audio_out_rate = audio_out_rate
        from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams

        if vad is None:
            from pipecat.audio.vad.silero import SileroVADAnalyzer
            vad = SileroVADAnalyzer()
        self.aggregators = LLMContextAggregatorPair(self.context, user_params=LLMUserAggregatorParams(
            vad_analyzer=vad, user_turn_strategies=make_turn_strategies(cfg)))

    async def on_started(self) -> None:
        if self.autostart:
            await self.begin()

    async def begin(self) -> None:
        """Greet the caller. Safe to call more than once."""
        if not self._begun:
            self._begun = True
            await self.flow.start()

    def task_params(self) -> PipelineParams:
        return PipelineParams(audio_in_sample_rate=self.audio_in_rate, audio_out_sample_rate=self.audio_out_rate,
                              enable_metrics=True)

    def build(self) -> Pipeline:
        return Pipeline([
            self.transport.input(),
            self.stt,
            _CallerTap(self),
            self.aggregators.user(),
            _ScriptGate(self),
            self.llm,
            _BotTap(self),
            self.tts,
            self.transport.output(),
            _LatencyTap(self),
            self.aggregators.assistant(),
        ])

    async def say(self, text: str) -> None:
        self.emit({"type": "bot_speech", "text": text, "node_name": self.flow.current.name})
        await self.queue([TTSSpeakFrame(text)])

    async def announce(self, text: str) -> None:
        self.emit({"type": "bot_speech", "text": text, "node_name": self.flow.current.name, "filler": True})
        await self.queue([TTSSpeakFrame(text, append_to_context=False)])
