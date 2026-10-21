"""Speech to speech on Google's Gemini Live, next to the OpenAI Realtime protocol (realtime.py).

Gemini Live sets a session's instructions and tools once, when it connects; it can't swap them as the call moves
through an agent's steps. So Lampline:

- connects when the call begins, with the current step's instructions (after Start's greeting and hand-over),
  a short note on how steps work, and every step's tools;
- on each step change, sends the new step's instructions and the tools it may use as a note from the call system.
  When the model moved the call on itself (it called a route), the note rides in that route's result, which Gemini
  reads before it carries on; otherwise it goes in as a turn, marked as not from the caller;
- asks for lines said word for word ("say exactly") the same way, one response at a time.

A tool the current step doesn't have is refused by the flow with "no tool named ... here", and the model carries on.
Uses the Gemini API key (GEMINI_API_KEY); the model and voice are VPE_GEMINI_LIVE_MODEL and VPE_GEMINI_LIVE_VOICE.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.adapters.services.gemini_live_adapter import GeminiLiveLLMAdapter
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.services.llm_service import LLMService

log = logging.getLogger("vpengine.gemini_live")

NOTE = "[A note from the call system, not the caller.]"
STEPS = ("This call moves through steps. When the step changes, the call system tells you, in a note marked as "
         "not from the caller, with the new step's instructions and the tools you may use in it. From then on, follow "
         "the latest step's instructions and use only its tools. Never read these notes aloud.")
ROUTE_WAIT = 1.5               # seconds a route's result waits for its step's note before going alone


class _Adapter(GeminiLiveLLMAdapter):
    """Every step's tools go in when the session connects; the context's (one step's) tools never trigger a
    reconnect of their own."""

    lp_tools: Any = None
    lp_connecting = False

    def get_llm_invocation_params(self, context, **kwargs):
        params = super().get_llm_invocation_params(context, **kwargs)
        params["tools"] = self.lp_tools if self.lp_connecting else None
        return params


class LamplineGeminiLive(GeminiLiveLLMService):
    adapter_class = _Adapter

    def __init__(self, **kwargs):
        super().__init__(inference_on_context_initialization=False, **kwargs)
        self.lp_busy = False
        self.lp_kind = "auto"
        self.lp_run_pending = False
        self._lp_waiting: list[tuple[str, str]] = []
        self._lp_flow = None
        self._lp_applied = ""          # the step whose instructions the model has
        self._lp_route: tuple[str, str, dict] | None = None
        self._lp_route_timer: asyncio.Task | None = None
        self._lp_started = False

    # ---------------------------------------------------------------- connecting

    async def setup(self, setup):
        # Don't connect yet: the call's first step is known only once the call begins (lp_connect).
        await LLMService.setup(self, setup)

    def lp_step(self) -> tuple[str, str, list[str]]:
        f = self._lp_flow
        if f is None or f.current is None:
            return "", "", []
        tools = [t.name for t in (f.context.tools.standard_tools if isinstance(f.context.tools, ToolsSchema) else [])]
        return f.current.name, f.system_prompt or "", tools

    async def lp_connect(self, flow, context, all_tools: ToolsSchema) -> None:
        """Connect with the current step's instructions and every step's tools."""
        self._lp_flow = flow
        self._context = context
        name, prompt, _ = self.lp_step()
        self._system_instruction_from_init = f"{prompt}\n\n{STEPS}".strip()
        self._lp_applied = name
        adapter = self.get_llm_adapter()
        adapter.lp_tools = adapter.from_standard_tools(all_tools) if all_tools.standard_tools else None
        await self._connect()

    async def _connect(self, session_resumption_handle: str | None = None):
        adapter = self.get_llm_adapter()
        adapter.lp_connecting = True
        try:
            await super()._connect(session_resumption_handle)
        finally:
            adapter.lp_connecting = False

    async def _handle_session_ready(self, session):
        if self._lp_started:                   # a reconnect: Pipecat resumes or re-seeds the conversation
            await super()._handle_session_ready(session)
        else:                                  # the first connect: nothing to seed, the call starts here
            self._session = session
        self._ready_for_realtime_input = True
        self._lp_started = True
        await self._lp_drain()

    # ---------------------------------------------------------------- one response at a time

    async def _lp_send(self, text: str, respond: bool) -> None:
        from google.genai.types import Content, Part

        if not self._session:
            return
        try:
            await self._session.send_client_content(turns=[Content(role="user", parts=[Part(text=text)])],
                                                    turn_complete=respond)
            if respond and self._is_gemini_3:
                await self._session.send_realtime_input(text=" ")
        except Exception as exc:  # noqa: BLE001
            await self._handle_send_error(exc)

    async def _lp_begin(self, kind: str, text: str = "") -> None:
        if self.lp_busy or not self._lp_started or not self._session:
            self._lp_waiting.append((kind, text))
            return
        self.lp_busy, self.lp_kind = True, kind
        if kind == "say":
            await self._lp_send(f"{NOTE} Say exactly this, word for word, and nothing else: {text}", True)
        else:
            await self._lp_send(text or f"{NOTE} Go on, following the current step.", True)

    async def _lp_drain(self) -> None:
        if self._lp_waiting and not self.lp_busy:
            kind, text = self._lp_waiting.pop(0)
            await self._lp_begin(kind, text)

    async def say_exactly(self, text: str) -> None:
        await self._lp_begin("say", text)

    async def _handle_msg_model_turn(self, msg):
        if not self.lp_busy:
            self.lp_busy, self.lp_kind = True, "auto"            # the model answered the caller on its own
        await super()._handle_msg_model_turn(msg)

    async def _handle_msg_output_transcription(self, message):
        if not self.lp_busy:
            self.lp_busy, self.lp_kind = True, "auto"
        await super()._handle_msg_output_transcription(message)

    async def _handle_msg_turn_complete(self, message):
        await super()._handle_msg_turn_complete(message)
        self.lp_busy = False
        await self._lp_drain()

    async def _handle_interruption(self):
        await super()._handle_interruption()
        self.lp_busy = False
        await self._lp_drain()

    # ---------------------------------------------------------------- step changes

    def _note(self) -> str:
        name, prompt, tools = self.lp_step()
        return (f"{NOTE} The call is now in the step \"{name}\". Instructions for this step:\n{prompt}\n"
                f"Tools you may use now: {', '.join(tools) if tools else 'none'}.")

    async def _update_settings(self, delta):
        changed = await LLMService._update_settings(self, delta)    # stored; Gemini can't apply them live
        await self._lp_step_changed()
        return changed

    async def _lp_step_changed(self) -> None:
        name = self.lp_step()[0]
        if not name or name == self._lp_applied or not self._lp_started:
            return
        self._lp_applied = name
        if self._lp_route is not None:                     # the model moved the call: tell it in the route's result
            call_id, tool, result = self._lp_route
            self._lp_route = None
            if self._lp_route_timer:
                self._lp_route_timer.cancel()
            self.lp_busy, self.lp_kind = True, "run"
            await GeminiLiveLLMService._tool_result(self, call_id, tool, {**result, "instructions": self._note()})
            return
        # entered by a rule or a hand-over: a turn of its own; the model answers only if the flow asks it to
        if self.lp_run_pending:
            self.lp_run_pending = False
            await self._lp_begin("run", self._note())
        else:
            await self._lp_send(self._note(), False)

    async def _tool_result(self, tool_call_id: str, tool_name: str, tool_result_message: dict[str, Any]):
        if isinstance(tool_result_message, dict) and "next_step" in tool_result_message and "error" not in tool_result_message:
            # A route: hold its result until the new step's note can go with it.
            self._lp_route = (tool_call_id, tool_name, tool_result_message)
            self._lp_route_timer = asyncio.get_running_loop().create_task(self._lp_route_alone())
            if self.lp_step()[0] != self._lp_applied:
                await self._lp_step_changed()
            return
        await super()._tool_result(tool_call_id, tool_name, tool_result_message)

    async def _lp_route_alone(self) -> None:
        await asyncio.sleep(ROUTE_WAIT)
        if self._lp_route is not None:
            call_id, tool, result = self._lp_route
            self._lp_route = None
            await GeminiLiveLLMService._tool_result(self, call_id, tool, result)

    async def _handle_context(self, context):
        before = len(self._completed_tool_calls)
        await super()._handle_context(context)
        if self.lp_run_pending and len(self._completed_tool_calls) == before and self._lp_route is None:
            self.lp_run_pending = False
            await self._lp_begin("run")
        self.lp_run_pending = False

    async def cleanup(self):
        if self._lp_route_timer:
            self._lp_route_timer.cancel()
        await super().cleanup()


def all_tools(graph, flow) -> ToolsSchema:
    """Every step's tools, once each, for the session's setup."""
    from .flow import tools_for

    seen: dict[str, Any] = {}
    for step in graph.steps.values():
        if getattr(step, "scripted", False):
            continue
        for t in tools_for(step, graph.tools_for(step)).standard_tools:
            seen.setdefault(t.name, t)
    return ToolsSchema(standard_tools=list(seen.values()))


def make_gemini_live(cfg):
    from pipecat.services.google.gemini_live.llm import GeminiVADParams  # noqa: F401 - checks the extra is installed

    settings = LamplineGeminiLive.Settings(voice=cfg.gemini_live_voice or "Charon")
    if cfg.gemini_live_model:
        model = cfg.gemini_live_model
        settings.model = model if model.startswith("models/") else f"models/{model}"
    return LamplineGeminiLive(api_key=cfg.gemini_key or "none", settings=settings)
