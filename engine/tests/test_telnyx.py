"""Phone calls: a simulated Telnyx (signed TeXML webhook + media stream)."""

import base64
import json
import re
import time
from urllib.parse import urlencode

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from starlette.testclient import TestClient

import voicefakes as vf
from vpengine import telnyx
from vpengine.server import Engine, create_app

PUBLIC = "https://calls.example.com"
SIGNER = Ed25519PrivateKey.generate()
PUBKEY = base64.b64encode(SIGNER.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()


def sign(body: bytes, ts: int | None = None, key=SIGNER) -> dict:
    ts = str(ts if ts is not None else int(time.time()))
    sig = base64.b64encode(key.sign(f"{ts}|".encode() + body)).decode()
    return {"telnyx-timestamp": ts, "telnyx-signature-ed25519": sig}


@pytest.fixture
def engine(cfg, agents_dir):
    from dataclasses import replace
    e = Engine(agents_dir, replace(cfg, telnyx_public_key=PUBKEY), public_url=PUBLIC, twilio_hang_up=False,
               speech_factory=lambda: (vf.FakeSTT(), vf.FakeTTS()))
    return e


@pytest.fixture
def client(engine):
    with TestClient(create_app(engine), base_url=PUBLIC) as c:
        yield c


def ring(client, agent="front-desk", headers=None):
    body = urlencode({"CallSid": "v3:abc", "From": "+15550100", "To": "+15550199"}).encode()
    h = {"Content-Type": "application/x-www-form-urlencoded", **(sign(body) if headers is None else headers)}
    return client.post(f"/v1/telnyx/voice?agent={agent}", content=body, headers=h)


def test_signature_check():
    body = b"CallSid=1"
    good = sign(body)
    assert telnyx.valid_signature(PUBKEY, good["telnyx-timestamp"], good["telnyx-signature-ed25519"], body)
    assert not telnyx.valid_signature(PUBKEY, good["telnyx-timestamp"], good["telnyx-signature-ed25519"], b"CallSid=2")
    old = sign(body, ts=int(time.time()) - 600)
    assert not telnyx.valid_signature(PUBKEY, old["telnyx-timestamp"], old["telnyx-signature-ed25519"], body)
    other = sign(body, key=Ed25519PrivateKey.generate())
    assert not telnyx.valid_signature(PUBKEY, other["telnyx-timestamp"], other["telnyx-signature-ed25519"], body)
    assert not telnyx.valid_signature(PUBKEY, "x", "not-base64!", body)


def test_voice_webhook_answers_with_a_bidirectional_stream(client):
    r = ring(client)
    assert r.status_code == 200, r.text
    assert 'bidirectionalMode="rtp"' in r.text and "wss://calls.example.com/v1/telnyx/stream?t=" in r.text


def test_forged_or_unsigned_requests_are_refused(client):
    assert ring(client, headers={}).status_code == 403
    assert ring(client, headers=sign(b"something else")).status_code == 403


def test_unknown_agent_gets_a_polite_hangup(client):
    r = ring(client, agent="nope")
    assert "<Hangup/>" in r.text and "<Stream" not in r.text


def test_phone_call_greets_the_caller(client, engine):
    token = re.search(r"\?t=([^\"&]+)", ring(client).text).group(1)
    start = {"event": "start", "sequence_number": "1", "stream_id": "st-1",
             "start": {"call_control_id": "v3:ctl", "from": "+15550100", "to": "+15550199",
                       "media_format": {"encoding": "PCMU", "sample_rate": 8000, "channels": 1}}}
    with client.websocket_connect(f"/v1/telnyx/stream?t={token}") as ws:
        ws.send_text(json.dumps({"event": "connected", "version": "1.0.0"}))
        ws.send_text(json.dumps(start))
        media = None
        deadline = time.time() + 20
        while time.time() < deadline:
            msg = json.loads(ws.receive_text())
            if msg.get("event") == "media":
                media = msg
                break
        assert media and base64.b64decode(media["media"]["payload"])
        session = next(iter(engine.sessions.values()))
        assert session.events[1]["text"].startswith("Thanks for calling Riverside Clinic")
    # the token was single use
    with client.websocket_connect(f"/v1/telnyx/stream?t={token}") as ws:
        ws.send_text(json.dumps(start))
        with pytest.raises(Exception):
            ws.receive_text()


def test_token_in_custom_parameters_is_found():
    assert telnyx.token_from_start({"custom_parameters": {"t": "abc"}}) == "abc"
    assert telnyx.token_from_start({"customParameters": [{"name": "t", "value": "xyz"}]}) == "xyz"
    assert telnyx.token_from_start({}) is None
