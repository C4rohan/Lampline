"""Scripted pages and condition transfers, mixed with LLM steps."""

from __future__ import annotations

import asyncio

import pytest

from conftest import write_agent
from vk import agentfs
from vk.lint import lint
from vpengine.graph import Graph
from vpengine.server import Engine


def intake(interpret="assisted", attempts=3):
    return {
        "nodes": [
            {"id": "start", "type": "startCall", "data": {"name": "Start", "greeting": "Hi, this is Harbor Clinic.", "prompt": "Greet."}},
            {"id": "intake", "type": "agentNode", "data": {
                "name": "Intake", "flow": "Booking", "mode": "scripted", "interpret": interpret, "max_attempts": attempts,
                "say": "Let's get you booked.",
                "form": [
                    {"name": "caller_name", "type": "text", "ask": "What's your name?"},
                    {"name": "party", "type": "integer", "ask": "How many people?", "max": 8,
                     "reprompt": "Sorry, I need a number from 1 to 8."},
                    {"name": "plan", "type": "choice", "ask": "Is your plan Gold, Silver or Bronze?",
                     "choices": ["Gold", "Silver", {"value": "Bronze", "synonyms": ["basic"]}],
                     "confirm": "So that's the {{plan}} plan, right?"},
                    {"name": "visit_day", "type": "date", "ask": "What day would you like?"},
                ]}},
            {"id": "gold", "type": "agentNode", "data": {"name": "Gold desk", "flow": "Booking", "prompt": "Thank gold members warmly."}},
            {"id": "done", "type": "endCall", "data": {"name": "Booked", "mode": "scripted",
                                                       "say": "Thanks {{caller_name}}, you're booked for {{visit_day}}. Goodbye."}},
            {"id": "person", "type": "endCall", "data": {"name": "Hand off", "mode": "scripted",
                                                         "say": "Let me get a person for you."}},
        ],
        "edges": [
            {"id": "e0", "source": "start", "target": "intake", "data": {"label": "begin", "condition": "Right away.", "after_greeting": True}},
            {"id": "e1", "source": "intake", "target": "gold", "data": {"label": "gold plan", "condition": "Gold members.", "when": 'plan == "Gold"'}},
            {"id": "e2", "source": "intake", "target": "done", "data": {"label": "everyone else", "condition": "Others.", "otherwise": True}},
            {"id": "e3", "source": "intake", "target": "person", "data": {"label": "wants a person",
                                                                          "condition": "The caller asks to speak to a person or human."}},
            {"id": "e4", "source": "intake", "target": "person", "data": {"label": "cannot understand", "condition": "Too many tries.", "on": "no_match"}},
            {"id": "e5", "source": "gold", "target": "done", "data": {"label": "finished", "condition": "The caller is done."}},
        ],
    }


@pytest.fixture
async def engine(cfg, tmp_path):
    made: list[Engine] = []

    def make(definition, context=None):
        d = tmp_path / "agents"
        write_agent(d / "clinic", definition, context or {})
        made.append(Engine(d, cfg))
        return made[-1]
    yield make
    for eng in made:                          # a failed test mustn't leave sessions keeping the loop busy
        for s in list(eng.sessions.values()):
            if hasattr(s, "close"):
                await s.close()
        await eng.shutdown()


def said(events):
    return [e["text"] for e in events if e.get("type") == "bot_speech"]


async def chat(engine, slug="clinic"):
    s = engine.new_text(engine.load_agent(slug), {})
    opening = await s.open()
    return s, opening


async def test_a_scripted_form_end_to_end(engine, requests_seen):
    s, opening = await chat(engine(intake()))
    assert said(opening) == ["Hi, this is Harbor Clinic.", "Let's get you booked.", "What's your name?"]
    assert said(await s.send("My name is Sam")) == ["How many people?"]
    assert s.flow.variables["caller_name"] == "Sam"
    assert said(await s.send("twelve")) == ["Sorry, I need a number from 1 to 8."]
    assert said(await s.send("three of us")) == ["Is your plan Gold, Silver or Bronze?"]
    assert said(await s.send("the basic one")) == ["So that's the Bronze plan, right?"]
    assert said(await s.send("no")) == ["Is your plan Gold, Silver or Bronze?"]
    assert said(await s.send("gold")) == ["So that's the Gold plan, right?"]
    assert said(await s.send("yes")) == ["What day would you like?"]
    events = await s.send("next tuesday")
    assert s.flow.variables["party"] == 3 and s.flow.variables["plan"] == "Gold"
    assert s.flow.variables["visit_day"].count("-") == 2
    moved = [e for e in events if e["type"] == "tool_call_started"]
    assert moved and moved[0]["rule"] == 'when plan == "Gold"'
    assert [e["node_name"] for e in events if e["type"] == "node_transition"][0] == "Gold desk"   # then the LLM step takes over
    await s.close()


async def test_otherwise_and_scripted_end(engine):
    d = intake()
    d["nodes"][1]["data"]["form"] = d["nodes"][1]["data"]["form"][:1]
    s, _ = await chat(engine(d, ))
    s.flow.variables["visit_day"] = "2026-10-20"
    events = await s.send("I'm Lee")
    assert said(events) == ["Thanks Lee, you're booked for 2026-10-20. Goodbye."]
    assert s.flow.ended and events[-1]["type"] == "call_ended"
    await s.close()


async def test_escape_route_and_no_match(engine):
    s, _ = await chat(engine(intake()))
    await s.send("Sam")
    events = await s.send("can I speak to a person please")
    assert said(events) == ["Let me get a person for you."] and s.flow.ended
    await s.close()

    s, _ = await chat(engine(intake(interpret="exact", attempts=2)))
    await s.send("Sam")
    assert said(await s.send("lots")) == ["Sorry, I need a number from 1 to 8."]
    assert said(await s.send("loads")) == ["Let me get a person for you."]
    await s.close()


async def test_exact_mode_never_asks_the_model(engine, requests_seen):
    d = intake(interpret="exact")
    d["edges"] = [e for e in d["edges"] if e["id"] != "e3"]
    s, _ = await chat(engine(d))
    before = len(requests_seen)
    for text in ["Sam Lee", "2", "silver", "yes", "tomorrow"]:
        await s.send(text)
    assert len(requests_seen) == before                           # every answer read by the built-in parsers
    assert s.flow.variables["caller_name"] == "Sam Lee" and s.flow.ended
    await s.close()


async def test_keypad_digits(engine):
    d = intake(interpret="exact")
    d["nodes"][1]["data"]["form"] = [{"name": "member_id", "type": "digits", "ask": "Enter your member ID.",
                                      "min_length": 4, "max_length": 4, "keypad": True}]
    s, _ = await chat(engine(d, ))
    for key in "4271":
        await s.flow.keypad(key)
    await asyncio.sleep(0.2)
    assert s.flow.variables["member_id"] == "4271" and s.flow.current.name == "Booked"
    assert any(e.get("keypad") for e in s.events)
    await s.close()


async def test_condition_transfer_on_an_llm_step(engine):
    d = {
        "nodes": [
            {"id": "s", "type": "startCall", "data": {"name": "Start", "greeting": "Hello.", "prompt": "Greet."}},
            {"id": "help", "type": "agentNode", "data": {"name": "Help", "prompt": "Help with parking."}},
            {"id": "vip", "type": "agentNode", "data": {"name": "VIP", "prompt": "Offer the VIP lot."}},
            {"id": "bye", "type": "endCall", "data": {"name": "Bye", "prompt": "Say goodbye."}},
        ],
        "edges": [
            {"id": "a", "source": "s", "target": "help", "data": {"label": "begin", "condition": "Now.", "after_greeting": True}},
            {"id": "b", "source": "help", "target": "vip", "data": {"label": "vip", "condition": "VIP member.", "when": "vip == true"}},
            {"id": "c", "source": "help", "target": "bye", "data": {"label": "goodbye", "condition": "The caller says goodbye."}},
            {"id": "c2", "source": "help", "target": "bye", "data": {"label": "billing", "condition": "A billing question."}},
            {"id": "d", "source": "vip", "target": "bye", "data": {"label": "goodbye", "condition": "The caller says goodbye."}},
            {"id": "d2", "source": "vip", "target": "bye", "data": {"label": "billing", "condition": "A billing question."}},
        ],
    }
    eng = engine(d, {"vip": True})
    tools = Graph.from_folder(eng.agents_dir / "clinic")
    assert [r.tool_name for r in tools.steps["help"].ask_routes] == ["goodbye", "billing"]   # rules aren't tools for the model
    s, _ = await chat(eng)
    await s.send("where do I park?")
    assert s.flow.current.name == "VIP"
    await s.close()


async def test_rule_loops_stop(engine):
    d = intake()
    d["nodes"] = d["nodes"][:2] + [{"id": "b", "type": "agentNode", "data": {"name": "B", "mode": "scripted", "say": "B."}}] + d["nodes"][2:]
    d["nodes"][1]["data"]["form"] = []
    d["edges"] = [d["edges"][0],
                  {"id": "x", "source": "intake", "target": "b", "data": {"label": "to b", "condition": "-", "otherwise": True}},
                  {"id": "y", "source": "b", "target": "intake", "data": {"label": "to a", "condition": "-", "otherwise": True}},
                  {"id": "z", "source": "b", "target": "done", "data": {"label": "end", "condition": "The caller is done."}}]
    s, opening = await chat(engine(d))
    assert any("loop" in e.get("message", "") for e in s.events if e["type"] == "execution_error")
    await s.close()


def test_lint_of_pages(tmp_path):
    d = intake()
    d["nodes"][1]["data"]["form"][1]["type"] = "count"
    d["nodes"][1]["data"]["form"][2]["choices"] = []
    d["edges"][1]["data"]["when"] = "plan = gold"
    d["edges"].append({"id": "e9", "source": "gold", "target": "done", "data": {"label": "x", "condition": "y", "otherwise": True}})
    d["edges"].append({"id": "e8", "source": "intake", "target": "done", "data": {"label": "vip", "condition": "z", "when": "tier == 1"}})
    findings = lint(agentfs.Agent(name="x", definition=d))
    msgs = [(f.code, f.severity, f.message) for f in findings]
    assert ("VK130", "error", "field party: type must be one of text, number, integer, yes_no, choice, date, time, digits, phone, email") in msgs
    assert any(c == "VK130" and "needs choices" in m for c, _, m in msgs)
    assert any(c == "VK131" and s == "error" and "can't read" in m for c, s, m in msgs)
    assert any(c == "VK131" and "only applies" in m for c, _, m in msgs)
    assert any(c == "VK131" and "tier" in m for c, _, m in msgs)
    assert not any(c == "VK104" for c, _, m in msgs)                 # a scripted page needs no prompt
    assert not any(c == "VK107" for c, _, m in msgs)                 # form fields count as provided
    clean = lint(agentfs.Agent(name="x", definition=intake()))
    assert [f for f in clean if f.severity == "error"] == []


async def test_rules_can_read_the_clock(engine):
    d = intake()
    d["nodes"][1]["data"]["form"] = []
    d["edges"][1]["data"]["when"] = 'current_hour >= 0 and current_time >= "00:00"'
    s, _ = await chat(engine(d))
    assert s.flow.current.name == "Gold desk"
    await s.close()


async def test_a_dialogflow_round_trip_still_runs(engine):
    from vk.exporters import export
    from vk.importers import convert
    d = intake(interpret="exact")
    d["nodes"][1]["data"]["form"][2].pop("confirm")              # Dialogflow forms don't read answers back
    b, _ = convert(export(agentfs.Agent(name="clinic", definition=d), "dfcx")[0])
    assert [f for f in lint(b) if f.severity == "error"] == []
    s, opening = await chat(engine(b.definition, b.context_variables))
    assert said(opening) == ["Hi, this is Harbor Clinic.", "Let's get you booked.", "What's your name?"]
    for text, reply in [("Sam", "How many people?"), ("3", "Is your plan Gold, Silver or Bronze?"),   # the 1-8 range stays behind
                        ("gold", "What day would you like?")]:
        assert said(await s.send(text)) == [reply]
    events = await s.send("tomorrow")
    assert [e["node_name"] for e in events if e["type"] == "node_transition"][0] == "Gold desk"
    await s.close()
