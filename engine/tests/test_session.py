import asyncio

import pytest

from conftest import TEMPLATES, mini_definition
from vpengine.graph import Graph
from vpengine.session import TextSession


def kinds(events, kind):
    return [e for e in events if e["type"] == kind]


async def opened(cfg, graph, **kw):
    s = TextSession(graph, cfg, **kw)
    events = await s.open(timeout=20)
    return s, events


async def test_greeting_is_spoken_without_the_model(cfg, requests_seen):
    g = Graph.from_definition("Mini", mini_definition(), {"business_name": "Riverside Clinic"})
    s, events = await opened(cfg, g)
    try:
        assert [e["node_name"] for e in kinds(events, "node_transition")] == ["Start"]
        assert kinds(events, "bot_speech")[0]["text"] == "Hello from Riverside Clinic."
        assert not [r for r in requests_seen if r["path"].endswith("/chat/completions")]
    finally:
        await s.close()


async def test_greeting_default_and_session_context(cfg):
    g = Graph.from_definition("Mini", mini_definition())
    s, events = await opened(cfg, g)
    assert kinds(events, "bot_speech")[0]["text"] == "Hello from the office."
    await s.close()
    s, events = await opened(cfg, g, variables={"business_name": "Hilltop Dental"})
    assert kinds(events, "bot_speech")[0]["text"] == "Hello from Hilltop Dental."
    await s.close()


async def test_after_greeting_hands_over_without_the_model(cfg, requests_seen):
    d = mini_definition()
    d["edges"][0]["data"]["after_greeting"] = True
    g = Graph.from_definition("Mini", d, {"business_name": "Riverside Clinic"})
    assert g.steps["1"].routes[0].after_greeting
    s, events = await opened(cfg, g)
    try:
        assert [e["node_name"] for e in kinds(events, "node_transition")] == ["Start", "Help"]
        assert kinds(events, "bot_speech")[0]["text"] == "Hello from Riverside Clinic."
        assert not [r for r in requests_seen if r["path"].endswith("/chat/completions")]
    finally:
        await s.close()
    # Only Start can hand over this way; the flag means nothing on other steps.
    d["edges"][1]["data"]["after_greeting"] = True
    assert not Graph.from_definition("Mini", d).steps["2"].routes[0].after_greeting


async def test_no_greeting_means_the_model_speaks_first(cfg):
    g = Graph.from_definition("Mini", mini_definition(greeting=""))
    s, events = await opened(cfg, g)
    try:
        speech = kinds(events, "bot_speech")
        assert len(speech) == 1 and speech[0]["text"].startswith("[test model]")
    finally:
        await s.close()


async def test_transition_tools_prompts_and_end(cfg, requests_seen):
    g = Graph.from_definition("Mini", mini_definition(speech="One moment."), {"business_name": "Riverside"})
    s, _ = await opened(cfg, g)
    try:
        events = await s.send("where do I park my car? parking question", timeout=20)
        moves = [e["node_name"] for e in kinds(events, "node_transition")]
        assert moves == ["Help"]
        started = kinds(events, "tool_call_started")
        assert [e["function_name"] for e in started] == ["parking_question"]
        assert kinds(events, "tool_call_result")[0]["result"]["next_step"] == "Help"
        said = [e["text"] for e in kinds(events, "bot_speech")]
        assert said[0] == "One moment." and said[1].startswith("[test model]")
        # The request that answered in Help carried Help's prompt and only Help's routes.
        last = [r for r in requests_seen if r["path"].endswith("/chat/completions")][-1]
        system = " ".join(m["content"] for m in last["messages"] if m["role"] == "system")
        assert "Answer questions about parking." in system and "You are Ava at Riverside." in system
        assert [t["function"]["name"] for t in last["tools"]] == ["goodbye"]

        events = await s.send("thanks, goodbye", timeout=20)
        assert [e["node_name"] for e in kinds(events, "node_transition")] == ["Bye"]
        assert kinds(events, "call_ended")[0]["reason"] == "end step reached"
        assert s.flow.ended and s.finished.is_set()
        assert s.summary()["steps"] == ["Start", "Help", "Bye"]
        with pytest.raises(RuntimeError, match="ended"):
            await s.send("hello?")
    finally:
        await s.close()


async def test_extraction_runs_when_leaving_a_step(cfg, requests_seen):
    g = Graph.from_definition("Mini", mini_definition(extraction=True))
    s, _ = await opened(cfg, g)
    try:
        events = await s.send("I'm Sam and I have a parking question", timeout=20)
        values = kinds(events, "variables")[0]["values"]
        assert values["is_new"] is True and "parking" in values["caller_name"]
        assert s.flow.variables["is_new"] is True
        structured = [r for r in requests_seen if r.get("response_format")]
        assert structured and "caller_name" in str(structured[0]["response_format"])
    finally:
        await s.close()


async def test_front_desk_template_end_to_end(cfg):
    g = Graph.from_folder(TEMPLATES / "front-desk")
    s, _ = await opened(cfg, g)
    try:
        await s.send("I need to move my appointment to Tuesday", timeout=20)
        assert s.summary()["steps"] == ["Greeting", "Router", "Appointments"]
    finally:
        await s.close()


async def test_close_is_idempotent_and_reports_disconnect(cfg):
    g = Graph.from_definition("Mini", mini_definition())
    s, _ = await opened(cfg, g)
    await s.close()
    await s.close()
    assert s.finished.is_set()
    assert s.events[-1]["type"] == "call_ended" and s.events[-1]["reason"] == "disconnected"


async def test_llm_errors_are_reported_not_hung(tmp_path):
    from vpengine.services import Config
    cfg = Config(llm="openai", model="x", base_url="http://127.0.0.1:9/v1")   # nothing listens there
    g = Graph.from_definition("Mini", mini_definition())
    s, _ = await opened(cfg, g)
    try:
        events = await asyncio.wait_for(s.send("parking?", timeout=30), 40)
        assert kinds(events, "execution_error")
    finally:
        await s.close()
