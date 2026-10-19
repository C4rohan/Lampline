"""Shared pieces for exporting our agents to other platforms.

An exporter turns an agent folder into the files another platform imports,
plus a report of what it changed or couldn't express there. Secrets never
leave: {{secrets.NAME}} becomes a placeholder you fill in on the other side.
"""

from __future__ import annotations

import io
import json
import keyword
import re
import zipfile
from dataclasses import dataclass, field
from typing import Any

from vk import pages
from vk.agentfs import Agent
from vk.tools import TEMPLATE, split_url


class ExportError(ValueError):
    pass


@dataclass
class Report:
    target: str
    notes: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def note(self, msg: str) -> None:
        if msg not in self.notes:
            self.notes.append(msg)

    def markdown(self, agent_name: str) -> str:
        lines = [f"# Exported to {self.target}", "", f"Agent: {agent_name}", ""]
        if self.counts:
            lines += ["## What went across", ""] + [f"- {k}: {v}" for k, v in self.counts.items()] + [""]
        lines += ["## Check after importing", ""] + ([f"- {n}" for n in self.notes] or ["- Nothing flagged."]) + [""]
        return "\n".join(lines)


@dataclass
class Step:
    id: str
    type: str
    name: str
    data: dict
    out: list[dict] = field(default_factory=list)       # edges leaving this step

    @property
    def scripted(self) -> bool:
        return pages.scripted(self.data)

    @property
    def prompt(self) -> str:
        """The step's instructions; a scripted page's are written out from its script."""
        if self.scripted:
            return pages.describe(self.data)
        return str(self.data.get("prompt") or "").strip()


def rule_text(edge: dict, src: Step) -> str:
    """What a transfer's condition means, in words, for platforms where the model decides."""
    d = edge.get("data") or {}
    kind = pages.route_kind(d)
    if kind == "when":
        return f"the details collected so far show {d['when']}"
    if kind == "otherwise":
        return "you have every answer and no rule above applies" if src.data.get("form") else "no rule above applies"
    if kind == "no_match":
        return f"you still can't understand the caller after {src.data.get('max_attempts', 3)} tries"
    return str(d.get("condition") or d.get("label") or "").strip()


class Plan:
    """The agent graph in a shape exporters can walk."""

    def __init__(self, agent: Agent):
        nodes = (agent.definition or {}).get("nodes") or []
        edges = (agent.definition or {}).get("edges") or []
        self.agent = agent
        self.steps: dict[str, Step] = {}
        for n in nodes:
            d = n.get("data") or {}
            self.steps[n["id"]] = Step(n["id"], n.get("type", ""), str(d.get("name") or n["id"]), d)
        for e in edges:
            if e.get("source") in self.steps and e.get("target") in self.steps:
                self.steps[e["source"]].out.append(e)
        starts = [s for s in self.steps.values() if s.type == "startCall"]
        if len(starts) != 1:
            raise ExportError("the agent needs exactly one Start step")
        self.start = starts[0]
        self.globals = [s for s in self.steps.values() if s.type == "globalNode"]
        self.tools = {t["name"]: t for t in agent.tools or [] if isinstance(t, dict) and t.get("name")}

    def target(self, edge: dict) -> Step:
        return self.steps[edge["target"]]

    def agents(self) -> list[Step]:
        """Conversation steps (Start's hand-over target first), in graph order."""
        order, seen = [], set()
        queue = [self.target(e) for e in self.start.out]
        while queue:
            s = queue.pop(0)
            if s.id in seen or s.type != "agentNode":
                continue
            seen.add(s.id)
            order.append(s)
            queue += [self.target(e) for e in s.out]
        order += [s for s in self.steps.values() if s.type == "agentNode" and s.id not in seen]
        return order

    def root(self) -> Step | None:
        """Where Start hands over: the after-greeting transfer, else Start's only agent target."""
        handoff = [e for e in self.start.out if (e.get("data") or {}).get("after_greeting")]
        targets = [self.target(e) for e in (handoff or self.start.out) if self.target(e).type == "agentNode"]
        return targets[0] if len(targets) == 1 or handoff else None

    def global_text(self) -> str:
        return "\n\n".join(s.prompt for s in self.globals if s.prompt)


def single_braces(text: str) -> str:
    """{{name}} -> {name} (Google's variable syntax). Secrets never appear in prompts."""
    return TEMPLATE.sub(lambda m: "{" + m.group(1) + "}", text or "")


def safe_name(text: str, taken: set[str], fallback: str = "Item") -> str:
    """Letters, digits and _ only (Google's naming rules), unique within `taken`."""
    base = re.sub(r"[^A-Za-z0-9_]+", "_", (text or "").strip()).strip("_")[:60] or fallback
    if not base[0].isalpha():
        base = f"{fallback}_{base}"
    name, n = base, 2
    while name.lower() in taken:
        name, n = f"{base}_{n}", n + 1
    taken.add(name.lower())
    return name


def openapi_for(tools: list[dict], title: str, report: Report, where: str) -> tuple[dict, str]:
    """Our HTTP tools (same address) -> an OpenAPI 3 document. Returns (spec, server_url)."""
    origin = split_url(tools[0].get("url") or "")[0]
    server = origin
    if not origin or TEMPLATE.search(origin):
        server = "https://replace-me.invalid"
        report.note(f"{where}: the address came from a secret or wasn't set; set the server URL after importing.")
    paths: dict[str, dict] = {}
    schemes: dict[str, dict] = {}
    for t in tools:
        path = split_url(t.get("url") or "")[1] or f"/{t['name']}"
        path, _, query = path.partition("?")
        path = TEMPLATE.sub(lambda m: "{" + m.group(1) + "}", path) or "/"
        method = str(t.get("method") or "GET").lower()
        in_path = set(re.findall(r"\{([^}]+)\}", path))
        params, body = [], {}
        for p in t.get("parameters") or []:
            schema = {"type": p.get("type") or "string"}
            if p["name"] in in_path:
                params.append({"name": p["name"], "in": "path", "required": True, "schema": schema,
                               **({"description": p["description"]} if p.get("description") else {})})
            elif method in ("get", "delete"):
                params.append({"name": p["name"], "in": "query", "required": bool(p.get("required")), "schema": schema,
                               **({"description": p["description"]} if p.get("description") else {})})
            else:
                body[p["name"]] = {**schema, **({"description": p["description"]} if p.get("description") else {})}
        for k, v in (dict(x.split("=", 1) for x in query.split("&") if "=" in x)).items():
            if not TEMPLATE.search(v):
                continue
            if all(q["name"] != k for q in params):
                params.append({"name": k, "in": "query", "required": False, "schema": {"type": "string"}})
        op: dict[str, Any] = {"operationId": t["name"], "summary": (t.get("description") or t["name"])[:300]}
        if params:
            op["parameters"] = params
        if body:
            req = [p["name"] for p in t.get("parameters") or [] if p.get("required") and p["name"] in body]
            op["requestBody"] = {"required": True, "content": {"application/json": {"schema": {
                "type": "object", "properties": body, **({"required": req} if req else {})}}}}
        example = t.get("mock")
        media: dict[str, Any] = {"schema": {"type": "object"}}
        if example is not None:
            media["example"] = example
        op["responses"] = {"200": {"description": "OK", "content": {"application/json": media}}}
        for h, v in (t.get("headers") or {}).items():
            if "secrets." in str(v):
                name = re.sub(r"[^A-Za-z0-9]+", "_", h).strip("_") or "key"
                schemes[name] = {"type": "apiKey", "in": "header", "name": h}
                op.setdefault("security", []).append({name: []})
                report.note(f"{where}: header {h} used a secret; add the key as the API authentication after importing.")
        if t.get("body"):
            report.note(f"tool {t['name']}: its custom request body template wasn't exported; the body lists the parameters.")
        paths.setdefault(path, {})[method] = op
    spec: dict[str, Any] = {"openapi": "3.0.0", "info": {"title": title, "version": "1.0.0"},
                            "servers": [{"url": server}], "paths": paths}
    if schemes:
        spec["components"] = {"securitySchemes": schemes}
    return spec, server


def by_origin(tools: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for t in tools:
        out.setdefault(split_url(t.get("url") or "")[0], []).append(t)
    return out


def code_name(name: Any, fallback: str = "tool") -> str:
    """A name that is safe to write as a Python identifier or a path segment."""
    out = re.sub(r"\W", "_", str(name or "")).strip("_")[:64] or fallback
    out = out if out[0].isalpha() or out[0] == "_" else f"{fallback}_{out}"
    return f"{out}_" if keyword.iskeyword(out) else out


def python_stub(tool: dict) -> str:
    """A tool with only a mock answer -> a Python function returning that answer (CX Agent Studio runs these).
    Nothing from the agent is written as code: names become identifiers, text goes into string literals."""
    types = {"string": "str", "number": "float", "integer": "int", "boolean": "bool"}
    name = code_name(tool.get("name"))
    params = sorted(tool.get("parameters") or [], key=lambda p: not p.get("required"))
    args = [f"{code_name(p.get('name'), 'arg')}: {types.get(p.get('type'), 'str')}" + ("" if p.get("required") else " = None")
            for p in params]
    doc = [str(tool.get("description") or name)]
    if tool.get("parameters"):
        doc += ["", "Args:"] + [f"  {code_name(p.get('name'), 'arg')}: {p.get('description') or p.get('name')}"
                                for p in tool["parameters"]]
    answer = json.dumps(tool.get("mock") if tool.get("mock") is not None else {"ok": True}, ensure_ascii=False)
    return (f'"""{name}: exported with its example answer. Replace with the real lookup."""\n\n'
            "import json\nfrom typing import Any\n\n\n"
            f"def {name}({', '.join(args)}) -> Any:\n"
            f"    {json.dumps(chr(10).join(doc), ensure_ascii=False)}\n"
            f"    return json.loads({answer!r})\n")


def safe_rel(rel: str) -> str:
    """An exported file's path: relative, with no way out of the export's folder."""
    parts = rel.replace("\\", "/").split("/")
    if rel.startswith("/") or any(p in ("", ".", "..") for p in parts):
        raise ExportError(f"refusing to write {rel!r}: it would leave the export's folder")
    return rel


def zip_bytes(files: dict[str, str | bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel, content in sorted(files.items()):
            rel = safe_rel(rel)
            z.writestr(rel, content.encode("utf-8") if isinstance(content, str) else content)
    return buf.getvalue()


def dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
