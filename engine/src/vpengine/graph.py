"""An agent as the engine sees it: steps, routes between them, and prompts.

Reads the same agent folders `vk` writes (agent.yaml + prompts/*.md).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vk import agentfs, pages
from vk.lint import transition_tool_name

from .tools import Tool, ToolError, parse_all

_VAR = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*(?:\|\s*([^}]*?))?\s*\}\}")


ZONE = None      # the business's time zone (set from Settings); None: this computer's


def clock() -> dict[str, str]:
    """current_date, current_time and current_weekday, for prompts and rules."""
    import datetime as _dt
    now = _dt.datetime.now(ZONE) if ZONE else _dt.datetime.now().astimezone()
    return {"current_date": now.strftime("%Y-%m-%d"), "current_time": now.strftime("%I:%M %p").lstrip("0"),
            "current_weekday": now.strftime("%A")}


def rule_clock() -> dict[str, Any]:
    """The clock as condition transfers see it: 24-hour time so comparisons like current_time >= "17:00" work."""
    import datetime as _dt
    now = _dt.datetime.now(ZONE) if ZONE else _dt.datetime.now().astimezone()
    return {"current_date": now.strftime("%Y-%m-%d"), "current_time": now.strftime("%H:%M"),
            "current_weekday": now.strftime("%A"), "current_hour": now.hour}


def render(text: str, variables: dict[str, Any]) -> str:
    """Fill {{name}} and {{name | fallback}}; unknown names become the fallback or ""."""
    now = clock() if "current_" in (text or "") else {}

    def repl(m: re.Match) -> str:
        name, fallback = m.group(1), m.group(2)
        value = variables.get(name, now.get(name))
        if value is None or value == "":
            return (fallback or "").strip().strip("\"'")
        return str(value)
    return _VAR.sub(repl, text or "")


@dataclass
class Route:
    id: str
    source: str
    target: str
    label: str
    condition: str
    speech: str = ""
    after_greeting: bool = False      # Start hands over right after its greeting, without asking the model
    kind: str = "ask"                 # ask (the model decides) | when (a condition) | otherwise | no_match
    when: str = ""

    @property
    def tool_name(self) -> str:
        return transition_tool_name(self.label)


@dataclass
class Step:
    id: str
    kind: str            # start | agent | end | global | other
    name: str
    prompt: str
    data: dict
    routes: list[Route] = field(default_factory=list)

    @property
    def uses_persona(self) -> bool:
        return bool(self.data.get("add_global_prompt", True))

    @property
    def tool_names(self) -> list[str]:
        listed = self.data.get("tools") or []
        return [str(t) for t in listed] if isinstance(listed, list) else []

    @property
    def greeting(self) -> str:
        return self.data.get("greeting") or ""

    @property
    def scripted(self) -> bool:
        return self.data.get("mode") == "scripted" and self.kind in ("agent", "end")

    @property
    def form(self) -> list[dict]:
        return [f for f in self.data.get("form") or [] if isinstance(f, dict) and f.get("name")]

    @property
    def ask_routes(self) -> list[Route]:
        """Transfers the model decides on (the others follow rules)."""
        return [r for r in self.routes if r.kind == "ask"]

    @property
    def extraction(self) -> tuple[str, list[dict]] | None:
        if not self.data.get("extraction_enabled"):
            return None
        return self.data.get("extraction_prompt") or "", list(self.data.get("extraction_variables") or [])


_KINDS = {"startCall": "start", "agentNode": "agent", "endCall": "end", "globalNode": "global"}


@dataclass
class Graph:
    name: str
    steps: dict[str, Step]
    start: Step
    persona: str
    context: dict[str, Any]
    tools: dict[str, Tool] = field(default_factory=dict)

    @classmethod
    def from_agent(cls, agent: agentfs.Agent) -> "Graph":
        steps: dict[str, Step] = {}
        for n in agent.definition.get("nodes") or []:
            data = n.get("data") or {}
            kind = _KINDS.get(n.get("type"), "other")
            steps[str(n["id"])] = Step(str(n["id"]), kind, data.get("name") or str(n["id"]),
                                       data.get("prompt") or "", data)
        for e in agent.definition.get("edges") or []:
            d = e.get("data") or {}
            src = steps.get(str(e.get("source")))
            if src is not None and str(e.get("target")) in steps:
                src.routes.append(Route(str(e.get("id")), src.id, str(e.get("target")), d.get("label") or "",
                                        d.get("condition") or "", d.get("transition_speech") or "",
                                        bool(d.get("after_greeting")) and src.kind == "start",
                                        pages.route_kind(d), str(d.get("when") or "")))
        starts = [s for s in steps.values() if s.kind == "start"]
        if len(starts) != 1:
            raise ValueError(f"agent needs exactly one start step (found {len(starts)}); run `vk lint`")
        persona = next((s.prompt for s in steps.values() if s.kind == "global"), "")
        try:
            tools = parse_all(agent.tools)
        except ToolError as exc:
            raise ValueError(f"{exc}; run `vk lint`") from exc
        for step in steps.values():
            groups = {t.group for t in tools.values()} | {str(x.get("name")) for x in agent.tools or [] if isinstance(x, dict)}
            missing = [t for t in step.tool_names if t not in groups]
            if missing:
                raise ValueError(f"step {step.name} uses tool {missing[0]}, which this agent does not define")
        return cls(agent.name, steps, starts[0], persona, dict(agent.context_variables or {}), tools)

    @classmethod
    def from_folder(cls, folder: str | Path) -> "Graph":
        return cls.from_agent(agentfs.load(folder))

    @classmethod
    def from_definition(cls, name: str, definition: dict, context: dict | None = None,
                        tools: list | None = None) -> "Graph":
        return cls.from_agent(agentfs.Agent(name=name, definition=definition, context_variables=context or {},
                                            tools=list(tools or [])))

    def tools_for(self, step: Step) -> list[Tool]:
        """The HTTP tools a step may call (a transfer with the same name wins)."""
        transfers = {r.tool_name for r in step.ask_routes}
        listed = set(step.tool_names)
        return [t for t in self.tools.values() if (t.group or t.name) in listed and t.name not in transfers]

    def system_prompt(self, step: Step, variables: dict[str, Any]) -> str:
        parts = []
        if step.uses_persona and self.persona:
            parts.append(render(self.persona, variables))
        parts.append(render(step.prompt, variables))
        if step.kind == "end":
            parts.append("This is the last thing you say on this call. Keep it to one or two short sentences.")
        elif step.ask_routes and not self.tools_for(step):
            parts.append("Everything you write is spoken aloud to the caller. When one of your tools' "
                         "conditions is met, call that tool and write nothing else in that reply: no note "
                         "about what you are doing or why. The next step will answer the caller. "
                         "Never mention tools or steps to the caller.")
        if self.tools_for(step):
            names = ", ".join(t.name for t in self.tools_for(step))
            transfers = ("When a transfer tool's condition is met, call it and write nothing else in that reply; "
                         "the next step will answer the caller. ") if step.ask_routes and step.kind != "end" else ""
            c = clock()
            parts.append(f"Today is {c['current_weekday']}, {c['current_date']}, and the time is {c['current_time']}.")
            parts.append("Everything you write is spoken aloud to the caller. " + transfers +
                         f"These tools look things up or make changes for the caller: {names}. Use them "
                         "whenever you need their answer, and never guess what they would return. Ask the "
                         "caller for anything a tool needs that you don't know yet. Tell the caller the result "
                         "in short, plain spoken words, not raw data. If a tool fails, apologize briefly and "
                         "offer another way to help. Never mention tools or steps to the caller.")
        return "\n\n".join(p for p in parts if p.strip())
