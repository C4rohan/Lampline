"""Phone calls over SIP: a simulated jambonz (signed calling webhook + listen websocket)."""

import hashlib
import hmac
import json
import time

import pytest
from starlette.testclient import TestClient

import voicefakes as vf
from vpengine import jambonz
from vpengine.server import Engine, create_app

PUBLIC = "https://calls.example.com"
SECRET = "wh_secret_for_tests_only"
CALL = {"call_sid": "jz-1", "direction": "inbound", "from": "+15550100", "to": "+15550199",
        "account_sid": "acct", "application_sid": "app", "call_status": "trying"}


def sign(body: bytes, ts: int | None = None, secret: str = SECRET) -> dict:
    ts = str(ts if ts is not None else int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return {"Jambonz-Signature": f"t={ts},v1={sig}"}


@pytest.fixture
def engine(cfg, agents_dir):
    from dataclasses import replace
    return Engine(agents_dir, replace(cfg, jambonz_webhook_secret=SECRET), public_url=PUBLIC, twilio_hang_up=True,
                  speech_factory=lambda: (vf.FakeSTT(), vf.FakeTTS()))


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine), base_url=PUBLIC) as c:
        yield c


def ring(client, agent="front-desk", headers=None):
    body = json.dumps(CALL, separators=(",", ":")).encode()
    h = {"Content-Type": "application/json", **(sign(body) if headers is None else headers)}
    return client.post(f"/v1/jambonz/call?agent={agent}", content=body, headers=h)


def test_signature_matches_jambonz_sdk():
    body = b'{"call_sid":"1"}'
    good = sign(body)["Jambonz-Signature"]
    assert jambonz.valid_signature(SECRET, good, body)
    assert jambonz.valid_signature(SECRET, good, b'{"call_sid": "1"}')      # re-serialised like the SDK does
    assert not jambonz.valid_signature(SECRET, good, b'{"call_sid":"2"}')
    assert not jambonz.valid_signature("other", good, body)
    assert not jambonz.valid_signature(SECRET, sign(body, ts=int(time.time()) - 900)["Jambonz-Signature"], body)
    assert not jambonz.valid_signature(SECRET, "t=abc,v1=00", body)
    assert not jambonz.valid_signature(SECRET, "", body)


def test_calling_webhook_answers_with_a_listen_stream(client):
    r = ring(client)
    assert r.status_code == 200, r.text
    listen, hangup = r.json()
    assert listen["verb"] == "listen" and hangup == {"verb": "hangup"}
    assert listen["url"].startswith("wss://calls.example.com/v1/jambonz/stream?t=")
    assert listen["bidirectionalAudio"] == {"enabled": True, "streaming": True, "sampleRate": 8000}
    assert listen["actionHook"] == f"{PUBLIC}/v1/jambonz/done" and listen["passDtmf"] is True


def test_forged_or_unsigned_requests_are_refused(client):
    assert ring(client, headers={}).status_code == 403
    assert ring(client, headers=sign(b"something else")).status_code == 403
    assert ring(client, headers=sign(json.dumps(CALL).encode(), secret="wrong")).status_code == 403


def test_unknown_agent_gets_a_polite_hangup(client):
    verbs = ring(client, agent="nope").json()
    assert [v["verb"] for v in verbs] == ["say", "hangup"]


def test_not_configured(cfg, agents_dir):
    with TestClient(create_app(Engine(agents_dir, cfg, public_url=PUBLIC)), base_url=PUBLIC) as c:
        assert c.post("/v1/jambonz/call?agent=front-desk", json=CALL).status_code == 503


def test_action_hook_hangs_up(client):
    body = b'{"call_sid":"jz-1","listen_status":"completed"}'
    r = client.post("/v1/jambonz/done", content=body, headers={"Content-Type": "application/json", **sign(body)})
    assert r.json() == [{"verb": "hangup"}]
    assert client.post("/v1/jambonz/done", content=body, headers={"Content-Type": "application/json"}).status_code == 403


def test_phone_call_greets_the_caller_and_echoes_the_subprotocol(client, engine):
    url = ring(client).json()[0]["url"]
    token = url.split("?t=", 1)[1]
    hello = {"callSid": "jz-1", "from": "+15550100", "to": "+15550199", "sampleRate": 8000, "mixType": "mono",
             "metadata": {"t": token}}
    with client.websocket_connect(f"/v1/jambonz/stream?t={token}", subprotocols=["audio.jambonz.org"]) as ws:
        assert ws.accepted_subprotocol == "audio.jambonz.org"
        ws.send_text(json.dumps(hello))
        ws.send_bytes(b"\x00\x00" * 160)                       # 20 ms of caller silence
        audio = None
        deadline = time.time() + 20
        while time.time() < deadline:
            msg = ws.receive()
            if msg.get("bytes"):
                audio = msg["bytes"]
                break
        assert audio and len(audio) % 2 == 0
        session = next(iter(engine.sessions.values()))
        assert session.events[1]["text"].startswith("Thanks for calling Riverside Clinic")
    with client.websocket_connect(f"/v1/jambonz/stream?t={token}") as ws:   # the token was single use
        ws.send_text(json.dumps(hello))
        with pytest.raises(Exception):
            ws.receive_bytes()


async def test_serializer_maps_frames():
    from pipecat.frames.frames import EndFrame, InputDTMFFrame, InterruptionFrame, OutputAudioRawFrame
    from pipecat.processors.frame_processor import FrameProcessorSetup

    s = jambonz.JambonzFrameSerializer()
    setup = FrameProcessorSetup.__new__(FrameProcessorSetup)
    setup.audio_in_sample_rate = 8000
    await s.setup(setup)
    assert json.loads(await s.serialize(InterruptionFrame())) == {"type": "killAudio"}
    out = await s.serialize(OutputAudioRawFrame(audio=b"\x01\x00" * 160, sample_rate=8000, num_channels=1))
    assert isinstance(out, bytes) and len(out) == 320
    assert json.loads(await s.serialize(EndFrame())) == {"type": "disconnect"}
    assert await s.serialize(EndFrame()) is None                                  # only once
    dtmf = await s.deserialize(json.dumps({"event": "dtmf", "dtmf": "5", "duration": "1600"}))
    assert isinstance(dtmf, InputDTMFFrame) and dtmf.button.value == "5"
    assert await s.deserialize(json.dumps({"type": "playDone"})) is None
    frame = await s.deserialize(b"\x00\x00" * 160)
    assert frame.sample_rate == 8000 and len(frame.audio) == 320


def test_hd_voice_asks_jambonz_for_16_khz(cfg, agents_dir):
    from dataclasses import replace
    eng = Engine(agents_dir, replace(cfg, jambonz_webhook_secret=SECRET, jambonz_sample_rate="16000"),
                 public_url=PUBLIC, twilio_hang_up=True, speech_factory=lambda: (vf.FakeSTT(), vf.FakeTTS()))
    with TestClient(create_app(eng), base_url=PUBLIC) as c:
        listen = ring(c).json()[0]
        assert listen["sampleRate"] == 16000 and listen["bidirectionalAudio"]["sampleRate"] == 16000
        token = listen["url"].split("?t=", 1)[1]
        with c.websocket_connect(f"/v1/jambonz/stream?t={token}", subprotocols=["audio.jambonz.org"]) as ws:
            ws.send_text(json.dumps({"callSid": "jz-1", "sampleRate": 16000, "metadata": {"t": token}}))
            ws.send_bytes(b"\x00\x00" * 320)                   # 20 ms at 16 kHz
            deadline = time.time() + 20
            while time.time() < deadline and not ws.receive().get("bytes"):
                pass
            session = next(iter(eng.sessions.values()))
            assert (session.audio_in_rate, session.audio_out_rate) == (16000, 16000)


def test_odd_rates_fall_back_to_phone_audio():
    assert jambonz.listen_verbs("wss://x/s", "t", "https://x/d", 44100)[0]["sampleRate"] == 8000
    from vpengine.services import Config, ConfigError
    with pytest.raises(ConfigError, match="VPE_JAMBONZ_SAMPLE_RATE"):
        Config.load({"VPE_JAMBONZ_SAMPLE_RATE": "48000"})
