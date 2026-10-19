"""Our agent -> a Dialogflow CX agent (JSON package zip), built on playbooks.

Our steps are instructions for a model, which is what Dialogflow CX playbooks
are, so each step becomes a playbook:

    root agent (Start's hand-over)   the default playbook (startPlaybook); it opens with Start's greeting
    other steps                      playbooks; transfers become ${PLAYBOOK:name} in the steps
    End call steps                   one "End Conversation" flow that says goodbye and ends the session,
                                     reached with ${FLOW:End Conversation}
    details to collect               output parameters of the playbook
    global instructions              added to every playbook's instructions
    HTTP tools                       OpenAPI tools (schema.yaml beside each), one per service address
    tools with only a mock           function tools (your client or a webhook answers them)
    scripted pages                   flows and pages: the script is the entry fulfillment, form fields are
                                     form parameters (choices become entity types), condition transfers
                                     become condition routes, "no match" a sys.no-match-N event handler,
                                     and transfers the model decides become intents

Restore it into an existing agent (Agent settings → Restore, or agents.restore),
then run Validate. Its default language must match the agent's (en).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from vk.agentfs import Agent

from vk.tools import TEMPLATE

from vk import pages

from .common import ExportError, Plan, Report, Step, by_origin, dump, openapi_for, rule_text, safe_name, zip_bytes


def single_braces(text: str) -> str:
    """{{name}} -> $session.params.name (how Dialogflow CX refers to session parameters)."""
    return TEMPLATE.sub(lambda m: "$session.params." + m.group(1), text or "")

ZERO = "00000000-0000-0000-0000-000000000000"
NS = uuid.UUID("6f1c2d40-1f5e-4c8e-9a51-0d6b2c9e7a10")       # stable ids for the same names
_TYPES = {"string": "STRING", "number": "NUMBER", "integer": "NUMBER", "boolean": "BOOLEAN"}
END_FLOW = "End Conversation"
START_FLOW = "Default Start Flow"
FINAL = '$page.params.status = "FINAL"'
_ENTITY = {"text": "@sys.any", "number": "@sys.number", "integer": "@sys.number-integer", "date": "@sys.date",
           "time": "@sys.time", "digits": "@sys.number-sequence", "phone": "@sys.phone-number", "email": "@sys.email"}
_YES = ["yes", "yeah", "yep", "sure", "correct", "right", "that's right"]
_NO = ["no", "nope", "nah", "not really", "wrong"]


def export(agent: Agent, *, time_zone: str = "America/New_York", language: str = "en") -> tuple[bytes, Report]:
    files, report = build(agent, time_zone=time_zone, language=language)
    return zip_bytes(files), report


def _id(kind: str, name: str) -> str:
    return str(uuid.uuid5(NS, f"{kind}/{name}"))


def _msg(text: str | list[str], lang: str) -> dict:
    texts = [t for t in ([text] if isinstance(text, str) else text) if t]
    return {"messages": [{"text": {"text": [t]}, "languageCode": lang} for t in texts]}


def _fname(name: str) -> str:
    """File names keep spaces; characters a file name can't hold are percent-encoded, as Dialogflow does."""
    return "".join(c if c.isalnum() or c in " ._-()" else f"%{ord(c):02x}" for c in name)


def build(agent: Agent, *, time_zone: str = "America/New_York", language: str = "en") -> tuple[dict[str, str], Report]:
    import yaml

    report = Report("Dialogflow CX")
    plan = Plan(agent)
    steps = plan.agents()
    if not steps:
        raise ExportError("the agent has no steps to export")
    root = plan.root() or steps[0]
    steps.sort(key=lambda s: s.id != root.id)
    scripted = [s for s in steps if s.scripted]
    steps = [s for s in steps if not s.scripted]           # the rest are playbooks
    taken: set[str] = {END_FLOW.lower()}
    names = {s.id: s.name.strip()[:60] or f"Playbook {i + 1}" for i, s in enumerate(steps)}
    seen: set[str] = set()
    for sid, n in names.items():                    # display names must be unique
        base, k = n, 2
        while n.lower() in seen | taken:
            n, k = f"{base} {k}", k + 1
        seen.add(n.lower())
        names[sid] = n
    files: dict[str, str] = {}
    lang = language

    start_by = {"startFlow": START_FLOW} if root.scripted else {"startPlaybook": names[root.id]}
    files["agent.json"] = dump({"defaultLanguageCode": lang, "supportedLanguageCodes": [], "timeZone": time_zone,
                                **start_by, "enableSpellCorrection": True,
                                "genAppBuilderSettings": {}, "advancedSettings": {"loggingSettings": {}}})
    report.note(f"Restore into an agent whose default language is {lang} and time zone {time_zone} (change with "
                "--language / --time-zone), then press Validate.")

    # Default intents every Dialogflow CX agent carries.
    files["intents/Default Welcome Intent/Default Welcome Intent.json"] = dump(
        {"name": ZERO, "displayName": "Default Welcome Intent", "priority": 500000})
    files["intents/Default Welcome Intent/trainingPhrases/en.json"] = dump({"trainingPhrases": [
        {"parts": [{"text": p}], "repeatCount": 1, "languageCode": "en"} for p in ("hi", "hello", "good morning")]})
    files["intents/Default Negative Intent/Default Negative Intent.json"] = dump(
        {"name": "00000000-0000-0000-0000-000000000001", "displayName": "Default Negative Intent", "priority": 500000,
         "isFallback": True})

    # Scripted pages: flows and pages. The start flow is required even when a playbook starts the conversation.
    flows = _Flows(plan, scripted, names, root, lang, report)
    files.update(flows.files())

    # Ending the conversation: playbooks hand over to this flow.
    ends = [plan.target(e) for s in steps + scripted for e in s.out
            if plan.target(e).type == "endCall" and not plan.target(e).scripted]
    if ends:
        goodbye = single_braces(ends[0].prompt) or "Thank you for calling. Goodbye."
        if any(single_braces(x.prompt) != goodbye for x in ends[1:]):
            report.note("Several End call steps became one End Conversation flow; their goodbyes are in the playbooks' steps.")
        files[f"flows/{END_FLOW}/{END_FLOW}.json"] = dump({
            "name": _id("flow", END_FLOW), "displayName": END_FLOW,
            "transitionRoutes": [{"condition": "true", "targetPage": "End Session",
                                  "triggerFulfillment": _msg("Thank you. Goodbye.", lang)}],
            "nluSettings": {"modelType": "MODEL_TYPE_STANDARD", "classificationThreshold": 0.3}})

    # Tools
    used = {t for s in steps for t in s.data.get("tools") or []}
    http = [plan.tools[n] for n in sorted(used) if n in plan.tools and (plan.tools[n].get("url") or "").strip()
            and "connector" not in plan.tools[n] and "mcp" not in plan.tools[n]]
    stubs = [plan.tools[n] for n in sorted(used) if n in plan.tools and n not in {t["name"] for t in http}]
    tool_of: dict[str, str] = {}                     # our tool -> Dialogflow tool display name
    ts_taken: set[str] = set()
    for origin, group in by_origin(http).items():
        tname = safe_name(f"{agent.name} API" if len(by_origin(http)) == 1 else f"{origin.split('://')[-1]} API", ts_taken, "API")
        spec, _ = openapi_for(group, f"{agent.name} tools", report, f"tool {tname}")
        files[f"tools/{_fname(tname)}/{_fname(tname)}.json"] = dump({
            "name": _id("tool", tname), "displayName": tname, "description": f"HTTP tools from {agent.name}.",
            "openApiSpec": {}})
        files[f"tools/{_fname(tname)}/schema.yaml"] = yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)
        for t in group:
            tool_of[t["name"]] = tname
    for t in stubs:
        kind = "connector" if "connector" in t else "mcp" if "mcp" in t else "mock"
        props = {p["name"]: {"type": (p.get("type") or "string").replace("integer", "number"),
                             **({"description": p["description"]} if p.get("description") else {})}
                 for p in t.get("parameters") or []}
        files[f"tools/{_fname(t['name'])}/{_fname(t['name'])}.json"] = dump({
            "name": _id("tool", t["name"]), "displayName": t["name"], "description": (t.get("description") or t["name"])[:500],
            "functionSpec": {"inputSchema": {"type": "object", "properties": props,
                                             "required": [p["name"] for p in t.get("parameters") or [] if p.get("required")]},
                             "outputSchema": {"type": "object"}}})
        tool_of[t["name"]] = t["name"]
        report.note(f"tool {t['name']}: became a function tool ({'a ' + kind + ' tool here' if kind != 'mock' else 'it had only a mock answer'}); "
                    "answer it from your client or a webhook.")

    for n in sorted(used):
        local = (plan.tools.get(n) or {}).get("connector") or ""
        if local == "knowledge.search":
            report.note(f"tool {n}: searches a knowledge base on this computer; there, create a data store with the same "
                        "documents and use a data store tool instead.")
        elif local.startswith("data."):
            report.note(f"tool {n}: reads or writes records kept on this computer; point it at your own database there.")
    # Playbooks
    shared = single_braces(plan.global_text())
    for s in steps:
        out: list[dict] = []
        if s.id == root.id and str(plan.start.data.get("greeting") or "").strip():
            out.append({"text": f"Start by greeting the user with: “{single_braces(str(plan.start.data['greeting']).strip())}”"})
        for line in [x.strip() for x in single_braces(s.prompt).split("\n") if x.strip()]:
            out.append({"text": line.lstrip("-• ").strip()})
        if shared and s.data.get("add_global_prompt", True):
            out.append({"text": "Always follow these rules:", "steps": [{"text": x.strip().lstrip("-• ")}
                                                                       for x in shared.split("\n") if x.strip()]})
        mine = [tool_of[t] for t in s.data.get("tools") or [] if t in tool_of]
        if mine:
            out.append({"text": "Use " + ", ".join(f"${{TOOL:{x}}}" for x in dict.fromkeys(mine)) + " when you need what it returns."})
        for e in s.out:
            t, d = plan.target(e), e.get("data") or {}
            cond = single_braces(rule_text(e, s)).rstrip(".")
            say = single_braces(str(d.get("transition_speech") or "").strip())
            first = f"say “{say}”, then " if say else ""
            if t.type == "endCall" and t.scripted:
                out.append({"text": f"If {cond[:1].lower() + cond[1:]}: {first}go to ${{FLOW:{flows.end_flow(t)}}}."})
            elif t.type == "endCall":
                out.append({"text": f"If {cond[:1].lower() + cond[1:]}: {first}{single_braces(t.prompt) or 'say goodbye'} "
                                    f"Then go to ${{FLOW:{END_FLOW}}}."})
            elif t.scripted:
                out.append({"text": f"If {cond[:1].lower() + cond[1:]}: {first}hand over to ${{FLOW:{flows.enter(t, s)}}}."})
            elif t.type == "agentNode" and t.id in names:
                out.append({"text": f"If {cond[:1].lower() + cond[1:]}: {first}hand over to ${{PLAYBOOK:{names[t.id]}}}."})
        extract = s.data.get("extraction_variables") or [] if s.data.get("extraction_enabled") else []
        doc: dict[str, Any] = {
            "name": ZERO if s.id == root.id else _id("playbook", names[s.id]),
            "displayName": names[s.id],
            "goal": _goal(s, plan),
            "instruction": {"steps": out or [{"text": "Help the user."}]},
            "playbookType": "TASK",
        }
        if mine:
            doc["referencedTools"] = list(dict.fromkeys(mine))
        if extract:
            doc["outputParameterDefinitions"] = [{"name": v["name"], "type": _TYPES.get(v.get("type"), "STRING"),
                                                  "description": single_braces(v.get("prompt") or v["name"])[:300]}
                                                 for v in extract]
            if s.id == root.id:
                report.note(f"{names[s.id]}: the start playbook collects details; Dialogflow keeps them as output parameters.")
        files[f"playbooks/{_fname(names[s.id])}/{_fname(names[s.id])}.json"] = dump(doc)

    if plan.agent.context_variables:
        report.note("Variables (" + ", ".join(plan.agent.context_variables) + ") appear as $session.params.name in the instructions; "
                    "set them as session parameters (for example with a webhook or the API) after restoring.")
    for n in (x for x in plan.steps.values() if x.type in ("trigger", "webhook", "qa")):
        report.note(f"{n.type} {n.name}: not part of a Dialogflow CX agent; set it up there separately.")
    files.update(flows.end_files())
    report.counts.update({"playbooks": len(steps), "tools": len(set(tool_of.values())), "end flow": 1 if ends else 0})
    if scripted:
        report.counts.update({"flows": len(flows.groups), "pages": len(scripted), "intents": len(flows.intents),
                              "entity types": len(flows.entities)})
    return files, report


def _goal(step, plan: Plan) -> str:
    """A playbook's goal: the step's first sentence, or the condition that leads to it."""
    first = step.prompt.split("\n")[0].strip()
    if first and len(first) <= 200:
        return single_braces(first)
    incoming = next((e for s in plan.steps.values() for e in s.out if e["target"] == step.id and (e.get("data") or {}).get("condition")), None)
    return single_braces(str(incoming["data"]["condition"]))[:200] if incoming else f"Handle {step.name.lower()}."


class _Flows:
    """Scripted pages as Dialogflow CX flows and pages.

    Pages are grouped by their flow; a page without one joins the flow of the pages that lead to it, or gets
    a flow of its own. Another flow (or a playbook) can only enter a flow at its start, so each flow starts
    at the page entered from outside it.
    """

    def __init__(self, plan: Plan, scripted: list[Step], playbooks: dict[str, str], root: Step, lang: str, report: Report):
        self.plan, self.lang, self.report, self.playbooks = plan, lang, report, playbooks
        self.pages = {s.id: s for s in scripted}
        self.root = root
        self.intents: dict[str, dict] = {}            # display name -> {"phrases", "description"}
        self.entities: dict[str, dict] = {}           # display name -> {"kind", "entities"}
        self.webhooks: dict[str, str] = {}            # tool a page runs -> its address (webhooks/)
        self._ends: dict[str, str] = {}               # scripted End call step -> its flow
        self._end_docs: dict[str, dict] = {}
        incoming: dict[str, list[Step]] = {}
        for st in plan.steps.values():
            for e in st.out:
                incoming.setdefault(e["target"], []).append(st)
        self.incoming = incoming
        flow_of: dict[str, str] = {s.id: str(s.data.get("flow") or "").strip() for s in scripted if str(s.data.get("flow") or "").strip()}
        for s in scripted:                            # graph order, so a page usually follows the pages before it
            if s.id in flow_of:
                continue
            srcs = incoming.get(s.id, [])
            joined = {flow_of[x.id] for x in srcs if x.id in flow_of}
            if srcs and len(joined) == 1 and all(x.scripted for x in srcs):
                flow_of[s.id] = joined.pop()
            else:
                flow_of[s.id] = s.name
        # Another flow can only enter a flow at its start: a page entered from outside its flow, when the flow
        # already starts elsewhere, starts a flow of its own (and so may the pages it leads to).
        while True:
            moved = False
            for f in dict.fromkeys(flow_of.values()):
                members = [s for s in scripted if flow_of[s.id] == f]
                outside = [s for s in members if s.id == root.id or any(flow_of.get(x.id) != f for x in incoming.get(s.id, []))]
                outside.sort(key=lambda s: s.id != root.id)
                for s in outside[1:]:
                    flow_of[s.id] = f"{f} / {s.name}"
                    moved = True
            if not moved:
                break
        taken = {END_FLOW.lower()}
        rename: dict[str, str] = {}
        for f in dict.fromkeys(flow_of.values()):
            if f == START_FLOW and root.scripted and flow_of.get(root.id) == START_FLOW:
                rename[f] = f                        # an imported start flow keeps its place
            else:
                rename[f] = _unique(f if f != START_FLOW else "Start flow pages", taken | {START_FLOW.lower()}, "Flow")
            taken.add(rename[f].lower())
        split = [f for f in rename if " / " in f]
        if split:
            report.note(f"{len(split)} page(s) are entered from outside their flow, which Dialogflow only allows at a flow's start, "
                        f"so each starts a flow of its own (e.g. {rename[split[0]]}).")
        self.flow_names = taken
        self.flow_of = {sid: rename[f] for sid, f in flow_of.items()}
        self.groups: dict[str, list[Step]] = {}
        for s in scripted:
            self.groups.setdefault(self.flow_of[s.id], []).append(s)
        self.page_name: dict[str, str] = {}
        for f, members in self.groups.items():
            used = {"end session", "end flow", "start", "current page", "previous page"}
            for s in members:
                self.page_name[s.id] = _unique(s.name, used, "Page")
                used.add(self.page_name[s.id].lower())
        self.entry: dict[str, str] = {}
        for f, members in self.groups.items():
            outside = [s for s in members if s.id == root.id or any(self.flow_of.get(x.id) != f for x in incoming.get(s.id, []))]
            self.entry[f] = (outside or members)[0].id

    # -- references --------------------------------------------------------------------
    def enter(self, page: Step, src: Step | None = None) -> str:
        """The flow to go to for this page (from a playbook or another flow)."""
        return self.flow_of[page.id]

    def end_flow(self, step: Step) -> str:
        """A scripted End call step reached from a playbook: a small flow that says its line and ends the session."""
        if step.id not in self._ends:
            name = _unique(step.name, self.flow_names, "Goodbye")
            self.flow_names.add(name.lower())
            self._ends[step.id] = name
            say = single_braces(str(step.data.get("say") or "").strip())
            route: dict[str, Any] = {"condition": "true", "targetPage": "END_SESSION"}
            if say:
                route["triggerFulfillment"] = _msg(say, self.lang)
            self._end_docs[name] = {"name": _id("flow", name), "displayName": name, "transitionRoutes": [route],
                                    "nluSettings": {"modelType": "MODEL_TYPE_STANDARD", "classificationThreshold": 0.3}}
        return self._ends[step.id]

    def target(self, src: Step, t: Step, texts: list[str]) -> dict | None:
        """Route target fields for a transfer from scripted page src to t; may add lines to say."""
        if t.scripted and t.type == "agentNode":
            f = self.flow_of[t.id]
            if f == self.flow_of[src.id]:
                return {"targetPage": self.page_name[t.id]}
            return {"targetFlow": f}
        if t.type == "agentNode" and t.id in self.playbooks:
            return {"targetPlaybook": self.playbooks[t.id]}
        if t.type == "endCall" and t.scripted:
            say = single_braces(str(t.data.get("say") or "").strip())
            if say:
                texts.append(say)
            return {"targetPage": "END_SESSION"}
        if t.type == "endCall":
            return {"targetFlow": END_FLOW}
        return None

    # -- building ------------------------------------------------------------------------
    def _entity(self, field: dict, where: str) -> str:
        kind, name = field.get("type") or "text", str(field.get("name"))
        if kind == "choice":
            ents = [{"value": v, "synonyms": syn} for v, syn in pages.choice_options(field.get("choices"))]
            return "@" + self._add_entity(name.replace("_", "-"), "KIND_MAP", ents)
        if kind == "yes_no":
            return "@" + self._add_entity("yes-no", "KIND_MAP", [{"value": "true", "synonyms": ["true"] + _YES},
                                                                  {"value": "false", "synonyms": ["false"] + _NO}])
        if kind == "text" and field.get("pattern"):
            pat = str(field["pattern"])
            return "@" + self._add_entity(f"{name.replace('_', '-')}-pattern", "KIND_REGEXP", [{"value": pat, "synonyms": [pat]}])
        if kind in ("number", "integer") and (field.get("min") is not None or field.get("max") is not None):
            self.report.note(f"{where}: field {name} must be between {field.get('min', '…')} and {field.get('max', '…')} here; "
                             "Dialogflow forms don't check ranges, so add a condition route or webhook if it matters.")
        if field.get("min_length") or field.get("max_length"):
            if not field.get("keypad"):
                self.report.note(f"{where}: field {name}'s length limits weren't carried over.")
        return _ENTITY.get(kind, "@sys.any")

    def _add_entity(self, name: str, kind: str, ents: list[dict]) -> str:
        base, k = name, 2
        while name in self.entities and self.entities[name]["entities"] != ents:
            name, k = f"{base}-{k}", k + 1
        self.entities[name] = {"kind": kind, "entities": ents}
        return name

    def _intent(self, edge: dict) -> str:
        d = edge.get("data") or {}
        label = str(d.get("label") or "route").strip()
        cond = str(d.get("condition") or label).strip()
        phrases = re.findall(r"“([^”]+)”", cond) or [label]
        base = re.sub(r"[^a-z0-9]+", ".", label.lower()).strip(".") or "route"
        name, k = base, 2
        while name in self.intents and self.intents[name]["description"] != cond[:140]:
            name, k = f"{base}.{k}", k + 1
        if name not in self.intents:
            self.intents[name] = {"phrases": phrases, "description": cond[:140]}
            if phrases == [label]:
                self.report.note(f"intent {name}: made from the transfer “{label}”; add training phrases in Dialogflow.")
        return name

    def _page(self, s: Step) -> dict:
        d, where = s.data, f"page {s.name}"
        doc: dict[str, Any] = {"name": _id("page", f"{self.flow_of[s.id]}/{self.page_name[s.id]}"), "displayName": self.page_name[s.id]}
        say = single_braces(str(d.get("say") or "").strip())
        if say:
            doc["entryFulfillment"] = _msg(say, self.lang)
        form = [f for f in d.get("form") or [] if isinstance(f, dict) and f.get("name")]
        params = []
        for f in form:
            prm: dict[str, Any] = {"displayName": f["name"], "entityType": self._entity(f, where),
                                   "required": f.get("required", True) is not False,
                                   "fillBehavior": {"initialPromptFulfillment": _msg(single_braces(str(f.get("ask") or "")), self.lang)}}
            if f.get("reprompt"):
                prm["fillBehavior"]["repromptEventHandlers"] = [
                    {"event": "sys.no-match-default", "triggerFulfillment": _msg(single_braces(str(f["reprompt"])), self.lang)}]
            if f.get("keypad"):
                dtmf: dict[str, Any] = {"enabled": True, "finishDigit": "#"}
                if f.get("max_length"):
                    dtmf["maxDigits"] = int(f["max_length"])
                prm["advancedSettings"] = {"dtmfSettings": dtmf}
            if f.get("confirm"):
                self.report.note(f"{where}: field {f['name']} reads the answer back to confirm here; Dialogflow forms don't, "
                                 "so add a confirmation page if you need it.")
            params.append(prm)
        if params:
            doc["form"] = {"parameters": params}
        done = single_braces(str(d.get("done_say") or "").strip())
        routes, intents, handlers = [], [], []
        for e in s.out:
            t, ed = self.plan.target(e), e.get("data") or {}
            kind = pages.route_kind(ed)
            texts = [x for x in [done if kind in ("when", "otherwise") else "", single_braces(str(ed.get("transition_speech") or "").strip())] if x]
            dest = self.target(s, t, texts)
            if dest is None:
                continue
            r: dict[str, Any] = dict(dest)
            if str(ed.get("label") or "").strip():
                r["description"] = str(ed["label"]).strip()[:500]
            if texts:
                r["triggerFulfillment"] = _msg(texts, self.lang)
            if kind == "when":
                try:
                    cond = pages.to_dialogflow(ed["when"])
                except pages.ExpressionError:
                    self.report.note(f"{where}: the condition {ed['when']} couldn't be written for Dialogflow and was left out.")
                    continue
                cond = self._yes_no(cond, form)
                r["condition"] = f"{FINAL} AND {_group(cond)}" if params else cond
                routes.append(r)
            elif kind == "otherwise":
                r["condition"] = FINAL if params else "true"
                routes.append(("last", r))
            elif kind == "no_match":
                tries = int(d.get("max_attempts") or 3)
                if tries > 6:
                    self.report.note(f"{where}: Dialogflow counts up to 6 misunderstood answers; “no match” now happens after 6, not {tries}.")
                handlers.append({"event": f"sys.no-match-{min(tries, 6)}", **r})
            else:
                intents.append({"intent": self._intent(e), **r})
        ordered = [r for r in routes if not isinstance(r, tuple)] + [r[1] for r in routes if isinstance(r, tuple)]
        if done and not ordered:
            ordered.append({"condition": FINAL if params else "true", "triggerFulfillment": _msg(done, self.lang)})
        entry_hooks, done_hooks = self._hooks(d.get("on_entry"), where), self._hooks(d.get("on_done"), where)
        if entry_hooks:
            doc.setdefault("entryFulfillment", {})["webhook"] = entry_hooks[0]
        if done_hooks:
            if not ordered:
                ordered.append({"condition": FINAL if params else "true"})
            for r in ordered:                          # Dialogflow calls it as the page moves on
                r.setdefault("triggerFulfillment", {})["webhook"] = done_hooks[0]
            self.report.note(f"{where}: its “when the form is complete” tool runs as a webhook on the way out in Dialogflow, "
                             "so its answer can't change which route is taken there.")
        doc["transitionRoutes"] = ordered + intents
        if str(d.get("no_match_say") or "").strip():
            handlers.append({"event": "sys.no-match-default", "triggerFulfillment": _msg(single_braces(d["no_match_say"].strip()), self.lang)})
        if handlers:
            doc["eventHandlers"] = handlers
        return doc

    def _hooks(self, names: Any, where: str) -> list[str]:
        names = [n for n in names or [] if isinstance(n, str)]
        if len(names) > 1:
            self.report.note(f"{where}: Dialogflow calls one webhook at a time; only {names[0]} was kept of {', '.join(names)}.")
        for n in names[:1]:
            tool = self.plan.tools.get(n) or {}
            url = str(tool.get("url") or "") if "{{" not in str(tool.get("url") or "") else ""
            self.webhooks[n] = url
        return names[:1]

    @staticmethod
    def _yes_no(cond: str, form: list[dict]) -> str:
        """Yes/no answers are the values "true" and "false" of the yes-no entity type in Dialogflow."""
        for f in form:
            if f.get("type") == "yes_no":
                cond = re.sub(rf"(\$session\.params\.{re.escape(f['name'])}\s*!?=\s*)(true|false)\b", r'\1"\2"', cond)
        return cond

    def files(self) -> dict[str, str]:
        out: dict[str, str] = {}
        nlu = {"modelType": "MODEL_TYPE_STANDARD", "classificationThreshold": 0.3}
        defaults = [{"event": "sys.no-match-default", "triggerFulfillment": _msg("Sorry, could you say that again?", self.lang)},
                    {"event": "sys.no-input-default", "triggerFulfillment": _msg("Are you still there?", self.lang)}]
        greeting = single_braces(str(self.plan.start.data.get("greeting") or "").strip())
        welcome: dict[str, Any] = {"intent": "Default Welcome Intent", "triggerFulfillment": _msg(greeting or "Hello! How can I help?", self.lang)}
        start_doc: dict[str, Any] = {"name": ZERO, "displayName": START_FLOW, "transitionRoutes": [welcome],
                                     "eventHandlers": defaults, "nluSettings": nlu}
        if self.root.scripted:
            rf = self.flow_of[self.root.id]
            if rf == START_FLOW:
                welcome["targetPage"] = self.page_name[self.root.id]
            else:
                welcome["targetFlow"] = rf
        for f, members in self.groups.items():
            folder = f"flows/{_fname(f)}"
            entry = {"condition": "true", "targetPage": self.page_name[self.entry[f]]}
            if f == START_FLOW:
                doc = start_doc
                doc["transitionRoutes"].append(entry)
            else:
                doc = {"name": _id("flow", f), "displayName": f, "transitionRoutes": [entry], "nluSettings": nlu}
                out[f"{folder}/{_fname(f)}.json"] = dump(doc)
            for s in members:
                out[f"{folder}/pages/{_fname(self.page_name[s.id])}.json"] = dump(self._page(s))
        out[f"flows/{START_FLOW}/{START_FLOW}.json"] = dump(start_doc)
        for name, it in self.intents.items():
            out[f"intents/{_fname(name)}/{_fname(name)}.json"] = dump(
                {"name": _id("intent", name), "displayName": name, "priority": 500000, "description": it["description"]})
            out[f"intents/{_fname(name)}/trainingPhrases/{self.lang}.json"] = dump({"trainingPhrases": [
                {"parts": [{"text": ph}], "repeatCount": 1, "languageCode": self.lang} for ph in it["phrases"]]})
        for name, ent in self.entities.items():
            out[f"entityTypes/{_fname(name)}/{_fname(name)}.json"] = dump(
                {"name": _id("entity", name), "displayName": name, "kind": ent["kind"], "enableFuzzyExtraction": ent["kind"] == "KIND_MAP"})
            out[f"entityTypes/{_fname(name)}/entities/{self.lang}.json"] = dump({"entities": ent["entities"]})
        for name, url in self.webhooks.items():
            out[f"webhooks/{_fname(name)}.json"] = dump({
                "name": _id("webhook", name), "displayName": name, "timeout": "5s",
                "genericWebService": {"uri": url or f"https://replace-me.example/{name}"}})
            self.report.note(f"webhook {name}: pages call it; Dialogflow sends its own webhook request format, so put an "
                             "adapter in front of the tool's service" + ("" if url else " and set its address") + ".")
        if any(s.data.get("interpret") == "exact" for s in self.pages.values()):
            self.report.note("Pages set to read answers exactly use Dialogflow's own understanding of answers there.")
        return out

    def end_files(self) -> dict[str, str]:
        return {f"flows/{_fname(n)}/{_fname(n)}.json": dump(doc) for n, doc in self._end_docs.items()}


def _group(cond: str) -> str:
    return f"({cond})" if " OR " in cond else cond


def _unique(name: str, taken: set[str], fallback: str) -> str:
    """A display name Dialogflow accepts (spaces are fine), unique ignoring case."""
    base = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f]", " ", name)).strip()[:60] or fallback
    out, k = base, 2
    while out.lower() in taken:
        out, k = f"{base} {k}", k + 1
    return out
