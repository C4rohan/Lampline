"""Agents as code: an agent definition (JSON) <-> a reviewable folder.

    agents/front-desk/
    ├── agent.yaml        # name, nodes, transitions, settings (prompts referenced by file)
    ├── prompts/
    │   ├── greeting.md   # one Markdown file per node prompt
    │   └── reschedule.md
    └── tests/            # scenario tests (vk test, later)

The conversion is lossless: folder -> JSON -> folder reproduces the same files,
and JSON -> folder -> JSON reproduces the same workflow definition.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

FORMAT_VERSION = 1
AGENT_FILE = "agent.yaml"
PROMPTS_DIR = "prompts"

# node data fields stored as separate Markdown files: field -> filename suffix
TEXT_FIELDS = {"prompt": "", "extraction_prompt": ".extraction"}

# agent.yaml keys that aren't part of the agent definition itself
LOCAL_ONLY_KEYS = ("setup", "lint", "tools")


@dataclass
class Agent:
    name: str
    definition: dict[str, Any]
    context_variables: dict[str, Any] = field(default_factory=dict)
    setup: dict[str, Any] = field(default_factory=dict)
    lint: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)   # HTTP tools (local; see vk.tools)
    triggers: list[dict[str, Any]] = field(default_factory=list)  # schedules, channels, after-call (local; see vk.triggers)


# --- YAML formatting -----------------------------------------------------------

class _Dumper(yaml.SafeDumper):
    pass


def _str_presenter(dumper: yaml.SafeDumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_Dumper.add_representer(str, _str_presenter)


def _dump_yaml(data: Any) -> str:
    return yaml.dump(data, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)


# --- naming --------------------------------------------------------------------

def slugify(text: str, fallback: str = "node") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug or fallback


def _prompt_filenames(nodes: list[dict]) -> dict[str, str]:
    """Stable, unique base filename per node id, derived from the node name."""
    used: set[str] = set()
    names: dict[str, str] = {}
    for node in nodes:
        data = node.get("data") or {}
        # The fallback is slugified too: a node id is client input and must never become a path.
        base = slugify(data.get("name") or "", fallback="node-" + slugify(str(node.get("id") or ""), fallback="step"))
        candidate, n = base, 2
        while candidate in used:
            candidate = f"{base}-{n}"
            n += 1
        used.add(candidate)
        names[str(node.get("id"))] = candidate
    return names


# --- render / parse --------------------------------------------------------------

def render_files(agent: Agent, *, include_local: bool = True) -> dict[str, str]:
    """Return {relative path: file text} for an agent."""
    definition = copy.deepcopy(agent.definition)
    nodes = definition.get("nodes") or []
    filenames = _prompt_filenames(nodes)
    files: dict[str, str] = {}

    for node in nodes:
        data = node.get("data")
        if not isinstance(data, dict):
            continue
        base = filenames[str(node.get("id"))]
        for fld, suffix in TEXT_FIELDS.items():
            text = data.get(fld)
            if text is not None and not isinstance(text, str):
                raise FolderError(f"node {node.get('id')}: {fld} must be text")
            if isinstance(text, str):
                rel = f"{PROMPTS_DIR}/{base}{suffix}.md"
                files[rel] = text + "\n"
                data[fld] = {"file": rel}

    doc: dict[str, Any] = {"vk_format": FORMAT_VERSION, "name": agent.name}
    doc["context_variables"] = agent.context_variables or {}
    if include_local:
        doc["setup"] = agent.setup or {}
        doc["lint"] = agent.lint or {"ignore": []}
    doc["nodes"] = nodes
    doc["edges"] = definition.get("edges") or []
    extra = {k: v for k, v in definition.items() if k not in ("nodes", "edges")}
    if extra:
        doc["extra"] = extra
    if include_local and agent.tools:
        doc["tools"] = copy.deepcopy(agent.tools)
    if include_local and agent.triggers:
        doc["triggers"] = copy.deepcopy(agent.triggers)

    files[AGENT_FILE] = _dump_yaml(doc)
    return dict(sorted(files.items()))


class FolderError(Exception):
    pass


def load(folder: str | Path) -> Agent:
    root = Path(folder)
    agent_file = root / AGENT_FILE
    if not agent_file.exists():
        raise FolderError(f"{agent_file} not found (is this an agent folder?)")
    try:
        doc = yaml.safe_load(agent_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise FolderError(f"{agent_file}: invalid YAML ({exc})") from exc
    if doc.get("vk_format") != FORMAT_VERSION:
        raise FolderError(f"{agent_file}: unsupported vk_format {doc.get('vk_format')!r}")

    nodes = copy.deepcopy(doc.get("nodes") or [])
    for node in nodes:
        data = node.get("data")
        if not isinstance(data, dict):
            continue
        for fld in TEXT_FIELDS:
            ref = data.get(fld)
            if isinstance(ref, dict) and "file" in ref:
                rel = ref["file"]
                path = (root / str(rel)).resolve() if isinstance(rel, str) else None
                if path is None or Path(str(rel)).is_absolute() or not path.is_relative_to(root.resolve()) \
                        or path.suffix.lower() not in (".md", ".txt"):
                    raise FolderError(f"node {node.get('id')}: {fld} must point at a .md file inside the agent's folder")
                if not path.exists():
                    raise FolderError(f"node {node.get('id')}: {ref['file']} is missing")
                text = path.read_text(encoding="utf-8")
                data[fld] = text[:-1] if text.endswith("\n") else text

    definition: dict[str, Any] = {"nodes": nodes, "edges": copy.deepcopy(doc.get("edges") or [])}
    definition.update(doc.get("extra") or {})

    return Agent(
        name=doc.get("name") or root.name,
        definition=definition,
        context_variables=doc.get("context_variables") or {},
        setup=doc.get("setup") or {},
        lint=doc.get("lint") or {},
        tools=_tools(doc.get("tools"), agent_file),
        triggers=_tools(doc.get("triggers"), agent_file, "triggers"),
    )


def _tools(raw: Any, agent_file: Path, key: str = "tools") -> list[dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise FolderError(f"{agent_file}: {key} must be a list")
    return copy.deepcopy(raw)


def write(agent: Agent, folder: str | Path) -> list[str]:
    """Write the agent to `folder`. Prompt files that no longer belong are removed."""
    root = Path(folder)
    files = render_files(agent)
    root.mkdir(parents=True, exist_ok=True)
    prompts = root / PROMPTS_DIR
    if prompts.exists():
        for stale in prompts.glob("*.md"):
            if f"{PROMPTS_DIR}/{stale.name}" not in files:
                stale.unlink()
    base = root.resolve()
    for rel, text in files.items():
        path = root / rel
        if not path.resolve().is_relative_to(base):          # defense in depth: never write outside the folder
            raise FolderError(f"refusing to write {rel!r} outside the agent folder")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return list(files)
