"""Speech to speech (experimental): a call driven by an OpenAI Realtime-style model."""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import time

import pytest
from starlette.testclient import TestClient

from fake_realtime import FakeRealtime
from vpengine import realtime
from vpengine.server import Engine, create_app
from vpengine.services import Config, ConfigError

SECRET, PUBLIC = "wh_secret_for_tests_only", "https://calls.example.com"


def _ring(client, agent="front-desk"):
    body = json.dumps({"call_sid": "jz-7", "direction": "inbound", "from": "+15550100", "to": "+15550199"},
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


def test_a_phone_call_runs_on_the_speech_to_speech_model(cfg, agents_dir, tmp_path):
    fake = FakeRealtime()
    c = dataclasses.replace(cfg, jambonz_webhook_secret=SECRET, voice_mode="speech-to-speech", realtime_url=fake.url,
                            realtime_key="test-key-123456")
    engine = Engine(agents_dir, c, public_url=PUBLIC, twilio_hang_up=True, data_dir=tmp_path / "data")
    graph = engine.load_agent("front-desk")
    root = graph.start                                   # Start greets, then listens; its route moves the call on
    route = root.routes[0]
    fake.caller_turn = ("I'd like to book a cleaning.", route.tool_name)
    assert engine.speech_to_speech(graph) == (True, None)
    with TestClient(create_app(engine), base_url=PUBLIC) as client:
        token = _ring(client).json()[0]["url"].split("?t=", 1)[1]
        with client.websocket_connect(f"/v1/jambonz/stream?t={token}", subprotocols=["audio.jambonz.org"]) as ws:
            ws.send_text(json.dumps({"callSid": "jz-7", "sampleRate": 8000, "metadata": {"t": token}}))
            got = None
            end = time.time() + 15
            while time.time() < end and got is None:
                got = ws.receive().get("bytes")
            assert got, "the model's greeting should reach the caller as phone audio"
            session = next(iter(engine.sessions.values()))
            assert isinstance(session, realtime.RealtimeVoiceSession)
            for _ in range(40):                                          # the caller talks
                ws.send_bytes(b"\x00\x00" * 160)
            assert _wait(lambda: any(e["type"] == "node_transition" and e["node_name"] == graph.steps[route.target].name
                                     for e in session.events))
            assert _wait(lambda: len(fake.of("response.create")) >= 2)
        updates = [((e.get("session") or {}).get("instructions") or "") for e in fake.of("session.update")]
        assert any(graph.steps[route.target].prompt[:40] in u for u in updates if graph.steps[route.target].prompt)
        greeting = (fake.of("response.create")[0].get("response") or {}).get("instructions", "")
        assert "Say exactly this" in greeting and "Thanks for calling" in greeting
        assert any(e["type"] == "user_speech" and "cleaning" in e["text"] for e in session.events)
        assert any(e["type"] == "bot_speech" and "Thanks for calling" in e["text"] for e in session.events)
        outputs = [e for e in fake.of("conversation.item.create") if (e.get("item") or {}).get("type") == "function_call_output"]
        assert outputs and "next_step" in outputs[0]["item"]["output"]


def test_scripted_agents_and_missing_keys_keep_the_chain(cfg):
    from vpengine.graph import Graph
    plain = Graph.from_definition("x", {"nodes": [
        {"id": "1", "type": "startCall", "data": {"name": "Start", "greeting": "Hi"}},
        {"id": "2", "type": "agentNode", "data": {"name": "Form", "mode": "scripted", "say": "Name?"}}],
        "edges": [{"id": "e", "source": "1", "target": "2", "data": {"label": "go", "condition": "always"}}]})
    c = dataclasses.replace(cfg, voice_mode="speech-to-speech", openai_key="")
    assert "API key" in realtime.problem(plain, c)
    c = dataclasses.replace(c, openai_key="sk-test-123")
    assert "scripted pages" in realtime.problem(plain, c)
    assert realtime.problem(plain, dataclasses.replace(c, realtime_url="ws://gpu-box:8000/v1/realtime")) is not None


def test_settings_validation():
    from vpengine.settings import SettingsStore
    import tempfile, pathlib
    s = SettingsStore(pathlib.Path(tempfile.mkdtemp()) / "s.json", env={})
    with pytest.raises(ConfigError, match="ws"):
        s.update({"realtime_url": "http://api.openai.com/v1/realtime"})
    cfg = s.update({"voice_mode": "speech-to-speech", "realtime_url": "wss://api.openai.com/v1/realtime"})
    assert cfg.voice_mode == "speech-to-speech" and Config(openai_key="k").realtime_api_key == "k"
    assert Config(openai_key="k", realtime_url="ws://box:8000/v1/realtime").realtime_api_key == ""


def test_without_a_greeting_the_model_speaks_first_and_nothing_is_replayed(cfg, agents_dir, tmp_path):
    """Found in review: entering a step with no tool result (no greeting) left the model silent; and at the
    first tool result Pipecat replayed the whole conversation as one message."""
    from vk import agentfs
    a = agentfs.load(agents_dir / "front-desk")
    start = next(n for n in a.definition["nodes"] if n["type"] == "startCall")
    start["data"]["greeting"] = ""
    agentfs.write(a, agents_dir / "front-desk")
    fake = FakeRealtime(reply="Hi, how can I help?")
    c = dataclasses.replace(cfg, jambonz_webhook_secret=SECRET, voice_mode="speech-to-speech", realtime_url=fake.url,
                            realtime_key="test-key-123456")
    engine = Engine(agents_dir, c, public_url=PUBLIC, twilio_hang_up=True, data_dir=tmp_path / "data")
    graph = engine.load_agent("front-desk")
    fake.caller_turn = ("I'd like to book a cleaning.", graph.start.routes[0].tool_name)
    with TestClient(create_app(engine), base_url=PUBLIC) as client:
        token = _ring(client).json()[0]["url"].split("?t=", 1)[1]
        with client.websocket_connect(f"/v1/jambonz/stream?t={token}", subprotocols=["audio.jambonz.org"]) as ws:
            ws.send_text(json.dumps({"callSid": "jz-7", "sampleRate": 8000, "metadata": {"t": token}}))
            got = None
            end = time.time() + 15
            while time.time() < end and got is None:
                got = ws.receive().get("bytes")
            assert got, "the model should open the call itself"
            for _ in range(40):
                ws.send_bytes(b"\x00\x00" * 160)
            assert _wait(lambda: len(fake.of("response.create")) >= 2)
        first = fake.of("response.create")[0]
        assert "Say exactly" not in json.dumps(first)                     # the model's own turn, not a scripted line
        replayed = [e for e in fake.of("conversation.item.create") if (e.get("item") or {}).get("type") == "message"]
        assert replayed == []
        assert all("tools" in (e.get("session") or {}) for e in fake.of("session.update")[1:])   # once a context exists


async def test_a_step_without_tools_clears_them():
    from pipecat.adapters.schemas.tools_schema import ToolsSchema
    from pipecat.processors.aggregators.llm_context import LLMContext
    llm = realtime.make_realtime_llm(Config(openai_key="k"))
    sent = []

    async def capture(raw):
        sent.append(raw)
    llm._ws_send = capture
    ctx = LLMContext()
    ctx.set_tools(ToolsSchema(standard_tools=[]))
    llm._context = ctx
    await llm._send_session_update()
    assert sent[-1]["session"]["tools"] == []


END_DEF = {"nodes": [
    {"id": "1", "type": "startCall", "data": {"name": "Start", "prompt": "Greet.", "greeting": "Hi there."}},
    {"id": "3", "type": "endCall", "data": {"name": "Bye", "prompt": "Say goodbye.", "transfer_to": "+15551234567"}}],
    "edges": [{"id": "b", "source": "1", "target": "3",
               "data": {"label": "goodbye", "condition": "caller is finished", "transition_speech": "Connecting you now."}}]}


def _goodbye_then_transfer(cfg, tmp_path, speak_delay=0.0, turn_done_delay=0.0):
    from conftest import write_agent
    fake = FakeRealtime(reply="Goodbye, take care.")
    fake.speak_delay, fake.turn_done_delay = speak_delay, turn_done_delay
    d = tmp_path / "agents"
    d.mkdir()
    write_agent(d / "mini", END_DEF)
    c = dataclasses.replace(cfg, jambonz_webhook_secret=SECRET, voice_mode="speech-to-speech", realtime_url=fake.url,
                            realtime_key="test-key-123456")
    engine = Engine(d, c, public_url=PUBLIC, twilio_hang_up=True, data_dir=tmp_path / "data")
    fake.caller_turn = ("that's all", engine.load_agent("mini").start.routes[0].tool_name)
    with TestClient(create_app(engine), base_url=PUBLIC) as client:
        token = _ring(client, "mini").json()[0]["url"].split("?t=", 1)[1]
        with client.websocket_connect(f"/v1/jambonz/stream?t={token}", subprotocols=["audio.jambonz.org"]) as ws:
            ws.send_text(json.dumps({"callSid": "jz-7", "sampleRate": 8000, "metadata": {"t": token}}))
            time.sleep(1 + speak_delay)
            for _ in range(40):
                try:
                    ws.send_bytes(b"\x00\x00" * 160)
                except Exception:  # noqa: BLE001 - the call may have ended
                    break
            session = next(iter(engine.sessions.values()))
            assert _wait(lambda: any(e["type"] == "call_ended" for e in session.events), 15)
    said = [e["text"] for e in session.events if e["type"] == "bot_speech"]
    order = [e["type"] for e in session.events if e["type"] in ("bot_speech", "transfer", "call_ended")]
    return fake, said, order


def test_the_goodbye_plays_before_the_transfer(cfg, tmp_path):
    """Found in review: a transition's line into an end step ended the call before the end step's own reply."""
    fake, said, order = _goodbye_then_transfer(cfg, tmp_path)
    assert said == ["Hi there.", "Connecting you now.", "Goodbye, take care."]
    assert order[-2:] == ["transfer", "call_ended"]
    kinds = [(e.get("response") or {}).get("instructions", "(model)") for e in fake.of("response.create")]
    assert kinds[-1] == "(model)" and "Connecting you now." in kinds[-2]


def test_a_late_done_for_the_callers_turn_doesnt_end_the_call_early(cfg, tmp_path):
    """Found on CI: the server's response.done for the caller's turn (the route call) arrived after the
    transition's line had started. It freed the queue, the goodbye started on top of the line, and the
    line was taken for the goodbye, so the call ended before the goodbye."""
    fake, said, order = _goodbye_then_transfer(cfg, tmp_path, speak_delay=0.3, turn_done_delay=0.15)
    assert said == ["Hi there.", "Connecting you now.", "Goodbye, take care."]
    assert order[-2:] == ["transfer", "call_ended"]
