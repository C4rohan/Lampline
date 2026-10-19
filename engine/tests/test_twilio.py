"""Phone calls: a simulated Twilio drives the voice webhook and media stream."""

import base64
import json
import re
import time
from urllib.parse import urlencode

import pytest
from starlette.testclient import TestClient

import voicefakes as vf
from vpengine.server import Engine, create_app
from vpengine.twilio import StreamTokens, signature

PUBLIC = "https://calls.example.com"
TOKEN = "test-auth-token"


@pytest.fixture
def engine(cfg, agents_dir):
    return Engine(agents_dir, cfg, public_url=PUBLIC, twilio_auth_token=TOKEN, twilio_hang_up=False,
                  speech_factory=lambda: (vf.FakeSTT(), vf.FakeTTS()))


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine), base_url=PUBLIC) as c:
        yield c


def ring(client, agent="front-desk", call_sid="CA123", token=TOKEN, signed=True):
    """What Twilio sends when someone dials the number."""
    form = {"CallSid": call_sid, "AccountSid": "AC1", "From": "+15550100", "To": "+15550199"}
    path = f"/v1/twilio/voice?agent={agent}"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if signed:
        headers["X-Twilio-Signature"] = signature(token, PUBLIC + path, form)
    return client.post(path, content=urlencode(form), headers=headers)


def stream_token(twiml: str) -> str:
    assert '<Stream url="wss://calls.example.com/v1/twilio/stream">' in twiml
    return re.search(r'name="t" value="([^"]+)"', twiml).group(1)


def start_msg(token, call_sid="CA123"):
    return {"event": "start", "sequenceNumber": "1", "streamSid": "MZ1",
            "start": {"streamSid": "MZ1", "callSid": call_sid, "accountSid": "AC1", "tracks": ["inbound"],
                      "mediaFormat": {"encoding": "audio/x-mulaw", "sampleRate": 8000, "channels": 1},
                      "customParameters": {"t": token}}}


def test_signature_matches_twilios_own_library():
    # Expected values computed with twilio.request_validator.RequestValidator("12345").
    params = {"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234",
              "From": "+12349013030", "To": "+18005551212"}
    assert signature("12345", "https://mycompany.com/myapp.php?foo=1&bar=2", params) == "0/KCTR6DLpKmkAf8muzZqo1nDgQ="
    form = {"CallSid": "CA123", "AccountSid": "AC1", "From": "+15550100", "To": "+15550199"}
    assert signature("12345", "https://calls.example.com/v1/twilio/voice?agent=front-desk",
                     form) == "lAWiO7vyd9ICUuazof06N8nGRaw="


def test_voice_webhook_answers_with_a_stream(client):
    r = ring(client)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert stream_token(r.text)


def test_unsigned_or_forged_requests_are_refused(client):
    assert ring(client, signed=False).status_code == 403
    assert ring(client, token="someone-elses-token").status_code == 403


def test_unknown_agent_gets_a_polite_hangup(client):
    r = ring(client, agent="nope")
    assert r.status_code == 200 and "<Hangup/>" in r.text and "<Stream" not in r.text


def test_not_configured(cfg, agents_dir):
    with TestClient(create_app(Engine(agents_dir, cfg)), base_url="http://localhost") as c:
        assert ring(c).status_code == 503


def test_stream_without_a_valid_token_is_closed(client):
    with client.websocket_connect("/v1/twilio/stream") as ws:
        ws.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        ws.send_text(json.dumps(start_msg("made-up")))
        with pytest.raises(Exception):
            ws.receive_text()


def test_phone_call_greets_the_caller(client, engine):
    token = stream_token(ring(client).text)
    with client.websocket_connect("/v1/twilio/stream") as ws:
        ws.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
        ws.send_text(json.dumps(start_msg(token)))
        media = None
        deadline = time.time() + 20
        while time.time() < deadline:
            msg = json.loads(ws.receive_text())
            if msg.get("event") == "media":
                media = msg
                break
        assert media and media["streamSid"] == "MZ1" and base64.b64decode(media["media"]["payload"])
        session = next(iter(engine.sessions.values()))
        assert session.events[1]["text"].startswith("Thanks for calling Riverside Clinic")
        assert session.audio_in_rate == 8000
    deadline = time.time() + 10
    while not session.finished.is_set() and time.time() < deadline:
        time.sleep(0.05)
    assert session.finished.is_set()


def test_stream_tokens_are_single_use_and_expire():
    tokens = StreamTokens(ttl=0.05)
    t = tokens.issue("front-desk", "CA1")
    assert tokens.redeem(t, "CA2") is None                # wrong call
    t = tokens.issue("front-desk", "CA1")
    assert tokens.redeem(t, "CA1").agent == "front-desk"
    assert tokens.redeem(t, "CA1") is None                # used
    t = tokens.issue("front-desk", "CA1")
    time.sleep(0.1)
    assert tokens.redeem(t, "CA1") is None                # expired
