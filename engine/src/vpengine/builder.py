"""Reading and writing agent folders for the visual builder.

Agents stay in the same folder format `vk` uses (agent.yaml + prompts/*.md),
so a change made in the builder shows up in git as a readable diff. Before every save the previous version is
kept in ~/.cache/vpengine/history/<agent>/ (last 30).
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import time
from importlib import resources
from pathlib import Path
from typing import Any

from vk import agentfs, pages
from vk.lint import lint

from .builtin_speech import CACHE

SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
NODE_TYPES = {"startCall", "agentNode", "endCall", "globalNode", "trigger", "webhook", "qa"}   # the last three: nodes kept off the canvas
KEEP_HISTORY = 30


class BuilderError(ValueError):
    pass


def findings(agent: agentfs.Agent, folder: Path | None) -> list[dict]:
    return [f.as_dict() for f in lint(agent, folder)]


def read(folder: Path) -> dict:
    agent = agentfs.load(folder)
    return {"slug": folder.name, "name": agent.name, "definition": agent.definition,
            "context_variables": agent.context_variables or {}, "tools": agent.tools or [],
            "triggers": agent.triggers or [],
            "findings": findings(agent, folder)}


# Step and transfer ids: what the builder, templates and importers make. Never a path.
SAFE_ID = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,79}")


def _clean_definition(definition: Any) -> dict:
    """Accept what the builder sends, but only well-formed nodes and edges."""
    if not isinstance(definition, dict):
        raise BuilderError("definition must be an object")
    nodes, edges = definition.get("nodes"), definition.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise BuilderError("definition needs nodes and edges lists")
    ids = set()
    for n in nodes:
        if not isinstance(n, dict) or n.get("type") not in NODE_TYPES or not isinstance(n.get("data"), dict):
            raise BuilderError("every step needs a known type and data")
        nid = str(n.get("id") or "")
        if not nid or nid in ids:
            raise BuilderError("every step needs a unique id")
        if not SAFE_ID.fullmatch(nid):
            raise BuilderError(f"step id {nid[:40]!r} may only use letters, digits, - _ . and :")
        ids.add(nid)
        n["id"] = nid
        pos = n.get("position") or {}
        n["position"] = {"x": round(float(pos.get("x", 0))), "y": round(float(pos.get("y", 0)))}
        data = n["data"]
        for fld in agentfs.TEXT_FIELDS:                # text only: never a {file: ...} reference from a client
            if data.get(fld) is not None and not isinstance(data[fld], str):
                raise BuilderError(f"{fld} must be text")
        if data.get("mode") == "scripted":
            # exports run every step as instructions for a model: give scripted pages a faithful one.
            data["prompt"] = pages.describe(data)
    eids = set()
    for e in edges:
        if not isinstance(e, dict) or not isinstance(e.get("data"), dict):
            raise BuilderError("every transfer needs data")
        eid = str(e.get("id") or "")
        if not eid or eid in eids:
            raise BuilderError("every transfer needs a unique id")
        if not SAFE_ID.fullmatch(eid):
            raise BuilderError(f"transfer id {eid[:40]!r} may only use letters, digits, - _ . and :")
        eids.add(eid)
        e["id"], e["source"], e["target"] = eid, str(e.get("source")), str(e.get("target"))
        d = e["data"]
        kind = pages.route_kind(d)
        if kind != "ask" and not str(d.get("condition") or "").strip():     # every transfer carries a condition in words (exports need it)
            d["condition"] = {"when": f"When {d.get('when')}.", "otherwise": "Otherwise.",
                              "no_match": "When the caller's answers can't be understood."}[kind]
        if e["source"] not in ids or e["target"] not in ids:
            raise BuilderError(f"transfer {eid} points at a missing step")
    return definition


def _snapshot(folder: Path, slug: str) -> None:
    if not (folder / agentfs.AGENT_FILE).is_file():
        return
    try:
        files = agentfs.render_files(agentfs.load(folder))
    except agentfs.FolderError:
        return
    hist = CACHE / "history" / slug
    hist.mkdir(parents=True, exist_ok=True)
    (hist / f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.json").write_text(
        json.dumps(files, ensure_ascii=False))
    for old in sorted(hist.glob("*.json"))[:-KEEP_HISTORY]:
        old.unlink()


def _agent_from(folder: Path, body: dict) -> agentfs.Agent:
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 80:
        raise BuilderError("the agent needs a name (up to 80 characters)")
    definition = _clean_definition(copy.deepcopy(body.get("definition")))
    context = body.get("context_variables") or {}
    if not isinstance(context, dict) or not all(isinstance(k, str) and re.fullmatch(r"[A-Za-z_]\w{0,63}", k)
                                                for k in context):
        raise BuilderError("variable names must be letters, digits and underscores")
    context = {k: "" if v is None else str(v) for k, v in context.items()}
    existing = agentfs.load(folder) if (folder / agentfs.AGENT_FILE).is_file() else None
    tools = body.get("tools")
    if tools is None:                     # a client that doesn't know about tools keeps them
        tools = existing.tools if existing else []
    if not isinstance(tools, list) or not all(isinstance(t, dict) for t in tools) or len(tools) > 50:
        raise BuilderError("tools must be a list of tool definitions (up to 50)")
    triggers = body.get("triggers")
    if triggers is None:                  # likewise for triggers
        triggers = existing.triggers if existing else []
    if not isinstance(triggers, list) or not all(isinstance(t, dict) for t in triggers) or len(triggers) > 30:
        raise BuilderError("triggers must be a list of trigger definitions (up to 30)")
    return agentfs.Agent(name=name, definition=definition, context_variables=context,
                         setup=existing.setup if existing else {}, lint=existing.lint if existing else {},
                         tools=copy.deepcopy(tools), triggers=copy.deepcopy(triggers))


def check(folder: Path, body: dict) -> list[dict]:
    """Lint an unsaved draft, so the builder can mark problems while you edit."""
    return findings(_agent_from(folder, body), folder)


def save(folder: Path, body: dict) -> dict:
    agent = _agent_from(folder, body)
    _snapshot(folder, folder.name)
    agentfs.write(agent, folder)
    return read(folder)


def blank(name: str) -> agentfs.Agent:
    """Start greets and hands over to the root agent, which transfers to a sub-agent or says goodbye."""
    definition = {
        "nodes": [
            {"id": "start", "type": "startCall", "position": {"x": 360, "y": 0},
             "data": {"name": "Start", "greeting": "Thanks for calling {{business_name}}. How can I help you today?",
                      "prompt": "Greet the caller.", "add_global_prompt": True, "allow_interrupt": True}},
            {"id": "root", "type": "agentNode", "position": {"x": 360, "y": 220},
             "data": {"name": "Root agent",
                      "prompt": "Find out what the caller needs and transfer them to the right agent. "
                                "Ask one short question if it isn't clear.",
                      "add_global_prompt": True, "allow_interrupt": True}},
            {"id": "help", "type": "agentNode", "position": {"x": 120, "y": 480},
             "data": {"name": "General questions", "prompt": "Answer the caller's general question briefly.",
                      "add_global_prompt": True, "allow_interrupt": True}},
            {"id": "bye", "type": "endCall", "position": {"x": 600, "y": 480},
             "data": {"name": "Goodbye", "prompt": "Thank the caller and say goodbye in one short sentence.",
                      "add_global_prompt": True}},
            {"id": "persona", "type": "globalNode", "position": {"x": 820, "y": 0},
             "data": {"name": "Global instructions",
                      "prompt": "You are a friendly phone assistant for {{business_name}}. Keep answers short and "
                                "natural; this is a phone call."}},
        ],
        "edges": [
            {"id": "start-root", "source": "start", "target": "root",
             "data": {"label": "after greeting", "condition": "Right after the greeting.", "after_greeting": True}},
            {"id": "root-bye", "source": "root", "target": "bye",
             "data": {"label": "nothing else", "condition": "The caller says they need nothing else."}},
            {"id": "root-help", "source": "root", "target": "help",
             "data": {"label": "general question", "condition": "The caller has a general question."}},
            {"id": "help-root", "source": "help", "target": "root",
             "data": {"label": "something else", "condition": "The caller needs something else."}},
            {"id": "help-bye", "source": "help", "target": "bye",
             "data": {"label": "finished", "condition": "The caller has what they need and is done."}},
        ],
    }
    return agentfs.Agent(name=name, definition=definition, context_variables={"business_name": "Example Clinic"},
                         setup={"real_data": False})


def templates() -> list[str]:
    root = resources.files("vk").joinpath("templates")
    return sorted(p.name for p in root.iterdir() if p.joinpath("agent.yaml").is_file())


def unique_slug(agents_dir: Path, name: str) -> str:
    base = agentfs.slugify(name, fallback="agent")[:50]
    slug, n = base, 2
    while (agents_dir / slug).exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def create_from(agents_dir: Path, agent: agentfs.Agent, extra_files: dict[str, str] | None = None) -> str:
    """Write an agent built elsewhere (drafted or imported) into a new folder."""
    agents_dir.mkdir(parents=True, exist_ok=True)
    slug = unique_slug(agents_dir, agent.name)
    folder = agents_dir / slug
    agentfs.write(agent, folder)
    for rel, text in (extra_files or {}).items():
        (folder / rel).write_text(text, encoding="utf-8")
    return slug


def create(agents_dir: Path, name: str, template: str = "") -> str:
    name = (name or "").strip()
    if not name or len(name) > 80:
        raise BuilderError("the agent needs a name (up to 80 characters)")
    base = agentfs.slugify(name, fallback="agent")[:50]
    slug, n = base, 2
    while (agents_dir / slug).exists():
        slug, n = f"{base}-{n}", n + 1
    folder = agents_dir / slug
    if template:
        if template not in templates():
            raise BuilderError(f"no template named {template!r}")
        src = resources.files("vk").joinpath("templates", template)
        with resources.as_file(src) as path:
            shutil.copytree(path, folder)
        agent = agentfs.load(folder)
        agent.name = name
        agentfs.write(agent, folder)
    else:
        agentfs.write(blank(name), folder)
    return slug
