"""Our agent -> a CX Agent Studio app export (zip, JSON format).

    Start's greeting           the root agent's instruction says to open with it
    root agent and steps       agents (instruction.txt each)
    transfers                  child agents (a tree: each agent under the first step that transfers to it);
                               the condition is written into the parent's instruction and the child's description
    transfers to End call      the end_session system tool
    global instructions        global_instruction.txt
    variables, details         variableDeclarations
    HTTP tools                 OpenAPI toolsets, one per service address
    tools with only a mock     Python tools that return the example answer

Import it in CX Agent Studio with Import app (or apps.importApp).
"""

from __future__ import annotations

from typing import Any

from vk.agentfs import Agent

from .common import (ExportError, code_name, Plan, Report, by_origin, dump, openapi_for, python_stub, rule_text, safe_name, single_braces,
                     zip_bytes)

_TYPES = {"string": "STRING", "number": "NUMBER", "integer": "INTEGER", "boolean": "BOOLEAN"}
_FORM_TYPES = {"number": "NUMBER", "integer": "INTEGER", "yes_no": "BOOLEAN"}


def export(agent: Agent) -> tuple[bytes, Report]:
    files, report = build(agent)
    return zip_bytes(files), report


def build(agent: Agent) -> tuple[dict[str, str], Report]:
    import yaml

    report = Report("CX Agent Studio")
    plan = Plan(agent)
    steps = plan.agents()
    if not steps:
        raise ExportError("the agent has no steps to export")
    root = plan.root() or steps[0]
    if plan.root() is None:
        report.note(f"Start didn't hand over to a single step; {root.name} is the root agent.")
    steps.sort(key=lambda s: s.id != root.id)

    taken: set[str] = {"end_session", "transfer_to_agent", "customize_response"}
    names = {s.id: safe_name(s.name, taken, "Agent") for s in steps}
    app_name = safe_name(agent.name, set(), "App")
    files: dict[str, str] = {}
    pre = f"{app_name}/"

    # The tree of child agents: each agent hangs under the first step that transfers to it.
    parent: dict[str, str] = {}
    for s in steps:
        for e in s.out:
            t = plan.target(e)
            if t.type == "agentNode" and t.id != root.id and t.id not in parent and t.id != s.id:
                parent[t.id] = s.id

    # Tools: HTTP tools grouped into toolsets by address; mock-only tools become Python tools.
    used = {t for s in steps for t in step_tools(s)}
    http = [plan.tools[n] for n in sorted(used) if n in plan.tools and (plan.tools[n].get("url") or "").strip()]
    stubs = [plan.tools[n] for n in sorted(used) if n in plan.tools and not (plan.tools[n].get("url") or "").strip()]
    toolset_of: dict[str, str] = {}
    ts_taken: set[str] = set()
    for origin, group in by_origin(http).items():
        ts = safe_name(f"{app_name}_api" if len(by_origin(http)) == 1 else f"{origin.split('://')[-1]}_api", ts_taken, "Toolset")
        spec, _ = openapi_for(group, f"{agent.name} tools", report, f"toolset {ts}")
        files[f"{pre}toolsets/{ts}/{ts}.json"] = dump({
            "displayName": ts, "description": f"HTTP tools exported from {agent.name}.",
            "openApiToolset": {"openApiSchema": f"toolsets/{ts}/open_api_toolset/open_api_schema.yaml"}})
        files[f"{pre}toolsets/{ts}/open_api_toolset/open_api_schema.yaml"] = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)
        for t in group:
            toolset_of[t["name"]] = ts
    for t in stubs:
        tn = code_name(t["name"])
        files[f"{pre}tools/{tn}/{tn}.json"] = dump({
            "displayName": tn, "pythonFunction": {
                "name": tn, "pythonCode": f"tools/{tn}/python_function/python_code.py",
                "description": (t.get("description") or tn)[:1000]}})
        files[f"{pre}tools/{tn}/python_function/python_code.py"] = python_stub(t)
        report.note(f"tool {t['name']}: had no address, so it's a Python tool returning its example answer. Replace it with the real lookup.")
    for n in sorted(used):
        local = (plan.tools.get(n) or {}).get("connector") or ""
        if local == "knowledge.search":
            report.note(f"tool {n}: searches a knowledge base on this computer; there, create a data store with the same "
                        "documents and use a data store tool instead.")
        elif local.startswith("data."):
            report.note(f"tool {n}: reads or writes records kept on this computer; point it at your own database there.")
    for n in sorted(used - set(plan.tools)):
        report.note(f"a step uses tool {n}, which isn't defined; it was left out.")

    # Agents
    variables: dict[str, dict] = {}
    for k, v in (agent.context_variables or {}).items():
        variables[k] = {"name": k, "description": k.replace("_", " "),
                        "schema": {"type": _type_of(v), "default": v if v is not None else ""}}
    for s in steps:
        lines: list[str] = []
        if s.id == root.id and str(plan.start.data.get("greeting") or "").strip():
            lines.append(f"When the conversation starts, greet the caller with: “{single_braces(str(plan.start.data['greeting']).strip())}”")
        if s.prompt:
            lines.append(single_braces(s.prompt))
        if s.scripted:
            report.note(f"step {s.name}: a scripted page; its script and rules became instructions, so CX Agent Studio's "
                        "model follows them rather than reading them word for word.")
            for f in s.data.get("form") or []:
                if isinstance(f, dict) and f.get("name"):
                    variables.setdefault(f["name"], {"name": f["name"], "description": single_braces(str(f.get("ask") or f["name"]))[:500],
                                                     "schema": {"type": _FORM_TYPES.get(f.get("type"), "STRING")}})
        extract = s.data.get("extraction_variables") or [] if s.data.get("extraction_enabled") else []
        if extract:
            lines.append("Collect these details from the caller and remember them:")
            for v in extract:
                lines.append(f"- {v['name']}: {single_braces(v.get('prompt') or v['name'])}")
                variables.setdefault(v["name"], {"name": v["name"], "description": str(v.get("prompt") or v["name"].replace("_", " "))[:500],
                                                 "schema": {"type": _TYPES.get(v.get("type"), "STRING")}})
        tools: list[str] = []
        toolsets: dict[str, list[str]] = {}
        for tname in step_tools(s):
            if tname in toolset_of:
                toolsets.setdefault(toolset_of[tname], []).append(tname)
            elif tname in plan.tools:
                tools.append(tname if tname in toolset_of else code_name(tname))
        uses = [f"{{@TOOL: {toolset_of[t]}_{t}}}" if t in toolset_of else f"{{@TOOL: {code_name(t)}}}"
                for t in step_tools(s) if t in plan.tools]
        if uses:
            lines.append("Tools you can use: " + ", ".join(uses) + ".")
        rules: list[str] = []
        children: list[str] = []
        for e in s.out:
            t, d = plan.target(e), e.get("data") or {}
            cond = single_braces(rule_text(e, s))
            say = single_braces(str(d.get("transition_speech") or "").strip())
            say = f" First say: “{say}”" if say else ""
            if t.type == "endCall":
                bye = single_braces(t.prompt) or "Say goodbye."
                rules.append(f"- When {_lc(cond)}:{say} {bye} Then end the conversation with {{@TOOL: end_session}}.")
                if "end_session" not in tools:
                    tools.append("end_session")
            elif t.type == "agentNode" and t.id in names:
                rules.append(f"- When {_lc(cond)}:{say} transfer to {{@AGENT: {names[t.id]}}}.")
                if parent.get(t.id) == s.id:
                    children.append(names[t.id])
        if rules:
            lines.append("Transfers:\n" + "\n".join(rules))
        base = f"agents/{names[s.id]}"
        doc: dict[str, Any] = {"displayName": names[s.id], "instruction": f"{base}/instruction.txt"}
        incoming = next((e for x in steps for e in x.out if e["target"] == s.id and (e.get("data") or {}).get("condition")), None)
        if incoming is not None and s.id != root.id:
            doc["description"] = single_braces(str(incoming["data"]["condition"]))[:500]
        if s.id == root.id and "end_session" not in tools:
            tools.append("end_session")           # CX Agent Studio expects the root agent to be able to end the session
        if tools:
            doc["tools"] = tools
        if toolsets:
            doc["toolsets"] = [{"toolset": k, "toolIds": v} for k, v in toolsets.items()]
        if children:
            doc["childAgents"] = children
        files[f"{pre}{base}/{names[s.id]}.json"] = dump(doc)
        files[f"{pre}{base}/instruction.txt"] = "\n\n".join(lines).strip() + "\n"
        if s.data.get("add_global_prompt") is False and plan.global_text():
            report.note(f"step {s.name}: skipped the global instructions here; in CX Agent Studio they apply to every agent.")

    back = [(s, plan.target(e)) for s in steps for e in s.out
            if plan.target(e).type == "agentNode" and parent.get(plan.target(e).id) != s.id and plan.target(e).id != s.id]
    if back:
        report.note(f"{len(back)} transfer(s) go back up or across the agent tree (e.g. {back[0][0].name} → {back[0][1].name}); "
                    "they're written as {@AGENT: …} instructions. Check they behave as expected.")

    app: dict[str, Any] = {"name": app_name, "displayName": agent.name[:80] or app_name, "rootAgent": names[root.id]}
    if plan.global_text():
        app["globalInstruction"] = "global_instruction.txt"
        files[f"{pre}global_instruction.txt"] = single_braces(plan.global_text()) + "\n"
    if variables:
        app["variableDeclarations"] = list(variables.values())
    files[f"{pre}app.json"] = dump(app)

    for n in (x for x in plan.steps.values() if x.type in ("trigger", "webhook", "qa")):
        report.note(f"{n.type} {n.name}: not part of a CX Agent Studio app; set it up there separately.")
    for n in (x for x in plan.steps.values() if x.type == "endCall" and x.data.get("transfer_to")):
        report.note(f"step {n.name}: hands the caller to {n.data['transfer_to']}; in CX Agent Studio, set up that "
                    "transfer on your telephony integration (the agent's end_session ends the conversation).")
    if any(str(v.get("pre_call_fetch_url") or "") for v in [plan.start.data]):
        report.note("Start fetched data before the call; recreate that with a before-agent callback or a tool.")
    report.counts.update({"agents": len(steps), "toolsets": len(set(toolset_of.values())), "python tools": len(stubs),
                          "variables": len(variables)})
    return files, report


def step_tools(s) -> list[str]:
    """A step's tools, including the ones a scripted page runs itself."""
    return list(dict.fromkeys([*(s.data.get("tools") or []), *(s.data.get("on_entry") or []), *(s.data.get("on_done") or [])]))


def _type_of(v: Any) -> str:
    if isinstance(v, bool):
        return "BOOLEAN"
    if isinstance(v, int):
        return "INTEGER"
    if isinstance(v, float):
        return "NUMBER"
    return "STRING"


def _lc(text: str) -> str:
    """'The caller wants X.' -> 'the caller wants X' so it reads after 'When'."""
    t = text.rstrip(". ")
    return t[:1].lower() + t[1:] if t[:2] != t[:2].upper() else t
