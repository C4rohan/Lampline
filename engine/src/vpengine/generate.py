"""Describe an agent in plain words; the model drafts it, we build the folder.

The model never writes agent.yaml directly. It fills a small, strict outline
(persona, greeting, a root agent, sub-agents with when-to-transfer, details to
collect, tools with example answers), and this module turns that outline into a
normal agent: Start greets and hands over to the root agent, the root agent
transfers to each sub-agent, sub-agents come back or say goodbye. Tools get no
URL, so they answer with their example until you point them at a real service.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from pipecat.processors.aggregators.llm_context import LLMContext
from vk import agentfs
from vk.lint import transition_tool_name

from .flow import parse_json_object

TIMEOUT = 150.0
MAX_TOKENS = 6000

_str = {"type": "string"}
_obj = lambda props: {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}  # noqa: E731
SCHEMA = _obj({
    "name": _str,
    "business_name": _str,
    "persona": _str,
    "greeting": _str,
    "root_instructions": _str,
    "goodbye_instructions": _str,
    "variables": {"type": "array", "items": _obj({"name": _str, "value": _str})},
    "sub_agents": {"type": "array", "items": _obj({
        "name": _str, "label": _str, "when": _str, "instructions": _str,
        "collects": {"type": "array", "items": _obj({"name": _str, "type": {"type": "string", "enum": ["string", "number", "boolean"]}, "description": _str})},
        "tools": {"type": "array", "items": _str},
    })},
    "tools": {"type": "array", "items": _obj({
        "name": _str, "description": _str, "method": {"type": "string", "enum": ["GET", "POST"]},
        "parameters": {"type": "array", "items": _obj({"name": _str, "type": {"type": "string", "enum": ["string", "number", "boolean"]}, "description": _str, "required": {"type": "boolean"}})},
        "example_answer": _str,
    })},
})

INSTRUCTION = """You design phone voice agents. Turn the user's description into the outline the schema asks for.

How these agents work:
- A call starts with a short greeting, then a root agent finds out what the caller needs and transfers them to one sub-agent per job.
- Each sub-agent does one job, then the caller goes back to the root agent or says goodbye.
- A transfer happens when its "when" condition is true. Write "when" from the caller's side, like "The caller wants to book an appointment."
- "label" is two to four lowercase words naming the transfer, like "book appointment". Labels must differ.
- Instructions are spoken on a phone: short sentences, one question at a time, no lists read aloud, no markdown.
- Sub-agent instructions say what to ask, in what order, what to do with the answer, and what not to do.
- "collects" lists details worth saving from that sub-agent, with snake_case names. Leave it empty if nothing needs saving.
- Add a tool only when the agent must look something up or change something in another system (open times, an order's status, a booking). Tool and parameter names are snake_case. "example_answer" is a small JSON object the service might return, with made-up values. List a sub-agent's tools by name in its "tools".
- Use {{business_name}} and other variables in the greeting and instructions instead of hard-coding them, and give each variable a made-up example value. Never invent real people, phone numbers, addresses or account numbers; use obviously fictional examples.
- Never ask callers for card numbers, passwords, or full social security numbers.
- 2 to 6 sub-agents is usual. Keep the root agent's instructions short: understand the need, ask one clarifying question if unclear, transfer.
- The persona sets tone and rules for every step in two to four sentences.
- Reply with the JSON object only."""

_SNAKE = re.compile(r"[^a-z0-9]+")


def snake(text: str, fallback: str) -> str:
    s = _SNAKE.sub("_", (text or "").lower()).strip("_")
    if not s or not s[0].isalpha():
        s = f"{fallback}_{s}".strip("_")
    return s[:60]


class GenerateError(ValueError):
    pass


async def draft(llm, description: str, name: str = "") -> dict:
    """Ask the model for the outline."""
    ask = description.strip()
    if name.strip():
        ask = f"Agent name: {name.strip()}\n\n{ask}"
    ctx = LLMContext(messages=[{"role": "user", "content": ask}])
    try:
        text = await asyncio.wait_for(
            llm.run_inference(ctx, max_tokens=MAX_TOKENS, system_instruction=INSTRUCTION, response_schema=SCHEMA), TIMEOUT)
    except asyncio.TimeoutError as exc:
        raise GenerateError("the model took too long to draft the agent; try a shorter description") from exc
    except Exception as exc:  # noqa: BLE001 - shown to the person
        raise GenerateError(f"the model couldn't draft the agent: {exc}") from exc
    out = parse_json_object(text)
    if not out or not isinstance(out.get("sub_agents"), list):
        raise GenerateError("the model's draft wasn't usable; try again or describe the agent differently")
    return out


def build(outline: dict, name: str = "") -> agentfs.Agent:
    """Turn the outline into an agent: steps, transfers, tools, variables, positions."""
    text = lambda v, n=4000: str(v or "").strip()[:n]  # noqa: E731
    agent_name = text(name or outline.get("name") or "New agent", 80) or "New agent"
    variables = {"business_name": text(outline.get("business_name"), 120) or "Example Business"}
    for v in outline.get("variables") or []:
        key = snake(v.get("name", ""), "var") if isinstance(v, dict) else ""
        if key and key not in variables:
            variables[key] = text(v.get("value"), 400)

    tools: list[dict] = []
    tool_names: set[str] = set()
    for t in (outline.get("tools") or [])[:12]:
        if not isinstance(t, dict):
            continue
        tname = snake(t.get("name", ""), "tool")
        if tname in tool_names:
            continue
        tool_names.add(tname)
        params, seen = [], set()
        for p in t.get("parameters") or []:
            pname = snake(p.get("name", ""), "value") if isinstance(p, dict) else ""
            if pname and pname not in seen:
                seen.add(pname)
                params.append({"name": pname, "type": p.get("type") if p.get("type") in ("string", "number", "boolean") else "string",
                               "description": text(p.get("description"), 300), "required": bool(p.get("required"))})
        try:
            mock = json.loads(t.get("example_answer") or "{}")
        except (TypeError, ValueError):
            mock = {"note": text(t.get("example_answer"), 500)}
        if not isinstance(mock, (dict, list)):
            mock = {"result": mock}
        tools.append({"name": tname, "description": text(t.get("description"), 600) or tname.replace("_", " "),
                      "method": t.get("method") if t.get("method") in ("GET", "POST") else "GET", "url": "",
                      "parameters": params, "mock": mock})

    nodes: list[dict] = [
        {"id": "start", "type": "startCall", "position": {"x": 0, "y": 0},
         "data": {"name": "Start", "greeting": text(outline.get("greeting"), 600) or "Thanks for calling {{business_name}}. How can I help you today?",
                  "prompt": "Greet the caller.", "add_global_prompt": True, "allow_interrupt": True}},
        {"id": "root", "type": "agentNode", "position": {"x": 0, "y": 230},
         "data": {"name": "Root agent", "prompt": text(outline.get("root_instructions")) or
                  "Find out what the caller needs and transfer them to the right agent. Ask one short question if it isn't clear.",
                  "add_global_prompt": True, "allow_interrupt": True}},
    ]
    edges: list[dict] = [{"id": "start-root", "source": "start", "target": "root",
                          "data": {"label": "after greeting", "condition": "Right after the greeting.", "after_greeting": True}}]
    subs = [s for s in (outline.get("sub_agents") or []) if isinstance(s, dict)][:8]
    used_ids, used_labels, used_names = {"start", "root", "bye", "persona"}, {"nothing_else"}, {"start", "root agent", "goodbye"}
    gap = 320
    x0 = -((len(subs) - 1) * gap) / 2
    for i, s in enumerate(subs):
        sname = text(s.get("name"), 60) or f"Sub-agent {i + 1}"
        while sname.lower() in used_names:
            sname = f"{sname} {i + 1}"
        used_names.add(sname.lower())
        sid = snake(sname, "agent").replace("_", "-")[:40] or f"agent-{i + 1}"
        while sid in used_ids:
            sid = f"{sid}-{i + 1}"
        used_ids.add(sid)
        data: dict[str, Any] = {"name": sname, "prompt": text(s.get("instructions")) or f"Help the caller with {sname.lower()}.",
                                "add_global_prompt": True, "allow_interrupt": True}
        collects = []
        for c in s.get("collects") or []:
            cname = snake(c.get("name", ""), "detail") if isinstance(c, dict) else ""
            if cname and cname not in {x["name"] for x in collects}:
                collects.append({"name": cname, "type": c.get("type") if c.get("type") in ("string", "number", "boolean") else "string",
                                 "prompt": text(c.get("description"), 300)})
        if collects:
            data.update(extraction_enabled=True, extraction_variables=collects,
                        extraction_prompt="Use the caller's own words. Leave out anything they didn't say.")
        mine = [snake(t, "tool") for t in s.get("tools") or [] if isinstance(t, str)]
        mine = [t for t in dict.fromkeys(mine) if t in tool_names]
        if mine:
            data["tools"] = mine
        nodes.append({"id": sid, "type": "agentNode", "position": {"x": round(x0 + i * gap), "y": 480}, "data": data})
        label = text(s.get("label"), 50).lower() or sname.lower()
        while transition_tool_name(label) in used_labels or transition_tool_name(label) in tool_names:
            label = f"{label} {i + 1}"
        used_labels.add(transition_tool_name(label))
        edges.append({"id": f"root-{sid}", "source": "root", "target": sid,
                      "data": {"label": label, "condition": text(s.get("when"), 400) or f"The caller needs help with {sname.lower()}."}})
        edges.append({"id": f"{sid}-root", "source": sid, "target": "root",
                      "data": {"label": "something else", "condition": "The caller needs help with something else."}})
        edges.append({"id": f"{sid}-bye", "source": sid, "target": "bye",
                      "data": {"label": "finished", "condition": "The caller has what they need and is ready to hang up."}})
    nodes.append({"id": "bye", "type": "endCall", "position": {"x": 0, "y": 760},
                  "data": {"name": "Goodbye", "prompt": text(outline.get("goodbye_instructions")) or
                           "Thank the caller and say goodbye in one short sentence.", "add_global_prompt": True}})
    nodes.append({"id": "persona", "type": "globalNode", "position": {"x": round(-x0 + gap + 60) if subs else 420, "y": 0},
                  "data": {"name": "Global instructions", "prompt": text(outline.get("persona")) or
                           "You are a friendly phone assistant for {{business_name}}. Keep answers short and natural; this is a phone call."}})
    edges.append({"id": "root-bye", "source": "root", "target": "bye",
                  "data": {"label": "nothing else", "condition": "The caller says they need nothing else."}})
    return agentfs.Agent(name=agent_name, definition={"nodes": nodes, "edges": edges}, context_variables=variables,
                         setup={"purpose": "drafted from a description", "real_data": False}, lint={"ignore": []}, tools=tools)
