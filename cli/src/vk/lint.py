"""Offline checks for an agent folder.

VK0xx  structure: problems that would stop the agent running, caught before
       a call instead of during one.
VK1xx  design: things that are legal but usually a mistake.
VK111-116  tools: HTTP tools in agent.yaml and the steps that use them.
VK2xx  data: patient/customer data pasted into prompts, settings or tests.

Silence a finding in agent.yaml:
    lint:
      ignore: [VK105, "VK204:greeting"]     # code, or code:node-id
      prompt_budget_chars: 8000
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from pathlib import Path

import json

from vk import pages, phi
from vk import tools as toolspec
from vk.agentfs import Agent

TEMPLATE_VAR = re.compile(r"\{\{\s*([^|\s}]+)(?:\s*\|\s*([^:}]+)(?::([^}]+))?)?\s*\}\}")
SYSTEM_VARIABLES = {"campaign_id", "provider", "source_uuid"}
CLOCK_VARIABLES = {"current_date", "current_time", "current_weekday", "current_hour"}
FLOW_TYPES = {"startCall", "agentNode", "endCall"}
DETACHED_TYPES = {"globalNode", "trigger", "webhook", "qa"}  # never connected by edges
DEFAULT_PROMPT_BUDGET = 8000  # characters (~2,000 tokens)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str  # "error" | "warning"
    message: str
    where: str = ""

    def as_dict(self) -> dict:
        return {"code": self.code, "severity": self.severity, "message": self.message, "where": self.where}


def transition_tool_name(label: str) -> str:
    """How an edge label becomes the name of the tool the model calls to take that transfer."""
    return re.sub(r"[^a-z0-9]", "_", (label or "").lower())


def template_variables(text: str) -> set[str]:
    names: set[str] = set()
    for m in TEMPLATE_VAR.finditer(text or ""):
        name, filt = m.group(1).strip(), m.group(2)
        if "." in name or filt is not None or name in SYSTEM_VARIABLES:
            continue
        if name in ("current_time", "current_weekday") or name.startswith(("current_time_", "current_weekday_")):
            continue
        names.add(name)
    return names


def _node_label(node: dict) -> str:
    data = node.get("data") or {}
    return f"node {node.get('id')} ({data.get('name') or node.get('type')})"


def lint(agent: Agent, folder: str | Path | None = None) -> list[Finding]:
    nodes = agent.definition.get("nodes") or []
    edges = agent.definition.get("edges") or []
    by_id = {str(n.get("id")): n for n in nodes}
    findings: list[Finding] = []
    add = findings.append

    # --- VK0xx structure ---------------------------------------------------------
    types = Counter(n.get("type") for n in nodes)
    if types["startCall"] != 1:
        add(Finding("VK001", "error", f"needs exactly one Start Call node (found {types['startCall']})", "agent"))
    for t, label in (("globalNode", "Global"), ("trigger", "API Trigger")):
        if types[t] > 1:
            add(Finding("VK003", "error", f"at most one {label} node allowed (found {types[t]})", "agent"))

    out_edges: dict[str, list[dict]] = defaultdict(list)
    in_count: Counter = Counter()
    for e in edges:
        eid = e.get("id")
        src, dst = str(e.get("source")), str(e.get("target"))
        data = e.get("data") or {}
        missing = [x for x in (src, dst) if x not in by_id]
        if missing:
            add(Finding("VK004", "error", f"edge points to missing node(s) {', '.join(missing)}", f"edge {eid}"))
            continue
        if not str(data.get("label") or "").strip() or not str(data.get("condition") or "").strip():
            add(Finding("VK005", "error", "transition needs a label and a condition", f"edge {eid}"))
        out_edges[src].append(e)
        in_count[dst] += 1

    for nid, node in by_id.items():
        t = node.get("type")
        n_out, n_in = len(out_edges[nid]), in_count[nid]
        if t == "startCall" and n_in:
            add(Finding("VK007", "error", "Start Call cannot have incoming transitions", _node_label(node)))
        if t == "endCall" and n_out:
            add(Finding("VK007", "error", "End Call cannot have outgoing transitions", _node_label(node)))
        if t in DETACHED_TYPES and (n_in or n_out):
            add(Finding("VK007", "error", f"{t} nodes cannot be connected by transitions", _node_label(node)))
        if t in ("agentNode", "endCall") and n_in == 0:
            add(Finding("VK008", "error", "needs at least one incoming transition", _node_label(node)))

    for src, outs in out_edges.items():
        seen: dict[str, str] = {}
        for e in outs:
            tool = transition_tool_name((e.get("data") or {}).get("label", ""))
            if not tool.strip("_"):
                continue
            if tool in seen:
                add(Finding(
                    "VK006", "error",
                    f'transitions {seen[tool]} and {e.get("id")} both become tool "{tool}"; use distinct labels',
                    _node_label(by_id[src]),
                ))
            else:
                seen[tool] = str(e.get("id"))

    # --- VK1xx design --------------------------------------------------------------
    starts = [nid for nid, n in by_id.items() if n.get("type") == "startCall"]
    reachable: set[str] = set()
    queue = deque(starts)
    while queue:
        cur = queue.popleft()
        if cur in reachable:
            continue
        reachable.add(cur)
        queue.extend(str(e.get("target")) for e in out_edges[cur])
    if starts:
        for nid, node in by_id.items():
            if node.get("type") in FLOW_TYPES and nid not in reachable:
                add(Finding("VK101", "warning", "can never be reached from Start Call", _node_label(node)))

    if not types["endCall"]:
        add(Finding("VK102", "warning", "no End Call node: calls can only end when the caller hangs up", "agent"))

    for nid, node in by_id.items():
        t = node.get("type")
        if t in ("startCall", "agentNode") and not out_edges[nid]:
            add(Finding("VK103", "warning", "dead end: no outgoing transition, so the conversation is stuck here", _node_label(node)))

    budget = int((agent.lint or {}).get("prompt_budget_chars") or DEFAULT_PROMPT_BUDGET)
    for nid, node in by_id.items():
        data = node.get("data") or {}
        if node.get("type") in FLOW_TYPES and not pages.scripted(data):
            prompt = data.get("prompt") or ""
            if not prompt.strip():
                add(Finding("VK104", "warning", "empty prompt", _node_label(node)))
            elif len(prompt) > budget:
                add(Finding("VK105", "warning", f"prompt is {len(prompt):,} chars (budget {budget:,}); long prompts add latency and cost on every turn", _node_label(node)))

    for src, outs in out_edges.items():
        outs = [e for e in outs if pages.route_kind(e.get("data")) == "ask"]       # rule routes aren't for the model
        conds = Counter(re.sub(r"\s+", " ", str((e.get("data") or {}).get("condition") or "").strip().lower()) for e in outs)
        for cond, count in conds.items():
            if cond and count > 1:
                add(Finding("VK106", "warning", f'{count} transitions share the condition "{cond}"; the LLM cannot tell them apart', _node_label(by_id[src])))

    provided = set((agent.context_variables or {}).keys())
    for tool in agent.tools or []:                     # values a tool saves from its answer
        if isinstance(tool, dict) and isinstance(tool.get("save"), dict):
            provided |= {str(k) for k in tool["save"]}
    pre_call_fetch = False
    for node in nodes:
        data = node.get("data") or {}
        for var in data.get("extraction_variables") or []:
            if isinstance(var, dict) and var.get("name"):
                provided.add(var["name"])
        if pages.scripted(data):
            provided |= {str(f.get("name")) for f in data.get("form") or [] if isinstance(f, dict) and f.get("name")}
        if data.get("pre_call_fetch_mode") not in (None, "", "disabled", "off", False):
            pre_call_fetch = True
    if not pre_call_fetch:
        for node in nodes:
            data = node.get("data") or {}
            used: set[str] = set()
            for fld in ("prompt", "greeting", "extraction_prompt", "say", "done_say", "no_match_say"):
                used |= template_variables(data.get(fld) or "")
            if pages.scripted(data):
                for f in data.get("form") or []:
                    if isinstance(f, dict):
                        for fld in ("ask", "reprompt", "confirm"):
                            used |= template_variables(str(f.get(fld) or ""))
            for var in sorted(used - provided):
                add(Finding("VK107", "warning", f"uses {{{{{var}}}}} but nothing provides it (add it to context_variables or extract it)", _node_label(node)))

    # --- VK130-132 scripted pages and rule transfers ---------------------------------
    for node in nodes:
        data = node.get("data") or {}
        if not pages.scripted(data):
            if data.get("mode") not in (None, "llm"):
                add(Finding("VK130", "error", "mode must be llm or scripted", _node_label(node)))
            continue
        if node.get("type") not in ("agentNode", "endCall"):
            add(Finding("VK130", "error", f"{node.get('type')} steps can't be scripted", _node_label(node)))
        for severity, message in pages.problems(data):
            add(Finding("VK130", severity, message, _node_label(node)))
        nid = str(node.get("id"))
        kinds = [pages.route_kind(e.get("data")) for e in out_edges[nid]]
        if node.get("type") == "agentNode" and not out_edges[nid] and not str(data.get("say") or "").strip() and not data.get("form"):
            add(Finding("VK130", "warning", "a scripted page with nothing to say, no fields to ask and no way on", _node_label(node)))
        if node.get("type") == "agentNode" and kinds and not ({"when", "otherwise"} & set(kinds)) and (data.get("form") or []):
            add(Finding("VK132", "warning", "nothing moves on once the form is complete: add a transfer with a condition or “otherwise”",
                        _node_label(node)))
        if data.get("interpret") == "exact" and "ask" in kinds:
            add(Finding("VK132", "warning", "interpret is exact, so the model never checks this page's other transfers",
                        _node_label(node)))
    for e in edges:
        d = e.get("data") or {}
        kind = pages.route_kind(d)
        src = by_id.get(str(e.get("source"))) or {}
        if kind == "when":
            try:
                pages.compile_expression(d["when"])
            except pages.ExpressionError as exc:
                add(Finding("VK131", "error", str(exc), f"edge {e.get('id')}"))
                continue
            for var in sorted(pages.names_in(d["when"]) - provided - SYSTEM_VARIABLES - CLOCK_VARIABLES):
                add(Finding("VK131", "warning", f"the condition uses {var}, which nothing provides", f"edge {e.get('id')}"))
        elif kind in ("otherwise", "no_match") and not pages.scripted(src.get("data")):
            add(Finding("VK131", "warning", f"“{'otherwise' if kind == 'otherwise' else 'no match'}” only applies to transfers "
                                            "from a scripted page", f"edge {e.get('id')}"))
        if src.get("type") == "startCall" and kind != "ask":
            add(Finding("VK131", "error", "transfers from Start can't have conditions; Start hands over right after the greeting",
                        f"edge {e.get('id')}"))

    for node in nodes:
        data = node.get("data") or {}
        if node.get("type") == "webhook" and data.get("enabled", True) and not data.get("endpoint_url"):
            add(Finding("VK108", "warning", "webhook is enabled but has no endpoint URL", _node_label(node)))
        if data.get("pre_call_fetch_mode") not in (None, "", "disabled", "off", False) and not data.get("pre_call_fetch_url"):
            add(Finding("VK108", "warning", "pre-call fetch is on but has no URL", _node_label(node)))

    names = Counter(((n.get("data") or {}).get("name") or "").strip().lower() for n in nodes)
    for name, count in names.items():
        if name and count > 1:
            add(Finding("VK109", "warning", f'{count} nodes are named "{name}"; give each a unique name', "agent"))

    # --- VK110 tests that point at steps which don't exist -----------------------
    if folder is not None and (Path(folder) / "tests").is_dir():
        from vk import scenario  # local import: scenario imports yaml only

        known = {str(n.get("id")) for n in nodes} | {
            ((n.get("data") or {}).get("name") or "").strip().lower() for n in nodes}
        try:
            scenarios = scenario.load(folder)
        except scenario.TestFileError as exc:
            add(Finding("VK110", "error", f"test file problem: {exc}", "tests"))
            scenarios = []
        for sc in scenarios:
            for where, ref in scenario.node_refs(sc):
                if ref not in known and ref.strip().lower() not in known:
                    add(Finding("VK110", "error", f'{where} names step "{ref}", which this agent does not have',
                                sc.path))

    # --- VK111-116 tools ------------------------------------------------------------
    tool_names: Counter = Counter()
    for i, tool in enumerate(agent.tools or []):
        tname = tool.get("name") if isinstance(tool, dict) and isinstance(tool.get("name"), str) else f"#{i + 1}"
        tool_names[tname] += 1
        for severity, message in toolspec.problems(tool):
            add(Finding("VK111", severity, message, f"tool {tname}"))
    for tname, count in tool_names.items():
        if count > 1:
            add(Finding("VK112", "error", f"{count} tools are named {tname}; tool names must be unique", f"tool {tname}"))
    functions: dict[str, str] = {}          # name the model sees -> tool that provides it
    funcs_of: dict[str, list[str]] = {}
    for tool in agent.tools or []:
        if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
            continue
        funcs_of[tool["name"]] = toolspec.function_names(tool)
        for fn in funcs_of[tool["name"]]:
            if fn in functions and functions[fn] != tool["name"]:
                add(Finding("VK112", "error", f"{tool['name']} and {functions[fn]} both give the model a tool named {fn}",
                            f"tool {tool['name']}"))
            functions.setdefault(fn, tool["name"])
    used_tools: set[str] = set()
    for nid, node in by_id.items():
        listed = (node.get("data") or {}).get("tools") or []
        if not isinstance(listed, list):
            add(Finding("VK113", "error", "tools must be a list of tool names", _node_label(node)))
            continue
        if listed and node.get("type") not in ("startCall", "agentNode", "endCall"):
            add(Finding("VK113", "error", f"{node.get('type')} nodes cannot use tools", _node_label(node)))
        transfer_tools = {transition_tool_name((e.get("data") or {}).get("label", "")) for e in out_edges[nid]}
        for tname in listed:
            used_tools.add(str(tname))
            if str(tname) not in tool_names:
                add(Finding("VK113", "error", f"uses tool {tname}, which this agent does not define", _node_label(node)))
            else:
                for fn in funcs_of.get(str(tname), [str(tname)]):
                    if fn in transfer_tools:
                        add(Finding("VK114", "error", f"tool {fn} has the same name as one of this step's transfers; "
                                                      "rename one of them", _node_label(node)))
    # --- VK133 tools a scripted page runs on entry / once its form is complete ----------
    by_tool = {t["name"]: t for t in agent.tools or [] if isinstance(t, dict) and isinstance(t.get("name"), str)}
    for node in nodes:
        data = node.get("data") or {}
        for when in ("on_entry", "on_done"):
            listed = data.get(when)
            if listed in (None, []):
                continue
            if not pages.scripted(data):
                add(Finding("VK133", "warning", f"{when} only runs on scripted pages", _node_label(node)))
                continue
            if not isinstance(listed, list) or not all(isinstance(x, str) for x in listed):
                add(Finding("VK133", "error", f"{when} must be a list of tool names", _node_label(node)))
                continue
            for tname in listed:
                used_tools.add(tname)
                tool = by_tool.get(tname)
                if tool is None:
                    add(Finding("VK133", "error", f"{when} runs {tname}, which this agent does not define", _node_label(node)))
                    continue
                if toolspec.kind(tool) == "connector":
                    from .connectors import ACTIONS, model_params
                    need = [p.name for p in model_params(tool) if p.required] if tool.get("connector") in ACTIONS else []
                elif toolspec.kind(tool) == "mcp":
                    need = []
                else:
                    need = [str(p.get("name")) for p in tool.get("parameters") or [] if isinstance(p, dict) and p.get("required")]
                missing = [n for n in need if n not in provided and n not in (agent.context_variables or {})]
                if missing:
                    add(Finding("VK133", "warning", f"{tname} needs {', '.join(missing)}, but nothing provides a variable by that "
                                                    "name; a page fills tool arguments from variables (or pin them in `with`)",
                                _node_label(node)))
    for tname in tool_names:
        if not tname.startswith("#") and tname not in used_tools:
            add(Finding("VK115", "warning", "no step uses this tool", f"tool {tname}"))
    known = set(agent.context_variables or {}) | provided
    for tool in agent.tools or []:
        if not isinstance(tool, dict):
            continue
        args = {p.get("name") for p in tool.get("parameters") or [] if isinstance(p, dict)}
        if toolspec.kind(tool) == "connector":
            from .connectors import ACTIONS, model_params
            args = {p.name for p in model_params(tool)} if tool.get("connector") in ACTIONS else set()
        refs = toolspec.template_names([tool.get("url"), tool.get("headers"), tool.get("body"), tool.get("mock"), tool.get("with")])
        for var in sorted(n for n in refs if not n.startswith("secrets.") and n not in args and n not in known
                          and n not in SYSTEM_VARIABLES):
            add(Finding("VK116", "warning", f"uses {{{{{var}}}}}, which is not a parameter or a known variable",
                        f"tool {tool.get('name')}"))

    # --- VK120-122 triggers ---------------------------------------------------------
    from . import triggers as triggerspec
    ids: Counter = Counter()
    channels: Counter = Counter()
    for i, trig in enumerate(agent.triggers or []):
        tid = trig.get("id") if isinstance(trig, dict) and isinstance(trig.get("id"), str) else f"#{i + 1}"
        ids[tid] += 1
        for severity, message in triggerspec.problems(trig):
            add(Finding("VK120", severity, message, f"trigger {tid}"))
        if isinstance(trig, dict) and trig.get("type") == "message" and triggerspec.enabled(trig):
            channels[trig.get("channel")] += 1
    for tid, count in ids.items():
        if count > 1:
            add(Finding("VK121", "error", f"{count} triggers have the id {tid}", f"trigger {tid}"))
    for ch, count in channels.items():
        if count > 1:
            add(Finding("VK122", "error", f"{count} message triggers answer {ch}; keep one", "triggers"))

    # --- VK2xx data --------------------------------------------------------------
    def scan(text: str, where: str) -> None:
        for hit in phi.scan(text or ""):
            add(Finding(hit.code, hit.severity, f"{hit.label}: {hit.excerpt}", where))

    for node in nodes:
        data = node.get("data") or {}
        for fld in ("prompt", "greeting", "extraction_prompt"):
            scan(data.get(fld) or "", f"{_node_label(node)} {fld}")
    for e in edges:
        data = e.get("data") or {}
        scan(f"{data.get('condition') or ''}\n{data.get('transition_speech') or ''}", f"edge {e.get('id')}")
    for key, value in (agent.context_variables or {}).items():
        scan(str(value), f"context_variables.{key}")
    for tool in agent.tools or []:
        if isinstance(tool, dict):
            text = json.dumps({k: tool.get(k) for k in ("description", "url", "body", "mock", "say_while_running", "with")},
                              ensure_ascii=False)
            scan(text, f"tool {tool.get('name')}")
    for trig in agent.triggers or []:
        if isinstance(trig, dict):
            scan(json.dumps({k: trig.get(k) for k in ("send", "recipients", "text")}, ensure_ascii=False),
                 f"trigger {trig.get('id')}")
    if folder is not None:
        tests = Path(folder) / "tests"
        if tests.is_dir():
            for path in sorted(tests.glob("*.y*ml")):
                scan(path.read_text(encoding="utf-8"), f"tests/{path.name}")

    return _apply_ignores(findings, (agent.lint or {}).get("ignore") or [])


def _apply_ignores(findings: list[Finding], ignore: list) -> list[Finding]:
    rules = {str(x) for x in ignore}
    kept = []
    for f in findings:
        node_id = ""
        m = re.match(r"node (\S+)", f.where)
        if m:
            node_id = m.group(1)
        if f.code in rules or (node_id and f"{f.code}:{node_id}" in rules):
            continue
        kept.append(f)
    return kept


def has_errors(findings: list[Finding]) -> bool:
    return any(f.severity == "error" for f in findings)
