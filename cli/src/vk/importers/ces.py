"""CX Agent Studio app (exported as JSON or YAML) -> our agent.

CX Agent Studio is already agent-first, so the mapping is close:

    app.rootAgent              Start (no fixed greeting: the root agent opens) -> root agent step
    agents                     one step each, with its instruction
    childAgents                transfers parent -> child (the child's description says when),
                               and child -> parent ("something else")
    end_session system tool    a transfer to an End call step
    globalInstruction          global instructions
    variableDeclarations       variables, with their defaults
    OpenAPI tools / toolsets   HTTP tools (one per operation)
    client function tools      tool stubs with mock answers
    Python tools, callbacks    not run here: tools become stubs with mock answers, callbacks are listed

Guardrails, data store and search tools, examples and evaluations aren't
carried over yet; the report lists them. Nothing from the export is executed.
"""

from __future__ import annotations

import re
from typing import Any

import yaml

from .common import scrub_tools, Export, GraphBuilder, ImportError_, Report, human, openapi_tools, snake

RESERVED = {"end_session", "transfer_to_agent", "customize_response"}
# What our own exporter writes into instructions (so a round trip comes back clean):
_GREETING = re.compile(r"When the conversation starts, greet the caller with: “([^”]*)”\s*")
_OUR_TRANSFERS = re.compile(r"\n*Transfers:\n(?:- When [^\n]*(?:\{@AGENT:|\{@TOOL: end_session\})[^\n]*\n?)+")
_OUR_TOOLS = re.compile(r"\n*Tools you can use: (?:\{@TOOL: [^}]+\}(?:, )?)+\.\n?")
_TYPES = {"STRING": "string", "NUMBER": "number", "INTEGER": "integer", "BOOLEAN": "boolean"}
CALLBACKS = ("beforeAgentCallbacks", "afterAgentCallbacks", "beforeModelCallbacks", "afterModelCallbacks",
             "beforeToolCallbacks", "afterToolCallbacks")


def _instruction_text(text: str) -> str:
    """{@TOOL: x} / {@AGENT: y} -> plain names; {var} -> {{var}}."""
    text = re.sub(r"\{@(?:TOOL|AGENT)\s*:\s*([^}]+)\}", lambda m: m.group(1).strip(), text)
    return re.sub(r"(?<!\{)\{([A-Za-z_][\w.]*)\}(?!\})", lambda m: "{{" + snake(m.group(1), "v") + "}}", text)


class _App:
    def __init__(self, ex: Export, report: Report):
        self.ex, self.report = ex, report
        self.app = self.doc("app")
        if not isinstance(self.app, dict):
            raise ImportError_("no app.json or app.yaml found: export the app from CX Agent Studio (JSON or YAML)")

    def doc(self, stem: str) -> Any:
        """Read <stem>.json / .yaml / .yml."""
        for ext in ("json", "yaml", "yml"):
            rel = f"{stem}.{ext}"
            if rel in self.ex.files:
                if ext == "json":
                    return self.ex.json(rel)
                try:
                    return yaml.safe_load(self.ex.text(rel) or "")
                except yaml.YAMLError as exc:
                    raise ImportError_(f"{rel} isn't valid YAML ({exc})") from exc
        return None

    def text_field(self, value: Any) -> str:
        """Text fields hold a path to a file in the export (or, sometimes, the text itself)."""
        s = str(value or "")
        if s and "\n" not in s and len(s) < 300 and s in self.ex.files:
            return self.ex.text(s) or ""
        return s

    def resources(self, kind: str) -> dict[str, dict]:
        out: dict[str, dict] = {}
        folders = sorted({k.split("/")[1] for k in self.ex.files if k.startswith(f"{kind}/") and k.count("/") >= 2})
        for folder in folders:
            d = self.doc(f"{kind}/{folder}/{folder}")
            if isinstance(d, dict):
                out[d.get("displayName") or folder] = d
                if folder != d.get("displayName"):
                    out.setdefault(folder, d)          # references may use the folder spelling
        return out


def convert(source, name: str = "") -> tuple[Any, Report]:
    ex = Export.open(source)
    ex = ex.rebase("app.json") if any(k.endswith("app.json") for k in ex.files) else ex.rebase("app.yaml")
    report = Report("CX Agent Studio")
    cx = _App(ex, report)
    app = cx.app
    agents = cx.resources("agents")
    tool_docs = cx.resources("tools")
    toolsets = cx.resources("toolsets")
    g = GraphBuilder()
    tools: list[dict] = []
    tool_names: set[str] = set()
    agent_name = name or app.get("displayName") or "Imported agent"

    def add_tool(t: dict) -> str:
        base, n = t["name"], 2
        while t["name"] in tool_names:
            t["name"], n = f"{base}_{n}", n + 1
        tool_names.add(t["name"])
        tools.append(t)
        return t["name"]

    converted: dict[str, list[str]] = {}
    python_tools: list[str] = []

    def tool_for(ref: str) -> list[str]:
        if ref in converted:
            return converted[ref]
        doc = tool_docs.get(ref) or {}
        names: list[str] = []
        if doc.get("openApiTool"):
            spec = doc["openApiTool"]
            names = [add_tool(t) for t in openapi_tools(cx.text_field(spec.get("openApiSchema")), snake(ref, "tool"), report, f"tool {ref}")]
            if spec.get("url") and not str(spec["url"]).startswith("http"):
                report.note(f"tool {ref}: its address was an environment value ({spec['url']}); set the real one.")
        elif doc.get("clientFunction"):
            fn = doc["clientFunction"]
            params = _params((fn.get("parameters") or {}))
            names = [add_tool({"name": snake(fn.get("name") or ref, "tool"), "description": str(fn.get("description") or doc.get("description") or human(ref))[:600],
                               "method": "POST", "url": "", "parameters": params, "mock": {"note": "replace with the real answer"}})]
            report.note(f"tool {ref}: was a client-side function; it answers with a mock until you point it at a service.")
        elif doc.get("pythonFunction"):
            fn = doc["pythonFunction"]
            names = [add_tool({"name": snake(fn.get("name") or ref, "tool"),
                               "description": str(fn.get("description") or doc.get("description") or human(ref))[:600],
                               "method": "POST", "url": "", "parameters": [], "mock": {"note": "replace with the real answer"}})]
            python_tools.append(names[0])
        elif doc.get("dataStoreTool") or doc.get("fileSearchTool"):
            base = re.sub(r"[^a-z0-9_-]+", "-", ref.lower()).strip("-_")[:60] or "knowledge"
            base = base if base[:1].isalpha() else f"kb-{base}"
            names = [add_tool({"name": snake(ref, "tool"), "connector": "knowledge.search", "with": {"base": base},
                               "description": str(doc.get("description") or f"Search {human(ref)} for facts to answer the caller.")[:600],
                               "mock": {"results": []}})]
            report.note(f"tool {ref}: a data store; it now searches the local knowledge base “{base}”. Create it on the "
                        "Knowledge page and upload the same documents.")
        elif doc.get("googleSearchTool"):
            report.note(f"tool {ref}: Google Search tools aren't supported; add the pages you need to a knowledge base instead.")
        elif doc:
            kind = next((k for k in doc if k.endswith(("Tool", "Function"))), "unknown")
            report.note(f"tool {ref}: {kind} tools aren't supported yet.")
        else:
            names = [add_tool({"name": snake(ref, "tool"), "description": human(ref), "method": "POST", "url": "",
                               "parameters": [], "mock": {"note": "replace with the real answer"}})]
            report.note(f"tool {ref}: isn't in the export; it became a stub with a mock answer.")
        converted[ref] = names
        return names

    def toolset_tools(ts: dict) -> list[str]:
        ref, ids = str(ts.get("toolset") or ""), set(ts.get("toolIds") or [])
        key = f"toolset:{ref}:{','.join(sorted(ids))}"
        if key in converted:
            return converted[key]
        doc = toolsets.get(ref) or {}
        names: list[str] = []
        spec = doc.get("openApiToolset")
        if spec:
            for t in openapi_tools(cx.text_field(spec.get("openApiSchema")), snake(ref, "tool"), report, f"toolset {ref}"):
                if not ids or t["name"] in {snake(i, "tool") for i in ids}:
                    names.append(add_tool(t))
        elif doc:
            report.note(f"toolset {ref}: only OpenAPI toolsets are supported so far.")
        else:
            report.note(f"toolset {ref}: isn't in the export.")
        converted[key] = names
        return names

    # ---- Steps ---------------------------------------------------------------------
    step_of: dict[str, str] = {}
    ends: set[str] = set()
    greetings: dict[str, str] = {}
    for aname, a in agents.items():
        if a.get("displayName", aname) != aname and a.get("displayName") in agents:
            continue                                     # folder alias of an agent already added
        raw = cx.text_field(a.get("instruction"))
        m = _GREETING.match(raw.strip())
        if m:
            greetings[aname] = _instruction_text(m.group(1))
            raw = raw.strip()[m.end():]
        raw = _OUR_TRANSFERS.sub("", raw)
        raw = _OUR_TOOLS.sub("", raw)
        desc = a.get("description") if isinstance(a.get("description"), str) else ""
        prompt = _instruction_text(raw).strip() or (desc or f"Help the caller as {human(aname)}.")
        data: dict[str, Any] = {"prompt": prompt, "add_global_prompt": True, "allow_interrupt": True}
        mine: list[str] = []
        for t in a.get("tools") or []:
            t = str(t)
            if t == "end_session":
                ends.add(aname)
            elif t not in RESERVED:
                mine += tool_for(t)
        for ts in a.get("toolsets") or []:
            if isinstance(ts, dict):
                mine += toolset_tools(ts)
        if mine:
            data["tools"] = list(dict.fromkeys(mine))
        cbs = [c for c in CALLBACKS if a.get(c)]
        if cbs:
            report.note(f"agent {aname}: has {', '.join(human(c) for c in cbs)} (Python). They aren't run here; "
                        "fold what they do into the instructions or a tool.")
        if a.get("guardrails"):
            report.note(f"agent {aname}: guardrails {', '.join(map(str, a['guardrails']))} weren't carried over; add them as instructions.")
        if a.get("remoteDialogflowAgent") or a.get("remoteA2aAgent"):
            report.note(f"agent {aname}: calls a remote agent, which isn't supported; its step has only the instructions.")
        if a.get("modelSettings", {}).get("model"):
            report.note(f"agent {aname}: used model {a['modelSettings']['model']}; here every step uses the model set in Settings.")
        step_of[aname] = g.add("agentNode", human(aname).capitalize() if "_" in aname else aname, data, key=aname)
    report.counts["agents"] = len(step_of)

    root = str(app.get("rootAgent") or "")
    if root not in step_of:
        parents = {c for a in agents.values() for c in a.get("childAgents") or []}
        root = next((a for a in step_of if a not in parents), next(iter(step_of), ""))
        if not root:
            raise ImportError_("the export has no agents")
        report.note(f"No root agent was set; {root} is used as the root.")

    greeting = greetings.get(root, "")
    start = g.add("startCall", "Start", {"greeting": greeting, "prompt": "Greet the caller.", "add_global_prompt": True,
                                         "allow_interrupt": True})
    g.connect(start, step_of[root], "after greeting", "Right after the greeting." if greeting else "Right after the call connects.",
              after_greeting=True)
    if not greeting:
        report.note("There's no fixed greeting: the root agent opens the call, as in CX Agent Studio. Add one on the Start step if you like.")

    for aname, a in agents.items():
        src = step_of.get(aname)
        if not src:
            continue
        for child in a.get("childAgents") or []:
            dst = step_of.get(str(child))
            if not dst:
                report.note(f"agent {aname}: child agent {child} isn't in the export.")
                continue
            cdoc = agents.get(str(child)) or {}
            when = (cdoc.get("description") or "").strip() or _gist(cx.text_field(cdoc.get("instruction")))
            g.connect(src, dst, human(str(child)), f"The caller needs what {human(str(child))} handles: {when}" if when
                      else f"The caller needs {human(str(child))}.")
            g.connect(dst, src, "something else", "The caller needs help with something this step doesn't cover.")
    if ends:
        bye = g.add("endCall", "End session", {"prompt": "Say goodbye in one short sentence.", "add_global_prompt": True}, key="end")
        for aname in ends:
            g.connect(step_of[aname], bye, "end session", "The conversation is complete and the caller is ready to hang up.")
    else:
        report.note("No agent could end the session; callers end calls by hanging up. Add an End call step if needed.")

    # ---- App-level settings ------------------------------------------------------------
    gi = _instruction_text(cx.text_field(app.get("globalInstruction"))).strip()
    if gi:
        g.add("globalNode", "Global instructions", {"prompt": gi}, key="global")
    context: dict[str, Any] = {}
    for v in app.get("variableDeclarations") or []:
        if not isinstance(v, dict) or not v.get("name"):
            continue
        key = snake(v["name"], "v")
        default = (v.get("schema") or {}).get("default", "")
        if isinstance(default, (dict, list)):
            report.note(f"variable {v['name']}: was structured data; it starts empty here.")
            default = ""
        context[key] = default
    used = set()
    for n in g.nodes:
        used |= set(re.findall(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}", n["data"].get("prompt") or ""))
    for v in sorted(used - set(context)):
        context[v] = ""
    if used - {snake(x.get("name", ""), "v") for x in app.get("variableDeclarations") or [] if isinstance(x, dict)}:
        report.note("Instructions use values the app didn't declare (" + ", ".join(sorted(used - set(
            snake(x.get("name", ""), "v") for x in app.get("variableDeclarations") or [] if isinstance(x, dict)))) +
            "); they start empty.")
    langs = (app.get("languageSettings") or {}).get("supportedLanguageCodes") or []
    if langs:
        report.note(f"The app also supported {', '.join(langs)}; set the voice and language under Integrations.")
    if app.get("guardrails"):
        report.note(f"App guardrails ({', '.join(map(str, app['guardrails']))}) weren't carried over; add their rules to the global instructions.")
    if any(k.startswith("environment.") for k in ex.files):
        report.note("environment.json held per-environment values (URLs, data stores); set the matching tool addresses by hand.")
    for extra, label in (("examples", "examples"), ("evaluations", "evaluations")):
        if any(k.startswith(f"{extra}/") for k in ex.files):
            report.note(f"The app's {label} weren't carried over; write them as `vk` tests.")
    if python_tools:
        report.note(f"{len(python_tools)} tool(s) were Python code in CX Agent Studio ({', '.join(python_tools)}). Code from an export "
                    "is never run here: each answers with a mock until you put its logic behind an HTTP endpoint and set its address and parameters.")
    report.counts["tools"] = len(tools)
    return g.agent(agent_name, context, scrub_tools(tools, report), "imported from CX Agent Studio"), report


def _gist(instruction: str) -> str:
    """First sentence of an instruction, without markup: a fallback for when to transfer."""
    text = re.sub(r"<[^>]+>", " ", instruction or "")
    text = re.sub(r"\s+", " ", text).strip()
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return first[:240]


def _params(schema: dict) -> list[dict]:
    out = []
    req = set(schema.get("required") or [])
    for pname, ps in (schema.get("properties") or {}).items():
        ps = ps or {}
        t = str(ps.get("type") or "STRING").upper()
        out.append({"name": snake(pname, "p"), "type": _TYPES.get(t, "string"),
                    "description": str(ps.get("description") or "")[:300], "required": pname in req})
    return out

