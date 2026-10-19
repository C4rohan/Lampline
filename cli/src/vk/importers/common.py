"""Shared pieces for importing agents from other platforms.

An importer reads an export (a zip or a folder) and returns an agent in our
format plus a report of what it changed or couldn't carry over. It never
calls the network and never runs anything from the export.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from vk.agentfs import Agent
from vk.tools import split_url
from vk.lint import transition_tool_name

MAX_FILES = 20000
MAX_TOTAL = 200 * 1024 * 1024        # uncompressed bytes read from one export
MAX_FILE = 20 * 1024 * 1024


class ImportError_(ValueError):       # not the builtin ImportError
    pass


class Export:
    """Files of an export, from a zip (bytes or path) or a folder, by relative path."""

    def __init__(self, files: dict[str, bytes]):
        self.files = files

    @classmethod
    def open(cls, source: "str | Path | bytes | Export") -> "Export":
        if isinstance(source, Export):
            return source
        if isinstance(source, (bytes, bytearray)):
            return cls._from_zip(io.BytesIO(source))
        p = Path(source)
        if p.is_dir():
            files, total = {}, 0
            for f in sorted(p.rglob("*")):
                if f.is_file() and not f.is_symlink():
                    rel = f.relative_to(p).as_posix()
                    data = f.read_bytes()
                    total += len(data)
                    if len(files) >= MAX_FILES or total > MAX_TOTAL:
                        raise ImportError_("the export is too large")
                    files[rel] = data
            return cls(files)
        if p.is_file():
            return cls._from_zip(p)
        raise ImportError_(f"{source} is not a zip file or a folder")

    @classmethod
    def _from_zip(cls, src) -> "Export":
        try:
            z = zipfile.ZipFile(src)
        except zipfile.BadZipFile as exc:
            raise ImportError_("that file isn't a zip archive") from exc
        files, total = {}, 0
        with z:
            infos = [i for i in z.infolist() if not i.is_dir()]
            if len(infos) > MAX_FILES:
                raise ImportError_("the export has too many files")
            for info in infos:
                name = PurePosixPath(info.filename)
                if name.is_absolute() or ".." in name.parts:
                    continue                                   # never trust paths in an archive
                if info.file_size > MAX_FILE:
                    raise ImportError_(f"{info.filename} is too large")
                with z.open(info) as fh:                       # sizes in the header can lie: count what we read
                    data = fh.read(MAX_FILE + 1)
                if len(data) > MAX_FILE:
                    raise ImportError_(f"{info.filename} is too large")
                total += len(data)
                if total > MAX_TOTAL:
                    raise ImportError_("the export is too large once unpacked")
                files[name.as_posix()] = data
        return cls(files)

    def rebase(self, marker: str) -> "Export":
        """Strip a leading folder so `marker` (e.g. agent.json) is at the top."""
        hits = sorted((k for k in self.files if k == marker or k.endswith("/" + marker)), key=lambda k: k.count("/"))
        if not hits or hits[0] == marker:
            return self
        prefix = hits[0][: -len(marker)]
        return Export({k[len(prefix):]: v for k, v in self.files.items() if k.startswith(prefix)})

    def json(self, rel: str) -> Any:
        try:
            return json.loads(self.files[rel].decode("utf-8-sig"))
        except KeyError:
            return None
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImportError_(f"{rel} isn't valid JSON ({exc})") from exc

    def text(self, rel: str) -> str | None:
        data = self.files.get(rel)
        return data.decode("utf-8", "replace") if data is not None else None

    def glob(self, pattern: str) -> list[str]:
        rx = re.compile("^" + re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*", "[^/]*") + "$")
        return sorted(k for k in self.files if rx.match(k))


@dataclass
class Report:
    source: str
    notes: list[str] = field(default_factory=list)       # things to review
    counts: dict[str, int] = field(default_factory=dict)

    def note(self, msg: str) -> None:
        if msg not in self.notes:
            self.notes.append(msg)

    def markdown(self, agent_name: str) -> str:
        lines = [f"# Imported from {self.source}", "", f"Agent: {agent_name}", ""]
        if self.counts:
            lines += ["## What came across", ""] + [f"- {k}: {v}" for k, v in self.counts.items()] + [""]
        lines += ["## Review before using with callers", ""]
        lines += [f"- {n}" for n in self.notes] or ["- Nothing flagged."]
        lines += ["", "Run `vk lint` and test the agent before taking real calls. Use synthetic data while testing.", ""]
        return "\n".join(lines)


def snake(text: str, fallback: str = "x") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    if not s or not s[0].isalpha():
        s = f"{fallback}_{s}".strip("_")
    return s[:60]


def human(name: str) -> str:
    """'billing.question' / 'BillingQuestion' -> 'billing question'."""
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name or "")
    return re.sub(r"[._\-/]+", " ", s).strip().lower()


class GraphBuilder:
    """Collects steps and transfers, keeps ids, names and labels unique, lays them out."""

    def __init__(self):
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self._ids: set[str] = set()
        self._names: set[str] = set()

    def add(self, type_: str, name: str, data: dict, key: str | None = None) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", (key or name).lower()).strip("-")[:40] or "step"
        nid, n = base, 2
        while nid in self._ids:
            nid, n = f"{base}-{n}", n + 1
        self._ids.add(nid)
        nm, n = (name or nid)[:80], 2
        while nm.lower() in self._names:
            nm, n = f"{(name or nid)[:76]} {n}", n + 1
        self._names.add(nm.lower())
        self.nodes.append({"id": nid, "type": type_, "position": {"x": 0, "y": 0}, "data": {"name": nm, **data}})
        return nid

    def connect(self, src: str, dst: str, label: str, condition: str, speech: str = "", **extra) -> str | None:
        if src == dst:
            return None
        from vk.pages import route_kind
        kind = route_kind(extra)
        if any(e["source"] == src and e["target"] == dst and route_kind(e["data"]) == kind for e in self.edges):
            return None                               # one transfer of each kind between two steps
        used = {transition_tool_name(e["data"]["label"]) for e in self.edges if e["source"] == src}
        label = (label or "next").strip().lower()[:50] or "next"
        lab, n = label, 2
        while transition_tool_name(lab) in used:
            lab, n = f"{label} {n}", n + 1
        eid = f"{src}-{dst}"
        ids = {e["id"] for e in self.edges}
        k = 2
        while eid in ids:
            eid, k = f"{src}-{dst}-{k}", k + 1
        data = {"label": lab, "condition": (condition or f"The caller needs {label}.")[:1000], **extra}
        if speech:
            data["transition_speech"] = speech[:500]
        self.edges.append({"id": eid, "source": src, "target": dst, "data": data})
        return eid

    def layout(self) -> None:
        by_id = {n["id"]: n for n in self.nodes}
        start = next((n["id"] for n in self.nodes if n["type"] == "startCall"), None)
        depth: dict[str, int] = {}
        if start:
            depth[start] = 0
            q = deque([start])
            while q:
                cur = q.popleft()
                for e in self.edges:
                    if e["source"] == cur and e["target"] not in depth:
                        depth[e["target"]] = depth[cur] + 1
                        q.append(e["target"])
        last = max(depth.values(), default=0)
        rows: dict[int, list[str]] = {}
        for n in self.nodes:
            if n["type"] == "globalNode":
                continue
            d = depth.get(n["id"], last + 1)
            if n["type"] == "endCall":
                d = max(d, last + (1 if n["id"] in depth else 2))
            rows.setdefault(d, []).append(n["id"])
        right = 0
        for d in sorted(rows):
            ids = rows[d]
            x0 = -((len(ids) - 1) * 330) / 2
            for i, nid in enumerate(ids):
                by_id[nid]["position"] = {"x": round(x0 + i * 330), "y": d * 240}
                right = max(right, x0 + i * 330 + 280)
        for i, n in enumerate(x for x in self.nodes if x["type"] == "globalNode"):
            n["position"] = {"x": round(right + 120), "y": i * 220}

    def agent(self, name: str, context: dict, tools: list[dict], purpose: str) -> Agent:
        self.layout()
        return Agent(name=name[:80] or "Imported agent", definition={"nodes": self.nodes, "edges": self.edges},
                     context_variables=context, setup={"purpose": purpose, "real_data": False}, lint={"ignore": []}, tools=tools)


_SECRET_REF = re.compile(r"\{\{\s*secrets\.[^}]*\}\}")


def scrub_tools(tools: list[dict], report: Report) -> list[dict]:
    """An export is someone else's file: it never decides where your tool secrets go. Any {{secrets.X}} it
    carries is removed (you add your own after checking the address), and the address's host can't be a template."""
    for t in tools:
        before = json.dumps(t, sort_keys=True)
        if isinstance(t.get("url"), str) and "{{" in split_url(t["url"])[0]:
            t["url"] = ""
            report.note(f"tool {t.get('name')}: its address used a template in the host, so it was cleared; set it by hand.")
        for key in ("url", "body", "headers", "with", "mcp"):
            if key in t:
                t[key] = json.loads(_SECRET_REF.sub("", json.dumps(t[key])))
        if json.dumps(t, sort_keys=True) != before and t.get("url") != "":
            report.note(f"tool {t.get('name')}: the export named secrets in it; they were removed. Add your own key "
                        "(Integrations → Tool secrets) once you've checked the address.")
    return tools


def openapi_tools(schema_text: str, prefix: str, report: Report, where: str) -> list[dict]:
    """OpenAPI operations -> our HTTP tools (one per operation). Keys become secrets to add."""
    import yaml
    try:
        spec = yaml.safe_load(schema_text) if schema_text else None
    except yaml.YAMLError:
        spec = None
    if not isinstance(spec, dict):
        report.note(f"{where}: its OpenAPI schema couldn't be read, so no tool was made from it.")
        return []
    servers = spec.get("servers") or [{}]
    base = str((servers[0] or {}).get("url") or "").rstrip("/")
    if base and not base.startswith(("http://", "https://")):
        base = ""
    tools = []
    for path, ops in (spec.get("paths") or {}).items():
        if not isinstance(ops, dict):
            continue
        for method, op in ops.items():
            if method.upper() not in ("GET", "POST", "PUT", "PATCH", "DELETE") or not isinstance(op, dict):
                continue
            name = snake(op.get("operationId") or f"{prefix}_{method}_{path}", "tool")
            params = []
            for p in list(ops.get("parameters") or []) + list(op.get("parameters") or []):
                if isinstance(p, dict) and p.get("name") and p.get("in") in ("path", "query"):
                    t = ((p.get("schema") or {}).get("type") or "string")
                    params.append({"name": snake(p["name"], "p"), "type": t if t in ("string", "number", "integer", "boolean") else "string",
                                   "description": str(p.get("description") or "")[:300], "required": bool(p.get("required") or p.get("in") == "path")})
            body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {}
            for pname, ps in (body.get("properties") or {}).items():
                t = (ps or {}).get("type") or "string"
                params.append({"name": snake(pname, "p"), "type": t if t in ("string", "number", "integer", "boolean") else "string",
                               "description": str((ps or {}).get("description") or "")[:300], "required": pname in (body.get("required") or [])})
            seen, uniq = set(), []
            for p in params:
                if p["name"] not in seen:
                    seen.add(p["name"])
                    uniq.append(p)
            url_path = re.sub(r"\{([^}]+)\}", lambda m: "{{" + snake(m.group(1), "p") + "}}", path)
            tool = {"name": name, "description": (op.get("summary") or op.get("description") or name.replace("_", " "))[:600],
                    "method": method.upper(), "url": f"{base}{url_path}" if base else "", "parameters": uniq,
                    "mock": {"note": "example answer; replace with what the service really returns"}}
            if not base:
                report.note(f"tool {name}: the OpenAPI schema has no usable server URL; it answers with its mock until you add one.")
            if spec.get("components", {}).get("securitySchemes") or op.get("security"):
                report.note(f"tool {name}: the service needs authentication. Save the key under Integrations → Tool secrets "
                            "and add a header like Authorization: Bearer {{secrets.NAME}}.")
            tools.append(tool)
    return tools
