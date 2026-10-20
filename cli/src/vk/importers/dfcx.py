"""Dialogflow CX agent (exported as a JSON package) -> our agent.

Dialogflow CX is state-machine first; our engine is LLM-first. The mapping:

    start flow           Start (greeting from the welcome route) -> "<flow>" step with the flow's routes
    other flows          a "<flow>" step holding the flow's routes (its start page)
    pages                one step each: says the entry fulfillment, asks for form parameters
    form parameters      details to collect when leaving the step
    intent routes        transfers; the condition is written from the intent and its training phrases
    condition routes     transfers whose condition quotes the original expression (review these)
    route groups         their routes are added to every flow or page that uses them
    route fulfillment    "say while transferring"
    END_SESSION          an End call step
    playbooks            steps with the playbook's goal and instructions; referenced playbooks become transfers
    OpenAPI tools        HTTP tools (one per operation)
    webhooks             tool stubs with mock answers (Dialogflow's webhook protocol differs from ours)

Event handlers (no-match, no-input), generators, data stores and other
languages aren't carried over yet; the report lists them.
"""

from __future__ import annotations

import re
from typing import Any

from .. import pages
from ..lint import CLOCK_VARIABLES, SYSTEM_VARIABLES
from .common import scrub_tools, Export, GraphBuilder, ImportError_, Report, human, openapi_tools, snake

SPECIAL = {"END_SESSION": "end", "END_FLOW": "end_flow", "END_FLOW_WITH_CANCELLATION": "end_flow",
           "END_FLOW_WITH_FAILURE": "end_flow", "END_FLOW_WITH_HUMAN_ESCALATION": "escalate",
           "CURRENT_PAGE": "current", "PREVIOUS_PAGE": "previous", "START_PAGE": "start"}
_FIELD_TYPES = {"@sys.number": "number", "@sys.number-integer": "integer", "@sys.cardinal": "integer", "@sys.ordinal": "integer",
                "@sys.number-sequence": "digits", "@sys.date": "date", "@sys.date-time": "date", "@sys.time": "time",
                "@sys.phone-number": "phone", "@sys.email": "email", "@sys.any": "text", "@sys.person": "text",
                "@sys.given-name": "text", "@sys.last-name": "text", "@sys.address": "text", "@sys.zip-code": "digits"}
_SYS_TYPES = {"@sys.number": "number", "@sys.number-integer": "number", "@sys.number-sequence": "string", "@sys.cardinal": "number",
              "@sys.ordinal": "number", "@sys.percentage": "number", "@sys.unit-currency": "string", "@sys.boolean": "boolean"}


def _ref(value: Any) -> str:
    """References are display names in a JSON package; full resource names end with the id or name."""
    s = str(value or "").strip()
    return s.rsplit("/", 1)[-1] if "/" in s and s.startswith("projects/") else s


def _special(value: Any) -> str | None:
    s = re.sub(r"[\s\-]+", "_", _ref(value).strip().upper())   # exports mix "End Flow" and "END_FLOW"
    return SPECIAL.get(s)


def _texts(fulfillment: Any, lang: str) -> list[str]:
    out: list[str] = []
    if not isinstance(fulfillment, dict):
        return out
    for m in fulfillment.get("messages") or []:
        if not isinstance(m, dict):
            continue
        if m.get("languageCode") and lang and m["languageCode"].split("-")[0] != lang.split("-")[0]:
            continue
        txt = (m.get("text") or {}).get("text") or []
        if txt:
            out.append(str(txt[0]))                      # several variants: Dialogflow picks one; we keep the first
        elif "outputAudioText" in m:
            out.append(str((m["outputAudioText"] or {}).get("text") or (m["outputAudioText"] or {}).get("ssml") or ""))
    for cases in (fulfillment.get("conditionalCases") or []):
        if cases:
            out.append("(Some replies here depend on conditions; review them.)")
            break
    return [_params(t).strip() for t in out if t.strip()]


def _transfer(fulfillment: Any) -> str:
    """The phone number or SIP address a fulfillment's live agent transfer dials, or ""."""
    if not isinstance(fulfillment, dict):
        return ""
    for m in fulfillment.get("messages") or []:
        call = m.get("telephonyTransferCall") if isinstance(m, dict) else None
        if isinstance(call, dict):
            to = str(call.get("phoneNumber") or call.get("sipUri") or "").strip()
            if to:
                return to if to.startswith(("sip:", "sips:")) else "+" + re.sub(r"\D", "", to)
    return ""


def _params(text: str) -> str:
    """$session.params.x / $flow.x / $page.params.x -> {{x}}; generator output -> a note."""
    text = re.sub(r"\$request\.generative\.[\w-]+", "(the generated answer; see the import report)", text)
    return re.sub(r"\$(?:session\.params|page\.params|flow)\.([A-Za-z][\w-]*)", lambda m: "{{" + snake(m.group(1), "v") + "}}", text)


def _condition_text(cond: str) -> str:
    c = (cond or "").strip()
    if c.lower() == "true":
        return "Right away, once this step's line has been said."
    final = re.compile(r'\$page\.params\.status\s*=\s*"?FINAL"?\s*', re.I)
    if final.fullmatch(c):
        return "All the details this step asks for have been collected."
    if final.match(c):
        rest = re.sub(r"^AND\s+", "", final.sub("", c, count=1).strip(), flags=re.I)
        return f"All the details this step asks for have been collected, and this is true (from Dialogflow, review it): {rest}"
    return f"When this is true (from Dialogflow, review it): {c}"


class _Ctx:
    def __init__(self, ex: Export, report: Report):
        self.ex = ex
        self.report = report
        agent = ex.json("agent.json")
        if not isinstance(agent, dict):
            raise ImportError_("no agent.json found: export the agent as a JSON package (Agent settings → Export, format JSON package)")
        self.agent = agent
        self.lang = agent.get("defaultLanguageCode") or "en"
        self.flows: dict[str, dict] = {}         # display name -> {"doc", "dir"}
        for path in ex.glob("flows/*/*.json"):
            parts = path.split("/")
            if len(parts) == 3 and parts[2] == f"{parts[1]}.json":
                doc = ex.json(path) or {}
                self.flows[doc.get("displayName") or parts[1]] = {"doc": doc, "dir": f"flows/{parts[1]}"}
        self.descriptions: dict[str, str] = {}
        self.intents = self._intents()
        self.synonyms: dict[str, list[tuple[str, list[str]]]] = {}
        self.patterns: dict[str, list[str]] = {}
        self.entities = self._entities()
        self.webhooks = {(ex.json(p) or {}).get("displayName") or p.split("/")[-1][:-5]: ex.json(p) or {} for p in ex.glob("webhooks/*.json")}

    def _intents(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for path in self.ex.glob("intents/*/*.json"):
            parts = path.split("/")
            if len(parts) != 3 or parts[2] != f"{parts[1]}.json":
                continue
            doc = self.ex.json(path) or {}
            name = doc.get("displayName") or parts[1]
            if doc.get("description"):
                self.descriptions[name] = str(doc["description"]).strip()
            phrases = []
            for tp_file in self.ex.glob(f"intents/{parts[1]}/trainingPhrases/*.json"):
                if tp_file.rsplit("/", 1)[-1][:-5].split("-")[0] != self.lang.split("-")[0]:
                    continue
                for tp in (self.ex.json(tp_file) or {}).get("trainingPhrases") or []:
                    phrases.append("".join(str(p.get("text", "")) for p in tp.get("parts") or []).strip())
            for tp in doc.get("trainingPhrases") or []:      # older exports keep them inline
                phrases.append("".join(str(p.get("text", "")) for p in tp.get("parts") or []).strip())
            out[name] = [p for p in dict.fromkeys(phrases) if p]
        return out

    def _entities(self) -> dict[str, list[str]]:
        """Entity type -> example values (none for pattern types: a regex isn't something to read out)."""
        out: dict[str, list[str]] = {}
        for path in self.ex.glob("entityTypes/*/*.json"):
            parts = path.split("/")
            if len(parts) != 3 or parts[2] != f"{parts[1]}.json":
                continue
            doc = self.ex.json(path) or {}
            values, entries = [], []
            for ent_file in self.ex.glob(f"entityTypes/{parts[1]}/entities/*.json"):
                if ent_file.rsplit("/", 1)[-1][:-5].split("-")[0] != self.lang.split("-")[0]:
                    continue
                for e in (self.ex.json(ent_file) or {}).get("entities") or []:
                    values.append(str(e.get("value", "")))
                    entries.append(e)
            for e in doc.get("entities") or []:
                values.append(str(e.get("value", "")))
                entries.append(e)
            key = doc.get("displayName") or parts[1]
            if doc.get("kind") == "KIND_REGEXP":
                self.patterns[key] = [v for v in values if v]
                values = []
            else:
                self.synonyms[key] = [(str(e.get("value", "")), [str(x) for x in e.get("synonyms") or [] if str(x) != str(e.get("value", ""))])
                                      for e in entries if e.get("value")]
            out[key] = [v for v in dict.fromkeys(values) if v]
        return out

    def field_for(self, p: dict, where: str) -> dict:
        """A Dialogflow form parameter -> a scripted page's field."""
        name = snake(p.get("displayName") or p.get("id") or "detail", "detail")
        etype = _ref(p.get("entityType"))
        fill = p.get("fillBehavior") or {}
        ask = " ".join(_texts(fill.get("initialPromptFulfillment"), self.lang)) or f"What is your {human(name)}?"
        f: dict[str, Any] = {"name": name, "type": _FIELD_TYPES.get(etype, "text"), "ask": ask}
        key = etype.lstrip("@")
        if etype in _FIELD_TYPES:
            pass
        elif key in self.synonyms and {v for v, _ in self.synonyms[key]} == {"true", "false"}:
            f["type"] = "yes_no"                       # how we export yes/no answers
        elif key in self.synonyms and self.synonyms[key]:
            f["type"] = "choice"
            f["choices"] = [{"value": v, "synonyms": syn} if syn else v for v, syn in self.synonyms[key]]
        elif key in self.patterns and len(self.patterns[key]) == 1:
            f["pattern"] = self.patterns[key][0]
        elif etype.startswith("@sys."):
            self.report.note(f"{where}: {etype} has no exact equivalent; field {name} takes the caller's words as text.")
        if not p.get("required", False):
            f["required"] = False
        for h in fill.get("repromptEventHandlers") or []:
            if str(h.get("event", "")).startswith("sys.no-match"):
                said = " ".join(_texts(h.get("triggerFulfillment"), self.lang))
                if said:
                    f["reprompt"] = said
                    break
        dtmf = (p.get("advancedSettings") or {}).get("dtmfSettings") or {}
        if dtmf.get("enabled"):
            f["keypad"] = True
            if f["type"] == "text":
                f["type"] = "digits"
            if dtmf.get("maxDigits"):
                f["max_length"] = int(dtmf["maxDigits"])
        if p.get("redact"):
            self.report.note(f"parameter {name} is redacted in Dialogflow; check how this agent stores it.")
        return f

    def route_groups(self, flow_dir: str | None) -> dict[str, list[dict]]:
        groups: dict[str, list[dict]] = {}
        paths = self.ex.glob("transitionRouteGroups/*.json") + self.ex.glob("agentTransitionRouteGroups/*.json")
        if flow_dir:
            paths += self.ex.glob(f"{flow_dir}/transitionRouteGroups/*.json")
        for p in paths:
            doc = self.ex.json(p) or {}
            groups[doc.get("displayName") or p.rsplit("/", 1)[-1][:-5]] = doc.get("transitionRoutes") or []
        return groups

    def intent_condition(self, intent: str) -> str:
        described = self.descriptions.get(intent, "")
        if described and not described.endswith("…") and len(described) < 140:
            return described                          # what our exporter wrote: the transfer's own condition
        phrases = self.intents.get(intent, [])[:4]
        said = "; ".join(f"“{p}”" for p in phrases)
        what = human(intent)
        return f"The caller wants: {what}." + (f" They might say {said}." if said else "")

    def param_line(self, p: dict) -> tuple[str, dict]:
        name = p.get("displayName") or p.get("id") or "detail"
        etype = _ref(p.get("entityType"))
        prompt = " ".join(_texts((p.get("fillBehavior") or {}).get("initialPromptFulfillment"), self.lang))
        kind = _SYS_TYPES.get(etype, "string")
        values = self.entities.get(etype.lstrip("@"), []) or self.entities.get(etype, [])
        desc = human(name)
        if values:
            desc += f" (one of: {', '.join(values[:8])}{'…' if len(values) > 8 else ''})"
        elif etype.startswith("@sys."):
            desc += f" ({etype[5:].replace('-', ' ')})"
        req = "required" if p.get("required") else "optional"
        if p.get("redact"):
            self.report.note(f"parameter {name} is redacted in Dialogflow; check how this agent stores it.")
        line = f"- {human(name)} ({req})" + (f": ask “{prompt}”" if prompt else "")
        return line, {"name": snake(name, "detail"), "type": kind, "prompt": desc[:300]}


def convert(source, name: str = "") -> tuple[Any, Report]:
    ex = Export.open(source).rebase("agent.json")
    report = Report("Dialogflow CX")
    cx = _Ctx(ex, report)
    g = GraphBuilder()
    tools: list[dict] = []
    tool_names: set[str] = set()
    agent_name = name or cx.agent.get("displayName") or "Imported agent"

    def add_tool(t: dict) -> str:
        base, n = t["name"], 2
        while t["name"] in tool_names:
            t["name"], n = f"{base}_{n}", n + 1
        tool_names.add(t["name"])
        tools.append(t)
        return t["name"]

    hooks: dict[tuple[str, str], str] = {}

    def webhook_tool(hook: Any, tag: Any) -> str:
        key = (_ref(hook), str(tag or ""))
        if key not in hooks:
            hooks[key] = add_tool(_webhook_stub(cx, hook, tag, report))
        return hooks[key]

    # ---- Steps for flows and pages --------------------------------------------------
    start_flow = _ref(cx.agent.get("startFlow")) or next((f for f in cx.flows if f.lower() == "default start flow"), None)
    if start_flow and start_flow not in cx.flows:
        start_flow = next(iter(cx.flows), None)
    flow_step: dict[str, str] = {}
    page_step: dict[tuple[str, str], str] = {}
    pages_of: dict[str, list[dict]] = {}
    end_flows = {fname for fname, f in cx.flows.items() if fname != start_flow and _goodbye_only(f, ex)}
    for fname, f in cx.flows.items():
        if fname in end_flows:
            pages_of[fname] = []
            continue
        no_match = next((" ".join(_texts(h.get("triggerFulfillment"), cx.lang)) for h in f["doc"].get("eventHandlers") or []
                         if str(h.get("event", "")).startswith("sys.no-match")), "")
        flow_data: dict[str, Any] = {"mode": "scripted", "flow": fname, "form": [], "add_global_prompt": True, "allow_interrupt": True}
        if no_match:
            flow_data["no_match_say"] = no_match
        if f["doc"].get("description"):
            flow_data["description"] = str(f["doc"]["description"])[:500]
        flow_step[fname] = g.add("agentNode", human(fname).capitalize() if fname != start_flow else "Main menu", flow_data,
                                 key=f"flow {fname}")
        pages_of[fname] = []
        for p in ex.glob(f"{f['dir']}/pages/*.json"):
            doc = ex.json(p) or {}
            pages_of[fname].append(doc)
            hook = (doc.get("entryFulfillment") or {}).get("webhook")
            pname = doc.get("displayName") or p.rsplit("/", 1)[-1][:-5]
            said = _texts(doc.get("entryFulfillment"), cx.lang)
            data = {"mode": "scripted", "flow": fname, "add_global_prompt": True, "allow_interrupt": True,
                    "form": [cx.field_for(prm, f"page {pname}") for prm in ((doc.get("form") or {}).get("parameters") or [])]}
            if said:
                data["say"] = " ".join(said)
            if hook:                                  # the page's webhook runs when it begins, as in Dialogflow
                data["on_entry"] = [webhook_tool(hook, (doc.get("entryFulfillment") or {}).get("tag"))]
            if doc.get("description"):
                data["description"] = str(doc["description"])[:500]
            to = _transfer(doc.get("entryFulfillment"))
            if to:                                    # a page that hands the caller to a person ends the call there
                end = {"mode": "scripted", "say": data.get("say", ""), "transfer_to": to, "add_global_prompt": True}
                page_step[(fname, pname)] = g.add("endCall", pname, end, key=f"{fname} {pname}")
                continue
            page_step[(fname, pname)] = g.add("agentNode", pname, data, key=f"{fname} {pname}")
    report.counts["flows"] = len(cx.flows)
    report.counts["pages"] = len(page_step)

    # ---- Start and goodbye -----------------------------------------------------------
    greeting = ""
    if start_flow and not cx.agent.get("startPlaybook"):
        for r in cx.flows[start_flow]["doc"].get("transitionRoutes") or []:
            if "welcome" in _ref(r.get("intent")).lower():
                greeting = " ".join(_texts(r.get("triggerFulfillment"), cx.lang))
                break
    start = g.add("startCall", "Start", {"greeting": greeting, "prompt": "Greet the caller.", "add_global_prompt": True, "allow_interrupt": True})
    end_id: list[str] = []

    def end_step() -> str:
        if not end_id:
            end_id.append(g.add("endCall", "End session", {"prompt": "Say goodbye in one short sentence.", "add_global_prompt": True}, key="end"))
        return end_id[0]

    quiet_id: list[str] = []

    def hang_up() -> str:
        """Dialogflow's END_SESSION from a page: the call ends without another word."""
        if not quiet_id:
            quiet_id.append(g.add("endCall", "Hang up", {"mode": "scripted", "add_global_prompt": True}, key="hang up"))
        return quiet_id[0]

    hand_id: list[str] = []

    def handoff_step() -> str:
        if not hand_id:
            hand_id.append(g.add("endCall", "Hand off to a person", {
                "prompt": "Tell the caller you're passing them to a member of the team, then end the call politely.",
                "add_global_prompt": True}, key="handoff"))
            report.note("Routes that escalated to a human now end the call at “Hand off to a person”. "
                        "Set up a transfer number (Phone page) if you want real hand-offs.")
        return hand_id[0]

    transfer_ids: dict[str, str] = {}

    def transfer_step(to: str) -> str:
        """A live agent transfer on a route: an End call step that hands the caller to that number."""
        if to not in transfer_ids:
            transfer_ids[to] = g.add("endCall", f"Transfer to {to}", {"mode": "scripted", "transfer_to": to, "add_global_prompt": True},
                                     key=f"transfer {to}")
        return transfer_ids[to]

    # ---- Playbooks and tools -----------------------------------------------------------
    tool_docs = {}
    for p in ex.glob("tools/*/*.json") + ex.glob("tools/*.json"):
        doc = ex.json(p) or {}
        if isinstance(doc, dict) and doc.get("displayName"):
            tool_docs[doc["displayName"]] = (doc, p.rsplit("/", 1)[0])
    converted: dict[str, list[str]] = {}

    def tool_for(tname: str) -> list[str]:
        if tname in converted:
            return converted[tname]
        doc, folder = tool_docs.get(tname, ({}, ""))
        names: list[str] = []
        if "openApiSpec" in doc:                         # the spec may sit beside it in schema.yaml
            schema = (doc["openApiSpec"] or {}).get("textSchema") or ""
            if not schema:
                for ext in ("yaml", "yml", "json"):
                    schema = ex.text(f"{folder}/schema.{ext}") or schema
            for t in openapi_tools(schema, snake(tname, "tool"), report, f"tool {tname}"):
                names.append(add_tool(t))
        elif doc.get("dataStoreSpec"):
            base = re.sub(r"[^a-z0-9_-]+", "-", tname.lower()).strip("-_")[:60] or "knowledge"
            base = base if base[:1].isalpha() else f"kb-{base}"
            names.append(add_tool({"name": snake(tname, "tool"), "connector": "knowledge.search", "with": {"base": base},
                                   "description": str(doc.get("description") or f"Search {human(tname)} for facts to answer the caller.")[:600],
                                   "mock": {"results": []}}))
            report.note(f"tool {tname}: a data store; it now searches the local knowledge base “{base}”. Create it on the "
                        "Knowledge page and upload the same documents.")
        elif doc or tname:
            names.append(add_tool({"name": snake(tname, "tool"), "description": (doc.get("description") or human(tname))[:600],
                                   "method": "POST", "url": "", "parameters": [], "mock": {"note": "replace with the real answer"}}))
            report.note(f"tool {tname}: became a stub with a mock answer; point it at a real service.")
        converted[tname] = names
        return names

    playbook_step: dict[str, str] = {}
    playbooks = []
    for p in ex.glob("playbooks/*/*.json"):
        parts = p.split("/")
        if len(parts) == 3 and parts[2] == f"{parts[1]}.json":
            playbooks.append(ex.json(p) or {})
    for pb in playbooks:
        pname = pb.get("displayName") or "Playbook"
        ins = pb.get("instruction") or {}
        steps = _params(_steps(ins.get("steps") or []))
        prompt = "\n".join(x for x in [f"Goal: {pb.get('goal')}" if pb.get("goal") else "",
                                        _refs(str(ins.get("guidelines") or "")), steps] if x) or "Help the caller."
        data: dict[str, Any] = {"prompt": prompt, "add_global_prompt": True, "allow_interrupt": True}
        mine = [n for t in pb.get("referencedTools") or [] for n in tool_for(_ref(t))]
        if mine:
            data["tools"] = list(dict.fromkeys(mine))
        playbook_step[pname] = g.add("agentNode", pname, data, key=f"playbook {pname}")
    stays: dict[str, list[tuple[str, str]]] = {}

    for fname in end_flows:                    # a flow that only says goodbye is an End call step
        if fname == "End Conversation":           # ours: the goodbye wording is in the playbooks
            flow_step[fname] = end_step()
            continue
        froutes = cx.flows[fname]["doc"].get("transitionRoutes") or []
        said = " ".join(t for r in froutes for t in _texts(r.get("triggerFulfillment"), cx.lang))
        fdata: dict[str, Any] = {"mode": "scripted", "say": said, "add_global_prompt": True}
        to = next((x for r in froutes if (x := _transfer(r.get("triggerFulfillment")))), "")
        if to:
            fdata["transfer_to"] = to
        flow_step[fname] = g.add("endCall", fname, fdata, key=f"flow {fname}")

    # ---- Routes ----------------------------------------------------------------------
    def wire(src: str, routes: list[dict], fname: str | None, where: str) -> None:
        for r in routes:
            hook = (r.get("triggerFulfillment") or {}).get("webhook")
            rcond = (r.get("condition") or "").strip()
            hook_done = bool(hook and scripted(src) and not r.get("intent") and (not rcond or rcond.lower() == "true" or "FINAL" in rcond))
            if hook_done:                              # the page's "form complete" webhook: runs before its rules
                done = node_of(src)["data"].setdefault("on_done", [])
                tool = webhook_tool(hook, (r.get("triggerFulfillment") or {}).get("tag"))
                if tool not in done:
                    done.append(tool)
            target_page, target_flow = r.get("targetPage"), r.get("targetFlow") or r.get("targetPlaybook")
            special = _special(target_page) if target_page else None
            dst = None
            if special == "end":
                dst = hang_up() if scripted(src) else end_step()
            elif special == "end_flow":
                dst = end_step()
                report.note(f"{where}: a route ends the flow; it now ends the call. If the flow returns to another, add that transfer.")
            elif special == "escalate":
                dst = handoff_step()
            elif special in ("current", "previous"):
                report.note(f"{where}: a route back to the {special} page was skipped.")
                continue
            elif special == "start" and fname:
                dst = flow_step[fname]
            elif target_page:
                dst = page_step.get((fname or "", _ref(target_page))) or next((v for (f2, pg), v in page_step.items() if pg == _ref(target_page)), None)
            elif target_flow:
                dst = flow_step.get(_ref(target_flow)) or playbook_step.get(_ref(target_flow))
            to = _transfer(r.get("triggerFulfillment"))
            if to:                                     # the route hands the caller to a person: that ends the call
                dst = transfer_step(to)
                if (target_page and special != "end") or target_flow:
                    report.note(f"{where}: a route transfers the caller to {to} and then goes on; here the transfer ends the call.")
            if not dst and not (target_page or target_flow):
                said = " ".join(_texts(r.get("triggerFulfillment"), cx.lang))
                cond0 = (r.get("condition") or "").strip()
                if said and scripted(src) and not r.get("intent") and (cond0.lower() == "true" or _final_only(cond0)):
                    node_of(src)["data"]["done_say"] = said        # what the page says once its form is complete
                    continue
                if said:
                    when = _condition_text(r.get("condition") or "") if r.get("condition") else f"The caller wants: {human(_ref(r.get('intent')))}."
                    stays.setdefault(src, []).append((when, said))
                continue
            if not dst:
                if target_page or target_flow:
                    report.note(f"{where}: a route points at {_ref(target_page or target_flow)}, which isn't in the export.")
                continue
            intent, cond = _ref(r.get("intent")), (r.get("condition") or "").strip()
            speech0 = " ".join(_texts(r.get("triggerFulfillment"), cx.lang))
            if not intent and dst != start and (scripted(src) or (cond and cond.lower() != "true" and "FINAL" not in cond)):
                rule = _rule_from(cond, scripted(src))
                if rule is not None:
                    label = str(r.get("description") or "").strip()[:50] or ("otherwise" if rule == {"otherwise": True} else "condition")
                    text = {"otherwise": "Otherwise."}.get(next(iter(rule)), f"When {rule.get('when')}.")
                    g.connect(src, dst, label, text, speech0, **rule)
                    continue
            if intent:
                label = str(r.get("description") or "").strip()[:50] or human(intent)
                condition = cx.intent_condition(intent) + (f" Only if: {cond}" if cond else "")
            else:
                label = "next" if cond.lower() == "true" else "details collected" if "FINAL" in cond else "condition"
                condition = _condition_text(cond)
                if cond and cond.lower() != "true" and "FINAL" not in cond:
                    report.note(f"{where}: the condition “{cond}” couldn't become a rule, so the model judges it; check it reads right.")
            speech = " ".join(_texts(r.get("triggerFulfillment"), cx.lang))
            if hook and not hook_done:
                report.note(f"{where}: a route calls webhook {_ref(hook)}; add the matching tool to the step it goes to.")
            if dst == start:
                continue
            g.connect(src, dst, label, condition, speech)

    def node_of(sid: str) -> dict:
        return next(n for n in g.nodes if n["id"] == sid)

    def scripted(sid: str) -> bool:
        return node_of(sid)["data"].get("mode") == "scripted"

    for fname, f in cx.flows.items():
        if fname in end_flows:
            continue
        groups = cx.route_groups(f["dir"])
        routes = list(f["doc"].get("transitionRoutes") or [])
        for gname in f["doc"].get("transitionRouteGroups") or []:
            routes += groups.get(_ref(gname), [])
        if fname == start_flow:                       # the welcome route greets (Start); where it goes is the way in
            routes = [{k: v for k, v in r.items() if k in ("targetPage", "targetFlow", "targetPlaybook")} | {"condition": "true"}
                      if "welcome" in _ref(r.get("intent")).lower() else r for r in routes]
            routes = [r for r in routes if not (r.get("condition") == "true" and len(r) == 1)]
        wire(flow_step[fname], routes, fname, f"flow {fname}")
        if f["doc"].get("eventHandlers"):
            report.note(f"flow {fname}: {len(f['doc']['eventHandlers'])} event handler(s) (no-match, no-input, …) weren't carried over.")
        for doc in pages_of[fname]:
            pname = doc.get("displayName") or "page"
            if node_of(page_step[(fname, pname)])["type"] == "endCall":
                if doc.get("transitionRoutes") or doc.get("form"):
                    report.note(f"page {pname}: hands the caller to {node_of(page_step[(fname, pname)])['data']['transfer_to']}, "
                                "which ends the call here; its form and routes weren't carried over.")
                continue
            proutes = list(doc.get("transitionRoutes") or [])
            for gname in doc.get("transitionRouteGroups") or []:
                proutes += groups.get(_ref(gname), [])
            wire(page_step[(fname, pname)], proutes, fname, f"page {pname}")
            handlers = doc.get("eventHandlers") or []
            sid = page_step[(fname, pname)]
            for h in list(handlers):                     # what to say when an answer isn't understood
                if h.get("event") == "sys.no-match-default" and not (h.get("targetPage") or h.get("targetFlow")) and scripted(sid):
                    said = " ".join(_texts(h.get("triggerFulfillment"), cx.lang))
                    if said:
                        node_of(sid)["data"]["no_match_say"] = said
                        handlers = [x for x in handlers if x is not h]
            for h in handlers:                           # repeated no-match with somewhere to go: our "no match"
                m = re.match(r"sys\.no-match(?:-(\d))?", str(h.get("event", "")))
                target = _ref(h.get("targetPage")) or _ref(h.get("targetFlow"))
                if m and target and scripted(page_step[(fname, pname)]):
                    dst = (hang_up() if _special(target) == "end" else handoff_step() if _special(target) == "escalate"
                           else page_step.get((fname, target)) or flow_step.get(target))
                    if dst:
                        if m.group(1):
                            node_of(page_step[(fname, pname)])["data"]["max_attempts"] = int(m.group(1))
                        g.connect(page_step[(fname, pname)], dst, str(h.get("description") or "").strip()[:50] or "no match",
                                  "When the caller's answers can't be understood.",
                                  " ".join(_texts(h.get("triggerFulfillment"), cx.lang)), on="no_match")
                        handlers = [x for x in handlers if x is not h]
            if handlers:
                report.note(f"page {pname}: {len(handlers)} event handler(s) weren't carried over.")

    by_id = {n["id"]: n for n in g.nodes}
    for n in list(g.nodes):                             # a page that never moves on: let the call finish there
        sid = n["id"]
        if n["type"] != "agentNode" or sid not in page_step.values() or any(e["source"] == sid for e in g.edges):
            continue
        last = stays.pop(sid, []) if len(stays.get(sid, [])) == 1 else []
        if n["data"].get("mode") == "scripted":
            g.connect(sid, hang_up(), "finished", "Otherwise.", otherwise=True)
        elif last:
            g.connect(sid, end_step(), "finished", last[0][0], last[0][1])
        else:
            g.connect(sid, end_step(), "finished", "This step's job is done and the caller has nothing else.")
        report.note(f"step {n['data']['name']}: it had no way out in Dialogflow, so it now finishes the call.")
    for sid, lines in stays.items():
        if by_id[sid]["data"].get("mode") == "scripted":
            for w, t in lines:
                report.note(f"step {by_id[sid]['data']['name']}: a route that only says “{t[:80]}” ({w.rstrip('.')}) and stays "
                            "wasn't carried over.")
            continue
        by_id[sid]["data"]["prompt"] += "\n\nWhile on this step:\n" + "\n".join(f"- {w} Then say: “{t}”" for w, t in lines)

    for pb in playbooks:
        src = playbook_step[pb.get("displayName") or "Playbook"]
        for ref in pb.get("referencedPlaybooks") or []:
            dst = playbook_step.get(_ref(ref))
            if dst:
                goal = next((x.get("goal") for x in playbooks if x.get("displayName") == _ref(ref)), "") or human(_ref(ref))
                g.connect(src, dst, human(_ref(ref)), f"The caller needs this: {goal}")
        for ref in pb.get("referencedFlows") or []:
            dst = flow_step.get(_ref(ref))
            if dst:
                g.connect(src, dst, human(_ref(ref)), f"The caller needs {human(_ref(ref))}.")
        # Dialogflow derives those lists from ${PLAYBOOK:x} / ${FLOW:x} in the steps; exports may only have the text.
        for text in _step_texts((pb.get("instruction") or {}).get("steps") or []):
            for kind, ref in re.findall(r"\$\{(PLAYBOOK|FLOW):\s*([^}]+)\}", text):
                dst = (playbook_step if kind == "PLAYBOOK" else flow_step).get(ref.strip())
                if not dst:
                    continue
                cond, speech = _rule(text)
                g.connect(src, dst, "end conversation" if dst in end_id else human(ref.strip()), cond, speech)
    report.counts["playbooks"] = len(playbooks)
    shared = [next((x for x in (pb.get("instruction") or {}).get("steps") or [] if isinstance(x, dict) and x.get("text") == SHARED), None)
              for pb in playbooks]
    if playbooks and shared[0] and all(x == shared[0] for x in shared):
        g.add("globalNode", "Global instructions", {"prompt": _params("\n".join(
            str(x.get("text", "")) for x in shared[0].get("steps") or []))}, key="global")

    # ---- Entry point -------------------------------------------------------------------
    start_pb = _ref(cx.agent.get("startPlaybook"))
    entry = playbook_step.get(start_pb) if start_pb else None
    entry = entry or (flow_step.get(start_flow) if start_flow else None) or next(iter(playbook_step.values()), None)
    if not entry:
        raise ImportError_("the export has no flows or playbooks to import")
    g.connect(start, entry, "after greeting", "Right after the greeting.", after_greeting=True)
    reached = {e["target"] for e in g.edges}
    for pb in playbooks:                                 # nothing routes to it: offer it from the entry step
        sid = playbook_step[pb.get("displayName") or "Playbook"]
        if sid != entry and sid not in reached:
            pname = pb.get("displayName") or "Playbook"
            g.connect(entry, sid, human(pname), f"The caller needs this: {pb.get('goal') or human(pname)}")
            report.note(f"playbook {pname}: nothing routed to it, so the main step can now transfer to it. Check that's what you want.")
    # A flow whose start page only passes the caller on: go straight to where it leads.
    flow_nodes = set(flow_step.values())
    for n in list(g.nodes):
        d = n["data"]
        if n["id"] not in flow_nodes or n["type"] != "agentNode" or d.get("mode") != "scripted" or d.get("say") or d.get("form"):
            continue
        outs = [e for e in g.edges if e["source"] == n["id"]]
        if len(outs) != 1 or not outs[0]["data"].get("otherwise") or outs[0]["data"].get("transition_speech") or outs[0]["target"] == n["id"]:
            continue
        nxt = outs[0]["target"]
        g.edges.remove(outs[0])
        keys = set()
        for e in list(g.edges):
            if e["target"] == n["id"]:
                e["target"] = nxt
            k = (e["source"], e["target"], e["data"].get("label"))
            if k in keys:
                g.edges.remove(e)
            keys.add(k)
        g.nodes.remove(n)
        flow_step = {f: (nxt if v == n["id"] else v) for f, v in flow_step.items()}
        gone = n["data"]["name"]
        twin = next((m for m in g.nodes if m["data"]["name"].lower() == f"{gone} 2".lower()), None)
        if twin and not any(m["data"]["name"].lower() == gone.lower() for m in g.nodes):
            twin["data"]["name"] = gone                # a page named like its flow keeps its own name
    yes_no = {f["name"] for n in g.nodes for f in n["data"].get("form") or [] if f.get("type") == "yes_no"}
    for e in g.edges:                                  # yes/no answers are true/false here, not text
        if e["data"].get("when") and yes_no:
            e["data"]["when"] = re.sub(r'\b(' + "|".join(map(re.escape, sorted(yes_no))) + r')\s*(==|!=)\s*"(true|false)"',
                                       r"\1 \2 \3", e["data"]["when"])
            if e["data"]["condition"].startswith("When "):
                e["data"]["condition"] = f"When {e['data']['when']}."
    # Leave out flows and pages nothing leads to (a start flow kept beside a start playbook, say).
    reach, todo = {start}, [start]
    while todo:
        cur = todo.pop()
        for e in g.edges:
            if e["source"] == cur and e["target"] not in reach:
                reach.add(e["target"])
                todo.append(e["target"])
    flowish = set(flow_step.values()) | set(page_step.values())
    dropped = [n["data"]["name"] for n in g.nodes if n["id"] in flowish - reach and n["type"] == "agentNode"]
    if dropped:
        g.nodes = [n for n in g.nodes if not (n["id"] in flowish - reach and n["type"] == "agentNode")]
        g.edges = [e for e in g.edges if e["source"] in {n["id"] for n in g.nodes} and e["target"] in {n["id"] for n in g.nodes}]
        report.note(f"Left out {len(dropped)} step(s) nothing leads to: {', '.join(dropped[:6])}{'…' if len(dropped) > 6 else ''}.")
    if start_pb and playbook_step.get(start_pb) and not greeting:
        first = next(iter(_step_texts(((next((x for x in playbooks if x.get("displayName") == start_pb), {}) or {})
                                       .get("instruction") or {}).get("steps") or [])), "")
        m = re.search(r"greet(?:ing)? the (?:user|caller) with:?\s*“([^”]+)”", first, re.I)
        if m:
            greeting = m.group(1)
            for n in g.nodes:
                if n["id"] == start:
                    n["data"]["greeting"] = _params(greeting)
    if not greeting:
        report.note("No welcome message was found; the model greets callers. Add a greeting on the Start step if you want a fixed one.")
    langs = cx.agent.get("supportedLanguageCodes") or []
    if langs:
        report.note(f"Only {cx.lang} was imported; the agent also had {', '.join(langs)}.")
    if ex.glob("generators/*/*.json") + ex.glob("generators/*.json"):
        report.note("Generators weren't carried over; their prompts can become step instructions.")
    report.counts["intents"] = len(cx.intents)
    report.counts["tools"] = len(tools)
    if cx.webhooks:
        report.counts["webhooks"] = len(cx.webhooks)
    collected = {v["name"] for n in g.nodes for v in n["data"].get("extraction_variables") or []}
    collected |= {f["name"] for n in g.nodes for f in n["data"].get("form") or [] if isinstance(f, dict)}
    used: set[str] = set()
    tpl = lambda text: set(re.findall(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}", text or ""))   # noqa: E731
    for n in g.nodes:
        d = n["data"]
        for key in ("prompt", "greeting", "say", "done_say", "no_match_say"):
            used |= tpl(d.get(key))
        for f in d.get("form") or []:
            for key in ("ask", "reprompt", "confirm"):
                used |= tpl(f.get(key))
    tested: set[str] = set()
    for e in g.edges:
        used |= tpl(e["data"].get("transition_speech"))
        if e["data"].get("when"):
            try:
                tested |= pages.names_in(e["data"]["when"])
            except pages.ExpressionError:
                pass
    # Values Dialogflow set at run time: blank for wording, null for values only conditions test.
    context: dict[str, Any] = {v: "" for v in sorted(used - collected)}
    context.update({v: None for v in sorted(tested - collected - used - SYSTEM_VARIABLES - CLOCK_VARIABLES)})
    if context:
        report.note("These values were set by Dialogflow at run time (webhooks or parameter presets) and start empty here: "
                    + ", ".join(context) + ". Give them example values or collect them in a step.")
    if "(the generated answer" in str(g.nodes):
        report.note("Some lines used a generator's output; write that part into the step's instructions.")
    return g.agent(agent_name, context, scrub_tools(tools, report), "imported from Dialogflow CX"), report


def _refs(text: str) -> str:
    """Playbook steps mention tools and playbooks as ${TOOL:Name}; write them as plain names."""
    return re.sub(r"\$\{(?:TOOL|PLAYBOOK|FLOW):\s*([^}]+)\}", lambda m: snake(m.group(1).strip(), "x"), text)


SHARED = "Always follow these rules:"      # how our exporter writes global instructions into each playbook


def _final_only(cond: str) -> bool:
    return bool(re.fullmatch(r'\$page\.params\.status\s*=\s*"?FINAL"?', cond.strip()))


def _rule_from(cond: str, scripted_src: bool) -> dict | None:
    """A Dialogflow condition route -> our rule: {"otherwise": True} or {"when": expr}; None if it can't be expressed."""
    from_dialogflow = pages.from_dialogflow
    c = cond.strip()
    if scripted_src and (not c or c.lower() == "true" or _final_only(c)):
        return {"otherwise": True}                     # our rules run once the form is complete
    m = re.match(r'\$page\.params\.status\s*=\s*"?FINAL"?\s+AND\s+(.+)$', c, re.I | re.S)
    rest = m.group(1) if m else c
    expr = from_dialogflow(rest)
    return {"when": expr} if expr else None


def _goodbye_only(f: dict, ex: Export) -> bool:
    """A flow with no pages whose routes all end the session (an exported End call step)."""
    if ex.glob(f"{f['dir']}/pages/*.json"):
        return False
    routes = f["doc"].get("transitionRoutes") or []
    return bool(routes) and all(_special(r.get("targetPage")) == "end" for r in routes)


def _step_texts(steps: list) -> list[str]:
    out = []
    for s in steps:
        if isinstance(s, dict):
            out.append(str(s.get("text") or ""))
            out += _step_texts(s.get("steps") or [])
        elif isinstance(s, str):
            out.append(s)
    return out


def _rule(text: str) -> tuple[str, str]:
    """'If the caller wants X: say “Y”, then hand over to ${PLAYBOOK:Z}.' -> ('The caller wants X.', 'Y')."""
    clean = re.sub(r"\$\{(?:PLAYBOOK|FLOW|TOOL):\s*([^}]+)\}", lambda m: m.group(1).strip(), text).strip()
    m = re.match(r"(?:if|when)\s+(.+?):\s+(.*)$", clean, re.I | re.S)
    if not m:
        return clean[:400], ""
    cond = m.group(1).strip()
    speech = re.search(r"say “([^”]+)”,? then", m.group(2))
    return (cond[:1].upper() + cond[1:]).rstrip(".") + ".", speech.group(1) if speech else ""


def _steps(steps: list, depth: int = 0) -> str:
    out = []
    for s in steps:
        if isinstance(s, dict):
            text = str(s.get("text") or "")
            if re.search(r"\$\{(PLAYBOOK|FLOW):", text) or re.match(r"start by greeting the (user|caller)", text, re.I) \
                    or (depth == 0 and text == SHARED):
                pass                         # became a transfer, the Start step's greeting, or global instructions
            elif text:
                out.append(f"{'  ' * depth}- {_refs(text)}")
            out.append(_steps(s.get("steps") or [], depth + 1))
        elif isinstance(s, str):
            out.append(f"{'  ' * depth}- {_refs(s)}")
    return "\n".join(x for x in out if x)


def _webhook_stub(cx: _Ctx, hook: Any, tag: Any, report: Report) -> dict:
    wname = _ref(hook)
    name = snake(f"{wname} {tag or ''}", "webhook")
    uri = ((cx.webhooks.get(wname) or {}).get("genericWebService") or {}).get("uri", "")
    report.note(f"tool {name}: was Dialogflow webhook {wname}" + (f" (tag {tag})" if tag else "") +
                (f" at {uri}" if uri else "") + ". Dialogflow's webhook request format is different from ours, "
                "so it answers with a mock until you point it at an endpoint that takes JSON parameters.")
    return {"name": name, "description": f"Look up what this step needs (was the {wname} webhook{f', tag {tag}' if tag else ''}).",
            "method": "POST", "url": "", "parameters": [], "mock": {"note": "replace with the webhook's real answer"}}
