"""Speech to speech on Gemini Live: a call driven step by step, with a stand-in for Google's service."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import time

import pytest
from starlette.testclient import TestClient

from fake_gemini_live import FakeGeminiLive
from vpengine import gemini_live, realtime
from vpengine.server import Engine, create_app
from vpengine.services import Config, ConfigError

SECRET, PUBLIC = "wh_secret_for_tests_only", "https://calls.example.com"


def _ring(client, agent="front-desk"):
    body = json.dumps({"call_sid": "jz-8", "direction": "inbound", "from": "+15550100", "to": "+15550199"},
                      separators=(",", ":")).encode()
    ts = str(int(time.time()))
    sig = hmac.new(SECRET.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return client.post(f"/v1/jambonz/call?agent={agent}", content=body,
                       headers={"Content-Type": "application/json", "Jambonz-Signature": f"t={ts},v1={sig}"})


def _wait(check, seconds=10.0):
    end = time.time() + seconds
    while time.time() < end:
        if check():
            return True
        time.sleep(0.05)
    return False


def _engine(cfg, agents_dir, tmp_path, fake, monkeypatch):
    monkeypatch.setattr(gemini_live.LamplineGeminiLive, "create_client", lambda self: setattr(self, "_client", fake))
    c = dataclasses.replace(cfg, jambonz_webhook_secret=SECRET, voice_mode="speech-to-speech", realtime_provider="gemini",
                            gemini_key="test-gemini-key-123", gemini_live_voice="Kore")
    return Engine(agents_dir, c, public_url=PUBLIC, twilio_hang_up=True, data_dir=tmp_path / "data")


def test_settings_and_problems():
    from vpengine.graph import Graph
    plain = Graph.from_definition("x", {"nodes": [{"id": "1", "type": "startCall", "data": {"name": "Start", "greeting": "Hi"}}],
                                        "edges": []})
    c = Config(voice_mode="speech-to-speech", realtime_provider="gemini")
    assert "Gemini API key" in realtime.problem(plain, c)
    assert realtime.problem(plain, dataclasses.replace(c, gemini_key="k")) is None
    llm = realtime.make_realtime_llm(dataclasses.replace(c, gemini_key="k", gemini_live_model="gemini-live-test",
                                                         gemini_live_voice="Puck"))
    assert isinstance(llm, gemini_live.LamplineGeminiLive)
    assert llm._settings.model == "models/gemini-live-test" and llm._settings.voice == "Puck"
    assert Config.load(env={}, saved={"realtime_provider": "gemini"}).realtime_provider == "gemini"
    with pytest.raises(ConfigError):
        Config.load(env={}, saved={"gemini_live_voice": "Puck; rm"})
    with pytest.raises(ConfigError):
        Config.load(env={}, saved={"realtime_provider": "somebody-else"})


def test_a_phone_call_runs_on_gemini_live(cfg, agents_dir, tmp_path, monkeypatch):
    fake = FakeGeminiLive()
    engine = _engine(cfg, agents_dir, tmp_path, fake, monkeypatch)
    graph = engine.load_agent("front-desk")
    route = graph.start.routes[0]
    target = graph.steps[route.target]
    fake.caller_turn = ("I'd like to book a cleaning.", route.tool_name)
    assert engine.speech_to_speech(graph) == (True, None)
    with TestClient(create_app(engine), base_url=PUBLIC) as client:
        token = _ring(client).json()[0]["url"].split("?t=", 1)[1]
        with client.websocket_connect(f"/v1/jambonz/stream?t={token}", subprotocols=["audio.jambonz.org"]) as ws:
            ws.send_text(json.dumps({"callSid": "jz-8", "sampleRate": 8000, "metadata": {"t": token}}))
            got = None
            end = time.time() + 15
            while time.time() < end and got is None:
                got = ws.receive().get("bytes")
            assert got, "the greeting should reach the caller as phone audio"
            session = next(iter(engine.sessions.values()))
            assert isinstance(session, realtime.RealtimeVoiceSession)
            for _ in range(40):                                          # the caller talks
                ws.send_bytes(b"\x00\x00" * 160)
            assert _wait(lambda: any(e["type"] == "node_transition" and e["node_name"] == target.name for e in session.events))
            assert _wait(lambda: fake.tool_responses)
        # one connect, with the first step's instructions, how steps work, every step's tools, and the chosen voice
        assert len(fake.configs) == 1
        config = fake.configs[0]
        instruction = str(config.system_instruction)
        assert gemini_live.STEPS[:40] in instruction
        get = lambda o, k: o.get(k) if isinstance(o, dict) else getattr(o, k, None)       # noqa: E731
        names = {get(f, "name") for t in config.tools for f in (get(t, "function_declarations") or [])}
        assert route.tool_name in names
        assert config.generation_config.speech_config.voice_config.prebuilt_voice_config.voice_name == "Kore"
        # the greeting was asked for word for word, as a note that isn't from the caller
        said, complete = fake.turns[0]
        assert "Say exactly this" in said and "Thanks for calling" in said and gemini_live.NOTE in said and complete
        # the route's result carried the next step's instructions, so the model went on in the right step
        first = fake.tool_responses[0]
        assert first["name"] == route.tool_name and first["response"]["next_step"] == target.name
        assert f'"{target.name}"' in first["response"]["instructions"]
        if target.prompt:
            assert target.prompt[:30] in first["response"]["instructions"]
        assert any(e["type"] == "user_speech" and "cleaning" in e["text"] for e in session.events)
        assert any(e["type"] == "bot_speech" and "Thanks for calling" in e["text"] for e in session.events)


def test_without_a_greeting_the_model_speaks_first(cfg, agents_dir, tmp_path, monkeypatch):
    from vk import agentfs
    a = agentfs.load(agents_dir / "front-desk")
    start = next(n for n in a.definition["nodes"] if n["type"] == "startCall")
    start["data"]["greeting"] = ""
    agentfs.write(a, agents_dir / "front-desk")
    fake = FakeGeminiLive(reply="Hi, how can I help?")
    engine = _engine(cfg, agents_dir, tmp_path, fake, monkeypatch)
    with TestClient(create_app(engine), base_url=PUBLIC) as client:
        token = _ring(client).json()[0]["url"].split("?t=", 1)[1]
        with client.websocket_connect(f"/v1/jambonz/stream?t={token}", subprotocols=["audio.jambonz.org"]) as ws:
            ws.send_text(json.dumps({"callSid": "jz-8", "sampleRate": 8000, "metadata": {"t": token}}))
            got = None
            end = time.time() + 15
            while time.time() < end and got is None:
                got = ws.receive().get("bytes")
            assert got, "the model should open the call itself"
    said, complete = fake.turns[0]
    assert "Say exactly" not in said and complete and gemini_live.NOTE in said


async def test_step_notes_without_a_route_and_routes_alone(monkeypatch):
    """A step entered by a rule gets a note (answered only when the flow asks); a route whose step never
    changes goes out on its own after a moment."""
    from pipecat.adapters.schemas.function_schema import FunctionSchema
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.processors.aggregators.llm_context import LLMContext

    monkeypatch.setattr(gemini_live, "ROUTE_WAIT", 0.05)
    fake = FakeGeminiLive()
    llm = gemini_live.LamplineGeminiLive(api_key="k")
    llm._client = fake

    class Step:
        def __init__(self, name):
            self.name = name

    class Flow:
        context = LLMContext()
        current = Step("Help")
        system_prompt = "Answer questions about parking."

    flow = Flow()
    flow.context.set_tools(ToolsSchema(standard_tools=[FunctionSchema(name="to_bye", description="done", properties={}, required=[])]))
    async with fake.connect(model="m", config=None) as session:
        llm._session, llm._lp_started, llm._lp_flow, llm._lp_applied = session, True, flow, "Help"
        flow.current, flow.system_prompt = Step("Bye"), "Say goodbye."
        await llm._lp_step_changed()                                   # entered by a rule; nobody asked it to speak
        text, complete = fake.turns[-1]
        assert '"Bye"' in text and "Say goodbye." in text and "to_bye" in text and not complete
        llm.lp_run_pending = True
        flow.current, flow.system_prompt = Step("Other"), "Something else."
        await llm._lp_step_changed()                                   # this time the flow wants it to go on
        assert fake.turns[-1][1] is True and '"Other"' in fake.turns[-1][0]
        llm.lp_busy = False
        await llm._tool_result("fc_9", "to_bye", {"status": "ok", "next_step": "Other"})
        import asyncio
        await asyncio.sleep(0.2)
        assert fake.tool_responses[-1] == {"id": "fc_9", "name": "to_bye", "response": {"status": "ok", "next_step": "Other"}}
        await llm._tool_result("fc_10", "lookup", {"ok": True, "result": "3 spaces"})   # an ordinary tool: straight through
        assert fake.tool_responses[-1]["id"] == "fc_10"
