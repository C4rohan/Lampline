"""Exporting to Dialogflow CX (playbooks) and CX Agent Studio, and importing the result back."""

from __future__ import annotations

import io
import json
import zipfile
from importlib import resources

import pytest
import yaml

from vk import agentfs
from vk.cli import main
from vk.exporters import ExportError, build, export
from vk.importers import convert
from vk.lint import lint

TEMPLATES = ["front-desk", "appointment-finder", "bilingual-front-desk", "appointment-scheduling"]


def template(name: str) -> agentfs.Agent:
    return agentfs.load(resources.files("vk") / "templates" / name)


def steps(agent) -> list[str]:
    return sorted(n["data"]["name"] for n in agent.definition["nodes"] if n["type"] == "agentNode")


def with_tools() -> agentfs.Agent:
    a = template("appointment-finder")
    a.tools = a.tools + [
        {"name": "book_slot", "description": "Book a time.", "method": "POST",
         "url": "https://sched.example.test/v1/clinics/{{clinic_id}}/book",
         "headers": {"Authorization": "Bearer {{secrets.SCHED_KEY}}"},
         "parameters": [{"name": "time", "type": "string", "description": "the time", "required": True},
                        {"name": "party", "type": "integer"}], "mock": {"ok": True}},
        {"name": "slot_list", "description": "List times.", "method": "GET",
         "url": "https://sched.example.test/v1/slots?day={{day}}",
         "parameters": [{"name": "day", "type": "string", "required": True}]},
    ]
    step = next(n for n in a.definition["nodes"] if n["type"] == "agentNode")
    step["data"]["tools"] = step["data"].get("tools", []) + ["book_slot", "slot_list"]
    return a


@pytest.mark.parametrize("target", ["ces", "dfcx"])
@pytest.mark.parametrize("name", TEMPLATES)
def test_round_trip(target, name):
    a = template(name)
    data, report = export(a, target)
    b, _ = convert(data)
    assert steps(b) == steps(a)
    assert [f.message for f in lint(b) if f.severity == "error"] == []
    start = next(n for n in b.definition["nodes"] if n["type"] == "startCall")
    assert ("{{" in start["data"]["greeting"]) == ("{{" in next(n for n in a.definition["nodes"] if n["type"] == "startCall")["data"]["greeting"])
    assert set(a.context_variables) <= set(b.context_variables)
    assert report.counts


def test_ces_files():
    files, report = build(with_tools(), "ces")
    root = next(k.split("/")[0] for k in files)
    app = json.loads(files[f"{root}/app.json"])
    assert app["rootAgent"] and app["name"] == root and all(v["description"] for v in app["variableDeclarations"])
    agent = json.loads(files[f"{root}/agents/{app['rootAgent']}/{app['rootAgent']}.json"])
    assert "end_session" in agent["tools"] and agent["instruction"] == f"agents/{app['rootAgent']}/instruction.txt"
    ts = agent["toolsets"][0]
    assert sorted(ts["toolIds"]) == ["book_slot", "slot_list"]
    spec = yaml.safe_load(files[f"{root}/toolsets/{ts['toolset']}/open_api_toolset/open_api_schema.yaml"])
    assert spec["servers"] == [{"url": "https://sched.example.test"}]
    book = spec["paths"]["/v1/clinics/{clinic_id}/book"]["post"]
    assert book["requestBody"]["content"]["application/json"]["schema"]["required"] == ["time"]
    assert book["security"] == [{"Authorization": []}] and "SCHED_KEY" not in json.dumps(spec)
    assert spec["paths"]["/v1/slots"]["get"]["parameters"][0]["name"] == "day"
    instruction = files[f"{root}/agents/{app['rootAgent']}/instruction.txt"]
    assert f"{{@TOOL: {ts['toolset']}_book_slot}}" in instruction
    assert any("secret" in n for n in report.notes)

    stub = files[f"{root}/tools/find_open_slots/python_function/python_code.py"]
    space: dict = {}
    exec(compile(stub, "python_code.py", "exec"), space)          # our own generated code
    assert space["find_open_slots"](day="Tuesday") == template("appointment-finder").tools[0]["mock"]


def test_dfcx_files():
    files, report = build(with_tools(), "dfcx", time_zone="Europe/London")
    agent = json.loads(files["agent.json"])
    assert agent["timeZone"] == "Europe/London" and agent["defaultLanguageCode"] == "en"
    root = json.loads(files[f"playbooks/{agent['startPlaybook']}/{agent['startPlaybook']}.json"])
    assert root["name"] == "00000000-0000-0000-0000-000000000000" and root["goal"]
    assert json.loads(files["intents/Default Negative Intent/Default Negative Intent.json"])["isFallback"]
    assert json.loads(files["flows/Default Start Flow/Default Start Flow.json"])["name"] == "0" * 8 + "-0000-0000-0000-" + "0" * 12
    texts = json.dumps(root["instruction"])
    assert "${FLOW:End Conversation}" in texts and "$session.params." in texts and "{{" not in texts
    assert "referencedPlaybooks" not in root                       # Dialogflow fills that in itself
    api = next(k for k in files if k.endswith("/schema.yaml"))
    assert "/v1/clinics/{clinic_id}/book" in yaml.safe_load(files[api])["paths"]
    assert any(t.startswith("${TOOL:") or "${TOOL:" in t for t in [texts])
    end = json.loads(files["flows/End Conversation/End Conversation.json"])
    assert end["transitionRoutes"][0]["targetPage"] == "End Session"
    first, second = build(with_tools(), "dfcx")[0], build(with_tools(), "dfcx")[0]
    assert first == second                                          # stable ids: re-exports diff cleanly


def test_nothing_secret_leaves():
    a = with_tools()
    for target in ("ces", "dfcx"):
        files, _ = build(a, target)
        assert "secrets." not in json.dumps(files)


def test_export_errors():
    a = template("front-desk")
    a.definition = {"nodes": [], "edges": []}
    with pytest.raises(ExportError):
        export(a, "ces")
    with pytest.raises(ExportError):
        export(template("front-desk"), "lex")


def test_vk_export(tmp_path, capsys):
    folder = tmp_path / "desk"
    agentfs.write(template("front-desk"), folder)
    out = tmp_path / "desk.zip"
    assert main(["export", "ces", str(folder), "--out", str(out)]) == 0
    assert "Import app" in capsys.readouterr().out
    with zipfile.ZipFile(io.BytesIO(out.read_bytes())) as z:
        assert any(n.endswith("/app.json") for n in z.namelist())
    assert main(["export", "ces", str(folder), "--out", str(out)]) == 1             # exists
    assert main(["export", "dfcx", str(folder), "--folder", "--out", str(tmp_path / "cx")]) == 0
    assert (tmp_path / "cx" / "agent.json").is_file()


def scripted_agent() -> agentfs.Agent:
    """A mixed agent: a scripted intake page, an LLM step and scripted endings, in a "Booking" flow."""
    return agentfs.Agent(name="Harbor Clinic", context_variables={}, definition={
        "nodes": [
            {"id": "start", "type": "startCall", "data": {"name": "Start", "greeting": "Hi, this is Harbor Clinic.", "prompt": "Greet."}},
            {"id": "intake", "type": "agentNode", "data": {
                "name": "Intake", "flow": "Booking", "mode": "scripted", "max_attempts": 2,
                "say": "Let's get you booked.", "no_match_say": "Sorry, I didn't catch that.",
                "form": [
                    {"name": "caller_name", "type": "text", "ask": "What's your name?"},
                    {"name": "party", "type": "integer", "ask": "How many people?", "max": 8, "reprompt": "A number from 1 to 8, please."},
                    {"name": "plan", "type": "choice", "ask": "Gold, Silver or Bronze?",
                     "choices": ["Gold", "Silver", {"value": "Bronze", "synonyms": ["basic"]}]},
                    {"name": "member", "type": "yes_no", "ask": "Are you a member?"},
                    {"name": "member_id", "type": "digits", "ask": "Key in your member ID.", "keypad": True, "max_length": 6,
                     "required": False},
                ]}},
            {"id": "gold", "type": "agentNode", "data": {"name": "Gold desk", "flow": "Booking", "prompt": "Thank gold members warmly."}},
            {"id": "confirm", "type": "agentNode", "data": {"name": "Confirm", "mode": "scripted", "say": "Booking {{party}} for you, {{caller_name}}."}},
            {"id": "done", "type": "endCall", "data": {"name": "Booked", "mode": "scripted", "say": "You're booked. Goodbye."}},
            {"id": "person", "type": "endCall", "data": {"name": "Hand off", "mode": "scripted", "say": "Let me get a person."}},
        ],
        "edges": [
            {"id": "e0", "source": "start", "target": "intake", "data": {"label": "begin", "condition": "Right away.", "after_greeting": True}},
            {"id": "e1", "source": "intake", "target": "gold", "data": {"label": "gold", "condition": "-", "when": 'plan == "Gold" or member == true'}},
            {"id": "e2", "source": "intake", "target": "confirm", "data": {"label": "others", "condition": "-", "otherwise": True}},
            {"id": "e3", "source": "intake", "target": "person", "data": {"label": "wants a person",
                                                                          "condition": "The caller asks for a person, e.g. “agent please”."}},
            {"id": "e4", "source": "intake", "target": "person", "data": {"label": "lost", "condition": "-", "on": "no_match"}},
            {"id": "e5", "source": "gold", "target": "done", "data": {"label": "finished", "condition": "The caller is done."}},
            {"id": "e6", "source": "confirm", "target": "done", "data": {"label": "next", "condition": "-", "otherwise": True,
                                                                         "transition_speech": "All set."}},
        ],
    })


def test_dfcx_scripted_pages_become_flows_and_pages():
    a = scripted_agent()
    assert [f.message for f in lint(a) if f.severity == "error"] == []
    files, report = build(a, "dfcx")
    agent = json.loads(files["agent.json"])
    assert agent["startFlow"] == "Default Start Flow" and "startPlaybook" not in agent
    start = json.loads(files["flows/Default Start Flow/Default Start Flow.json"])
    welcome = start["transitionRoutes"][0]
    assert welcome["targetFlow"] == "Booking" and "Harbor Clinic" in json.dumps(welcome["triggerFulfillment"])
    booking = json.loads(files["flows/Booking/Booking.json"])
    assert booking["transitionRoutes"] == [{"condition": "true", "targetPage": "Intake"}]
    page = json.loads(files["flows/Booking/pages/Intake.json"])
    params = {p["displayName"]: p for p in page["form"]["parameters"]}
    assert params["party"]["entityType"] == "@sys.number-integer"
    assert params["plan"]["entityType"] == "@plan" and params["member"]["entityType"] == "@yes-no"
    assert params["member_id"]["advancedSettings"]["dtmfSettings"]["maxDigits"] == 6 and params["member_id"]["required"] is False
    assert "A number from 1 to 8" in json.dumps(params["party"]["fillBehavior"]["repromptEventHandlers"])
    routes = page["transitionRoutes"]
    assert routes[0]["condition"] == ('$page.params.status = "FINAL" AND ($session.params.plan = "Gold" OR '
                                      '$session.params.member = "true")')
    assert routes[0]["targetPlaybook"] == "Gold desk" and routes[0]["description"] == "gold"
    assert routes[1] == {"targetPage": "Confirm", "description": "others", "condition": '$page.params.status = "FINAL"'}   # it joined the flow it's reached from
    assert routes[2]["intent"] == "wants.a.person" and routes[2]["targetPage"] == "END_SESSION"
    assert {h["event"] for h in page["eventHandlers"]} == {"sys.no-match-2", "sys.no-match-default"}
    plan = json.loads(files["entityTypes/plan/entities/en.json"])["entities"]
    assert {"value": "Bronze", "synonyms": ["Bronze", "basic"]} in plan
    phrases = json.loads(files["intents/wants.a.person/trainingPhrases/en.json"])["trainingPhrases"]
    assert [p["parts"][0]["text"] for p in phrases] == ["agent please"]
    gold = json.loads(files["playbooks/Gold desk/Gold desk.json"])
    assert "${FLOW:Booked}" in json.dumps(gold)                      # a scripted ending reached from a playbook
    assert json.loads(files["flows/Booked/Booked.json"])["transitionRoutes"][0]["targetPage"] == "END_SESSION"
    confirm = json.loads(files["flows/Booking/pages/Confirm.json"])
    assert "$session.params.party" in json.dumps(confirm["entryFulfillment"])
    assert confirm["transitionRoutes"][0]["condition"] == "true"
    assert any("range" in n for n in report.notes)
    assert report.counts["pages"] == 2 and report.counts["playbooks"] == 1
    assert build(a, "dfcx")[0] == files


def test_dfcx_scripted_round_trip():
    a = scripted_agent()
    data, _ = export(a, "dfcx")
    b, report = convert(data)
    nodes = {n["data"]["name"]: n for n in b.definition["nodes"]}
    names = {n["id"]: n["data"]["name"] for n in b.definition["nodes"]}
    edges = {(names[e["source"]], names[e["target"]], e["data"]["label"]): e["data"] for e in b.definition["edges"]}
    intake = nodes["Intake"]["data"]
    assert intake["mode"] == "scripted" and intake["flow"] == "Booking" and intake["say"] == "Let's get you booked."
    assert intake["no_match_say"] == "Sorry, I didn't catch that." and intake["max_attempts"] == 2
    fields = {f["name"]: f for f in intake["form"]}
    assert {k: f["type"] for k, f in fields.items()} == {f["name"]: f["type"] for f in a.definition["nodes"][1]["data"]["form"]}
    assert fields["member_id"]["keypad"] and fields["member_id"]["max_length"] == 6
    assert fields["party"]["reprompt"] == "A number from 1 to 8, please."
    starts = [e for e in b.definition["edges"] if names[e["source"]] == "Start"]
    assert [names[e["target"]] for e in starts] == ["Intake"]
    rule = edges[("Intake", "Gold desk", "gold")]                    # labels travel as route descriptions
    assert rule["when"] == 'plan == "Gold" or member == true' and rule["condition"] == 'When plan == "Gold" or member == true.'
    assert next(d for (s, t, _), d in edges.items() if s == "Intake" and t == "Confirm")["otherwise"]
    person = next(d for (s, t, _), d in edges.items() if s == "Intake" and t == "Hang up" and "when" not in d and not d.get("on"))
    assert person["condition"] == "The caller asks for a person, e.g. “agent please”." and person["transition_speech"] == "Let me get a person."
    assert any(d.get("on") == "no_match" for (s, _, _), d in edges.items() if s == "Intake")
    assert nodes["Booked"]["type"] == "endCall" and nodes["Booked"]["data"]["say"] == "You're booked. Goodbye."
    assert nodes["Confirm"]["data"]["say"] == "Booking {{party}} for you, {{caller_name}}."
    assert [f.message for f in lint(b) if f.severity == "error"] == []


def test_ces_describes_scripted_pages():
    files, report = build(scripted_agent(), "ces")
    root = next(k.split("/")[0] for k in files)
    text = files[f"{root}/agents/Intake/instruction.txt"]
    assert "Say exactly: “Let's get you booked.”" in text and "- plan: “Gold, Silver or Bronze?” (one of: Gold, Silver, Bronze)" in text
    assert 'When the details collected so far show plan == "Gold" or member == true' in text
    app = json.loads(files[f"{root}/app.json"])
    assert {"caller_name", "party", "member"} <= {v["name"] for v in app["variableDeclarations"]}
    assert any("scripted page" in n for n in report.notes)


def test_nothing_from_the_agent_becomes_code_or_escapes_the_folder(tmp_path):
    a = template("appointment-finder")
    evil = 'x"""\n    __import__("os").system("id")\n    """'
    a.tools = a.tools + [{"name": "../../../../tmp/pwn", "description": evil, "method": "POST", "url": "",
                          "parameters": [{"name": "day; import os", "type": "string", "description": evil}], "mock": {"ok": True}}]
    step = next(n for n in a.definition["nodes"] if n["type"] == "agentNode")
    step["data"]["tools"] = step["data"].get("tools", []) + ["../../../../tmp/pwn"]
    files, _ = build(a, "ces")
    assert all(".." not in k.split("/") for k in files)
    stub = next(v for k, v in files.items() if k.endswith("python_code.py") and "pwn" in k)
    import ast
    tree = ast.parse(stub)
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef))
    assert [type(x).__name__ for x in fn.body] == ["Expr", "Return"]          # the docstring and the return, nothing else
    assert isinstance(fn.body[0].value, ast.Constant) and "__import__" in fn.body[0].value.value   # kept as text only
    space: dict = {}
    exec(compile(stub, "python_code.py", "exec"), space)                        # our own generated code
    assert space[fn.name](**{fn.args.args[0].arg: "x"}) == {"ok": True}
    from vk.exporters.common import safe_rel, zip_bytes
    with pytest.raises(ExportError):
        zip_bytes({"a/../../x": "y"})
    with pytest.raises(ExportError):
        safe_rel("/etc/passwd")
