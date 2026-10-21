"""Moves a call through an agent's steps.

Each step becomes a system prompt plus one tool per outgoing route (the route's
condition is the tool's description), plus the HTTP tools the step lists (see
tools.py; their answers go back to the model). When the model calls a route's tool we
switch steps in place: run the leaving step's extraction, say the route's
transition line, swap the system prompt and tools, and let the model answer
from the new step. Reaching an end step lets the model say one last thing,
then hangs up.

The controller is transport-agnostic: a session gives it `say`, `queue`,
`end` and `emit`, so text chat, browser calls and phone calls share it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from vk import pages

from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import FunctionCallResultProperties, LLMRunFrame, LLMUpdateSettingsFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.services.llm_service import FunctionCallParams, LLMService
from pipecat.services.settings import LLMSettings

from .graph import Graph, Route, Step, render, rule_clock
from .tools import Tool, ToolRunner

log = logging.getLogger("vpengine.flow")

EXTRACTION_TIMEOUT = 20.0


class Host(Protocol):
    """What a session provides to the controller."""

    async def say(self, text: str) -> None: ...
    async def announce(self, text: str) -> None: ...   # spoken, but not added to the model's context
    async def queue(self, frames: list) -> None: ...
    async def end(self, reason: str) -> None: ...
    async def transfer(self, transfer) -> None: ...      # hand the caller to a person before the call ends
    def emit(self, event: dict) -> None: ...


def tools_for(step: Step, http_tools: list[Tool] | None = None) -> ToolsSchema:
    return ToolsSchema(standard_tools=[
        FunctionSchema(name=r.tool_name, description=r.condition or r.label, properties={}, required=[])
        for r in step.ask_routes
    ] + [t.schema() for t in http_tools or []])


@dataclass
class PageState:
    """Where a scripted page is: which fields failed how often, what's being confirmed."""
    step_id: str
    attempts: dict[str, int] = field(default_factory=dict)
    skipped: set[str] = field(default_factory=set)
    confirming: str | None = None


DEFAULT_REPROMPT = "Sorry, I didn't catch that."
INTERPRET_TIMEOUT = 15.0


_JSON_TYPES = {"string": "string", "number": "number", "integer": "integer", "boolean": "boolean"}


def extraction_schema(variables: list[dict]) -> dict:
    props = {}
    for v in variables:
        t = _JSON_TYPES.get(str(v.get("type") or "string").lower(), "string")
        props[v["name"]] = {"type": [t, "null"], "description": v.get("prompt") or v["name"]}
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def parse_json_object(text: str | None) -> dict | None:
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        value = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


class FlowController:
    def __init__(self, graph: Graph, context: LLMContext, llm: LLMService, host: Host,
                 variables: dict[str, Any] | None = None, tools: ToolRunner | None = None):
        self.graph = graph
        self.context = context
        self.llm = llm
        self.host = host
        self.variables: dict[str, Any] = {**graph.context, **(variables or {})}
        self.current: Step | None = None
        self.system_prompt = ""
        self.visited: list[str] = []
        self.ended = False
        # Busy tracking, so a text turn knows when the agent has finished replying.
        self.transitioning = False
        self.awaiting_response = False
        self.responding = False
        self._ending = False
        self._final_started = False
        self.runner = tools or ToolRunner(mock=True)
        self.extract_llm = None                     # a text model for extraction when self.llm only talks (speech to speech)
        self.step_tools: list[Tool] = []
        self.running_tools = 0
        self.page: PageState | None = None          # the scripted page being filled in
        self.scripting = False                      # handling a caller turn on a scripted page
        self._turn_lock = asyncio.Lock()
        self._keys = ""                             # keypad digits typed so far
        self._keys_timer: asyncio.Task | None = None
        self._hops = 0                              # rule transfers since the caller last spoke
        # Not cancelled when the caller talks over the agent: a booking that was sent must
        # finish and be reported. Tools time out on their own (1-30 s).
        llm.register_function(None, self._on_tool_call, cancel_on_interruption=False, timeout_secs=45)

    # ---------------------------------------------------------------- state

    @property
    def busy(self) -> bool:
        return (self.transitioning or self.awaiting_response or self.responding or self.running_tools > 0
                or self.scripting)

    async def _set_step(self, step: Step) -> None:
        previous = self.current
        self.current = step
        self.visited.append(step.id)
        self.system_prompt = self.graph.system_prompt(step, self.variables)
        # The step's prompt goes in as the service's system instruction (it reaches the
        # LLM ahead of the next inference); tools live on the shared context.
        await self.host.queue([LLMUpdateSettingsFrame(delta=LLMSettings(system_instruction=self.system_prompt))])
        self.step_tools = [] if step.scripted else self.graph.tools_for(step)
        self.page = PageState(step.id) if step.scripted else None
        self.context.set_tools(tools_for(step, self.step_tools) if (step.ask_routes or self.step_tools) and not step.scripted
                               else ToolsSchema(standard_tools=[]))
        if step.kind == "end":
            self._ending = True
        self.host.emit({"type": "node_transition", "node_id": step.id, "node_name": step.name,
                        "from_node_id": previous.id if previous else None,
                        "from_node_name": previous.name if previous else None})

    async def _run_llm(self) -> None:
        self.awaiting_response = True
        await self.host.queue([LLMRunFrame()])

    # ---------------------------------------------------------------- start

    async def start(self) -> None:
        start = self.graph.start
        await self._set_step(start)
        greeting = render(start.greeting, self.variables).strip()
        handoff = next((r for r in start.routes if r.after_greeting), None)
        if greeting:
            await self.host.say(greeting)
        if handoff is not None:
            # Start only greets and hands over: the next step (usually the root agent) takes
            # the caller's first words directly, without a model round trip to decide.
            self.host.emit({"type": "tool_call_result", "function_name": handoff.tool_name, "tool_call_id": "start",
                            "result": {"status": "ok", "next_step": self.graph.steps[handoff.target].name}})
            await self._set_step(self.graph.steps[handoff.target])
            if not greeting or self.current.scripted:
                await self._enter()
        elif not greeting:
            await self._run_llm()

    # ---------------------------------------------------------------- tools

    def _route(self, tool_name: str) -> Route | None:
        if self.current is None:
            return None
        return next((r for r in self.current.routes if r.tool_name == tool_name), None)

    async def _on_tool_call(self, params: FunctionCallParams) -> None:
        name = params.function_name
        self.host.emit({"type": "tool_call_started", "function_name": name, "tool_call_id": params.tool_call_id,
                        "arguments": dict(params.arguments or {})})
        route = self._route(name)
        tool = next((t for t in self.step_tools if t.name == name), None) if route is None else None
        if tool is not None and not self.transitioning and not self.ended:
            await self._run_tool(tool, params)
            return
        if route is None or self.transitioning or self.ended:
            reason = "already moving to another step" if self.transitioning else f"no tool named {name} here"
            self.host.emit({"type": "tool_call_result", "function_name": name, "tool_call_id": params.tool_call_id,
                            "result": {"error": reason}})
            # Let the model carry on (unknown tool) or stay quiet (duplicate transition).
            if not self.transitioning:
                self.awaiting_response = True
            await params.result_callback({"error": reason},
                                         properties=FunctionCallResultProperties(run_llm=not self.transitioning))
            return
        self.transitioning = True

        async def go() -> None:
            try:
                await self._transition(route, params.tool_call_id)
            except Exception as exc:  # noqa: BLE001 - reported to the client, call continues
                log.exception("transition failed")
                self.host.emit({"type": "execution_error", "message": f"transition failed: {exc}"})
            finally:
                self.transitioning = False

        await params.result_callback({"status": "ok", "next_step": self.graph.steps[route.target].name},
                                     properties=FunctionCallResultProperties(run_llm=False, on_context_updated=go))

    async def _run_tool(self, tool: Tool, params: FunctionCallParams) -> None:
        self.running_tools += 1
        try:
            speech = render(tool.speech, self.variables).strip()
            if speech:
                await self.host.announce(speech)
            result = await self.runner.run(tool, dict(params.arguments or {}), self.variables)
            if result.saved:
                self.variables.update(result.saved)
                self.host.emit({"type": "variables", "node_name": self.current.name if self.current else None,
                                "tool": tool.name, "values": result.saved, "seconds": result.seconds})
            self.host.emit({"type": "tool_call_result", "function_name": tool.name,
                            "tool_call_id": params.tool_call_id, "result": result.for_event()})
            if not result.ok:
                self.host.emit({"type": "execution_error", "message": f"tool {tool.name}: {result.error}"})
            self.awaiting_response = True
            await params.result_callback(result.for_model(), properties=FunctionCallResultProperties(run_llm=True))
        except Exception as exc:  # noqa: BLE001 - a broken tool must not end the call
            log.exception("tool %s failed", tool.name)
            self.host.emit({"type": "execution_error", "message": f"tool {tool.name} failed: {exc}"})
            self.awaiting_response = True
            await params.result_callback({"ok": False, "error": "the tool failed"},
                                         properties=FunctionCallResultProperties(run_llm=True))
        finally:
            self.running_tools -= 1

    async def _transition(self, route: Route, tool_call_id: str) -> None:
        leaving = self.current
        target = self.graph.steps[route.target]
        if leaving is not None and leaving.extraction:
            await self._extract(leaving)
        self.host.emit({"type": "tool_call_result", "function_name": route.tool_name, "tool_call_id": tool_call_id,
                        "result": {"status": "ok", "next_step": target.name}})
        speech = render(route.speech, self.variables).strip()
        if speech:
            await self.host.say(speech)
        await self._set_step(target)
        await self._enter()

    async def _enter(self) -> None:
        """A step has just begun: a scripted page speaks its lines; an LLM step lets the model answer."""
        if self.current is not None and self.current.scripted:
            await self._script_enter()
        else:
            await self._run_llm()

    # ----------------------------------------------------------- scripted pages

    def _today(self) -> dt.date:
        from .graph import ZONE
        return (dt.datetime.now(ZONE) if ZONE else dt.datetime.now()).date()

    def _next_field(self) -> dict | None:
        st = self.page
        for f in self.current.form if self.current else []:
            if f["name"] in (st.skipped if st else set()):
                continue
            if self.variables.get(f["name"]) in (None, ""):
                return f
        return None

    async def _say_lines(self, lines: list[str]) -> None:
        for line in lines:
            line = render(line, self.variables).strip()
            if line:
                await self.host.say(line)

    async def _script_enter(self) -> None:
        step = self.current
        await self._page_tools("on_entry")
        if self.current is not step or self.ended:
            return
        lines = [str(step.data.get("say") or "")]
        if step.kind == "end":
            await self._say_lines(lines)
            await self.finish("end step reached")
            return
        nxt = self._next_field()
        if nxt is None:
            await self._say_lines(lines)
            await self._page_tools("on_done")
            await self._follow_rules()
            return
        await self._say_lines(lines + [str(nxt.get("ask") or "")])

    async def _page_tools(self, when: str) -> None:
        """A scripted page's tools, run in order on entry or once its form is complete (Dialogflow's page webhooks).
        Arguments come from `with` and from variables of the same name; nothing is asked of the model."""
        step = self.current
        for name in step.data.get(when) or []:
            tool = next((t for t in self.graph.tools.values() if t.name == name), None) or \
                next((t for t in self.graph.tools.values() if t.group == name), None)
            if tool is None:
                self.host.emit({"type": "execution_error", "message": f"{step.name}: there's no tool named {name}"})
                continue
            args = {p.name: self.variables[p.name] for p in tool.params if self.variables.get(p.name) not in (None, "")}
            call_id = f"page-{step.id}-{when}-{tool.name}"
            self.host.emit({"type": "tool_call_started", "function_name": tool.name, "tool_call_id": call_id,
                            "arguments": args, "page": when})
            speech = render(tool.speech, self.variables).strip()
            if speech:
                await self.host.say(speech)
            try:
                result = await self.runner.run(tool, args, self.variables)
            except Exception as exc:  # noqa: BLE001 - a broken tool must not end the call
                log.exception("page tool %s failed", tool.name)
                self.host.emit({"type": "execution_error", "message": f"tool {tool.name} failed: {exc}"})
                continue
            if result.saved:
                self.variables.update(result.saved)
                self.host.emit({"type": "variables", "node_name": step.name, "tool": tool.name,
                                "values": result.saved, "seconds": result.seconds})
            self.host.emit({"type": "tool_call_result", "function_name": tool.name, "tool_call_id": call_id,
                            "result": result.for_event()})
            if not result.ok:
                self.host.emit({"type": "execution_error", "message": f"tool {tool.name}: {result.error}"})

    async def _follow_rules(self) -> bool:
        """Condition transfers in order, then "otherwise". True if the call moved on."""
        step = self.current
        if step is None:
            return False
        for r in step.routes:
            if r.kind == "when" and pages.evaluate(r.when, {**rule_clock(), **self.variables}):
                await self._rule_transition(r, f"when {r.when}")
                return True
        for r in step.routes:
            if r.kind == "otherwise" and step.scripted:
                await self._rule_transition(r, "otherwise")
                return True
        return False

    async def _rule_transition(self, route: Route, why: str) -> None:
        self._hops += 1
        if self._hops > 12:
            self.host.emit({"type": "execution_error", "message": "condition transfers went round in a loop; stopped"})
            return
        self.host.emit({"type": "tool_call_started", "function_name": route.tool_name, "tool_call_id": f"rule-{route.id}",
                        "arguments": {}, "rule": why})
        self.transitioning = True
        try:
            await self._transition(route, f"rule-{route.id}")
        finally:
            self.transitioning = False

    async def caller_turn(self, text: str, *, keypad: bool = False) -> None:
        """What the caller said (or typed on the keypad) while a scripted page is on."""
        if self.ended or self.current is None or not self.current.scripted:
            return
        async with self._turn_lock:
            self.scripting = True
            self._hops = 0
            try:
                await self._script_turn(text, keypad)
            except Exception as exc:  # noqa: BLE001 - reported; the call goes on
                log.exception("scripted turn failed")
                self.host.emit({"type": "execution_error", "message": f"scripted page {self.current.name}: {exc}"})
            finally:
                self.scripting = False
                self.awaiting_response = False

    async def _script_turn(self, text: str, keypad: bool) -> None:
        step, st = self.current, self.page
        assisted = step.data.get("interpret", "assisted") != "exact"
        if st.confirming:
            ok, yes, _ = pages.parse_answer({"type": "yes_no"}, text, keypad=keypad)
            if ok:
                name, st.confirming = st.confirming, None
                if not yes:
                    self.variables.pop(name, None)
                    f = next(x for x in step.form if x["name"] == name)
                    await self._say_lines([str(f.get("ask") or "")])
                    return
                await self._after_answer()
                return
        field_ = self._next_field()
        ok, value, why = pages.parse_answer(field_, text, today=self._today(), keypad=keypad) if field_ else (False, None, "")
        route = None
        wants_help = assisted and not keypad and (step.ask_routes or (field_ is not None and (not ok or field_.get("type") == "text")))
        if wants_help:
            route, said = await self._interpret(step, field_, text)
            if route is None and field_ is not None and said not in (None, "") and (not ok or field_.get("type") == "text"):
                ok, value, why = pages.parse_answer(field_, str(said), today=self._today())
        if route is not None:
            await self._rule_transition(route, "the caller's request")
            return
        if field_ is None:                            # nothing to ask, and no transfer fits
            if not await self._follow_rules():
                await self._say_lines([str(step.data.get("no_match_say") or DEFAULT_REPROMPT)])
            return
        name = field_["name"]
        if ok:
            self.variables[name] = value
            self.host.emit({"type": "variables", "node_name": step.name, "values": {name: value}, "source": "form"})
            if field_.get("confirm"):
                st.confirming = name
                await self._say_lines([str(field_["confirm"])])
                return
            await self._after_answer()
            return
        st.attempts[name] = st.attempts.get(name, 0) + 1
        self.host.emit({"type": "variables", "node_name": step.name, "values": {}, "invalid": {name: why}, "source": "form"})
        limit = int(step.data.get("max_attempts") or 3)
        if st.attempts[name] >= limit:
            no_match = next((r for r in step.routes if r.kind == "no_match"), None)
            if no_match is not None:
                await self._rule_transition(no_match, f"no match for {name}")
                return
            if field_.get("required") is False:
                st.skipped.add(name)
                await self._after_answer()
                return
        await self._say_lines([str(field_.get("reprompt") or f"{DEFAULT_REPROMPT} {field_.get('ask') or ''}")])

    async def _after_answer(self) -> None:
        nxt = self._next_field()
        if nxt is not None:
            await self._say_lines([str(nxt.get("ask") or "")])
            return
        await self._page_tools("on_done")
        await self._say_lines([str(self.current.data.get("done_say") or "")])
        if not await self._follow_rules():
            self.host.emit({"type": "execution_error",
                            "message": f"{self.current.name}: the form is complete but no transfer's condition is true"})

    async def _interpret(self, step: Step, field_: dict | None, text: str) -> tuple[Route | None, Any]:
        """Ask the model, once, whether the caller wants a different transfer, and what their answer was."""
        options = {r.tool_name: r for r in step.ask_routes}
        lines = ["You read one turn of a phone call for a scripted step. Reply with JSON only."]
        if options:
            lines.append("If the caller is asking for one of these instead of answering, set route to its name; "
                         "otherwise route is none:")
            lines += [f"- {n}: {r.condition or r.label}" for n, r in options.items()]
        if field_ is not None:
            kind = field_.get("type") or "text"
            hint = f" One of: {', '.join(str(c.get('value') if isinstance(c, dict) else c) for c in field_.get('choices') or [])}." \
                if kind == "choice" else ""
            lines.append(f"The agent asked: “{render(str(field_.get('ask') or ''), self.variables)}” (expects {kind.replace('_', ' or ')}).{hint} "
                         "Set value to the caller's answer in plain form (for a name, just the name), or null if they "
                         "didn't answer it.")
        else:
            lines.append("value is always null.")
        schema = {"type": "object", "additionalProperties": False, "required": ["route", "value"], "properties": {
            "route": {"type": "string", "enum": list(options) + ["none"]}, "value": {"type": ["string", "null"]}}}
        try:
            out = parse_json_object(await asyncio.wait_for((self.extract_llm or self.llm).run_inference(
                LLMContext(messages=[{"role": "user", "content": text}]), max_tokens=200,
                system_instruction="\n".join(lines), response_schema=schema), INTERPRET_TIMEOUT)) or {}
        except Exception as exc:  # noqa: BLE001 - fall back to the built-in parsers
            self.host.emit({"type": "execution_error", "message": f"reading the answer failed: {exc}"})
            return None, None
        return options.get(str(out.get("route"))), out.get("value")

    # -- keypad (DTMF) ------------------------------------------------------

    async def keypad(self, key: str) -> None:
        """A key pressed on the phone. # submits, * clears; a pause submits too."""
        field_ = self._next_field() if self.current is not None and self.current.scripted else None
        if field_ is None and not (self.page and self.page.confirming):
            return
        if key == "*":
            self._keys = ""
            return
        if key != "#":
            self._keys += key
        if self._keys_timer:
            self._keys_timer.cancel()
        want = (field_ or {}).get("max_length") if (field_ or {}).get("type") in ("digits", "phone") else 1 \
            if (field_ or {}).get("type") in ("yes_no", "choice") or (self.page and self.page.confirming) else None
        if key == "#" or (want and len(self._keys) >= int(want)):
            digits, self._keys = self._keys, ""
            if digits:
                self.host.emit({"type": "user_speech", "text": digits, "keypad": True})
                await self.caller_turn(digits, keypad=True)
            return

        async def later() -> None:
            await asyncio.sleep(3.0)
            digits, self._keys = self._keys, ""
            if digits:
                self.host.emit({"type": "user_speech", "text": digits, "keypad": True})
                await self.caller_turn(digits, keypad=True)
        self._keys_timer = asyncio.create_task(later())

    async def check_rules(self) -> None:
        """LLM steps: after a reply, follow a condition transfer whose condition became true."""
        step = self.current
        if self.ended or self.transitioning or step is None or step.scripted or step.kind != "agent":
            return
        if any(r.kind == "when" for r in step.routes):
            self._hops = 0
            await self._follow_rules()

    # ----------------------------------------------------------- extraction

    def transcript(self) -> str:
        lines = []
        for m in self.context.get_messages():
            if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
                continue
            content = m.get("content")
            if isinstance(content, list):
                content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
            if content:
                lines.append(f"{'Caller' if m['role'] == 'user' else 'Agent'}: {content}")
        return "\n".join(lines)

    async def _extract(self, step: Step) -> None:
        prompt, variables = step.extraction  # type: ignore[misc]
        if not variables:
            return
        names = [v["name"] for v in variables]
        instruction = "\n\n".join(p for p in [
            render(prompt, self.variables),
            "Read the call transcript and fill in these fields. Use null for anything the caller did not say.",
            "\n".join(f"- {v['name']} ({v.get('type') or 'string'}): {v.get('prompt') or ''}" for v in variables),
            "Reply with one JSON object and nothing else.",
        ] if p.strip())
        ctx = LLMContext(messages=[{"role": "user", "content": self.transcript() or "(no conversation yet)"}])
        started = time.monotonic()
        try:
            text = await asyncio.wait_for(
                (self.extract_llm or self.llm).run_inference(ctx, max_tokens=400, system_instruction=instruction,
                                       response_schema=extraction_schema(variables)),
                EXTRACTION_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - extraction never blocks the call
            self.host.emit({"type": "execution_error", "message": f"extraction in {step.name} failed: {exc}"})
            return
        found = parse_json_object(text)
        if found is None:
            self.host.emit({"type": "execution_error", "message": f"extraction in {step.name} returned no JSON"})
            return
        values = {k: found.get(k) for k in names if found.get(k) not in (None, "")}
        self.variables.update(values)
        self.host.emit({"type": "variables", "node_name": step.name, "values": values,
                        "seconds": round(time.monotonic() - started, 2)})

    # -------------------------------------------------- response bookkeeping

    def on_response_started(self) -> None:
        self.awaiting_response = False
        self.responding = True
        if self._ending:
            self._final_started = True

    async def on_response_ended(self) -> None:
        self.responding = False
        if self._final_started and not self.ended:
            await self.finish("end step reached")
            return
        if not self.awaiting_response and not self.running_tools:
            await self.check_rules()

    def collected(self) -> list[str]:
        """What this call gathered: everything but the agent's own unchanged constants."""
        constants = self.graph.context
        return [k for k, v in self.variables.items() if k not in constants or constants[k] != v]

    async def finish(self, reason: str) -> None:
        if self.ended:
            return
        self.ended = True
        transfer = None
        if reason == "end step reached" and self.current is not None:
            from .transfer import build
            try:
                transfer = build(self.current, self.variables, getattr(self.host, "id", ""), render, self.collected())
            except ValueError as exc:
                self.host.emit({"type": "execution_error", "message": f"{exc}. Hanging up instead."})
        if transfer is not None and not getattr(self.host, "can_transfer", True):
            # A Telegram, WhatsApp or email conversation has no call to hand over.
            self.host.emit({"type": "notice", "message": f"{self.current.name} transfers calls to {transfer.to}; "
                                                         "a message conversation just ends here."})
            transfer = None
        if transfer is not None:
            self.host.emit({"type": "transfer", "node_name": self.current.name, **transfer.event()})
            try:
                await self.host.transfer(transfer)
                reason = f"transferred to {transfer.to}"
            except Exception as exc:  # noqa: BLE001 - a failed hand-off still ends the call cleanly
                self.host.emit({"type": "execution_error", "message": f"transfer to {transfer.to} failed: {exc}"})
                transfer = None
        self.host.emit({"type": "call_ended", "reason": reason,
                        "node_name": self.current.name if self.current else None,
                        "variables": dict(self.variables),
                        **({"transfer": transfer.event()} if transfer is not None else {})})
        await self.host.end(reason)
