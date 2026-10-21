"""Call history: finished conversations kept on this computer, with optional recordings."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import wave

import httpx
import pytest
from starlette.testclient import TestClient

import voicefakes as vf
from vpengine.history import CallHistory
from vpengine.server import Engine, create_app


async def _wait(check, seconds=8.0):
    for _ in range(int(seconds / 0.05)):
        if check():
            return True
        await asyncio.sleep(0.05)
    return False


@pytest.fixture
async def c(cfg, agents_dir, tmp_path):
    engine = Engine(agents_dir, cfg, data_dir=tmp_path / "data")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost",
                                 timeout=30) as client:
        client.engine = engine
        yield client
    await engine.shutdown()


async def test_a_finished_chat_is_kept(c):
    r = (await c.post("/v1/chat/sessions", json={"agent": "front-desk", "context": {"caller_name": "Sam"}})).json()
    sid = r["session_id"]
    await c.post(f"/v1/chat/sessions/{sid}/messages", json={"text": "I'd like to book a cleaning"})
    await c.post(f"/v1/chat/sessions/{sid}/end")
    assert await _wait(lambda: c.engine.history.get(sid) is not None)
    listed = (await c.get("/v1/calls")).json()
    assert listed["keeping"] and listed["days"] == 30 and not listed["recordings"]
    row = next(x for x in listed["calls"] if x["id"] == sid)
    assert row["agent"] == "front-desk" and row["channel"] == "chat" and row["turns"] == 1
    assert row["collected"].get("caller_name") == "Sam" and "clinic_name" not in row["collected"]   # constants aren't details
    call = (await c.get(f"/v1/calls/{sid}")).json()
    assert call["transcript"][0]["who"] == "agent" and any(t["text"] == "I'd like to book a cleaning" for t in call["transcript"])
    assert all(e["type"] != "variables" for e in call["events"])
    assert (await c.get("/v1/calls", params={"q": "cleaning"})).json()["calls"][0]["id"] == sid
    assert (await c.get("/v1/calls", params={"q": "nothing-like-this"})).json()["calls"] == []
    assert (await c.get("/v1/calls", params={"agent": "appointment-scheduling"})).json()["calls"] == []
    assert (await c.get(f"/v1/calls/{sid}/recording")).status_code == 404
    assert (await c.delete(f"/v1/calls/{sid}")).json() == {"deleted": sid}
    assert (await c.get(f"/v1/calls/{sid}")).status_code == 404


async def test_history_off_keeps_nothing(cfg, agents_dir, tmp_path):
    engine = Engine(agents_dir, dataclasses.replace(cfg, call_history="off"), data_dir=tmp_path / "data")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost") as client:
        sid = (await client.post("/v1/chat/sessions", json={"agent": "front-desk"})).json()["session_id"]
        await client.post(f"/v1/chat/sessions/{sid}/end")
        await asyncio.sleep(0.3)
        assert (await client.get("/v1/calls")).json() == {"calls": [], "keeping": False, "days": 30, "recordings": False}
    await engine.shutdown()
    assert not (tmp_path / "data" / "calls.sqlite3").exists()


def test_prune_and_odd_ids(tmp_path):
    h = CallHistory(tmp_path)
    assert h.list() == [] and h.get("x") is None and h.recording_path("../../etc/passwd") is None
    h.db().execute("INSERT INTO calls (id, agent, started, ended, steps, collected, events) VALUES (?,?,?,?,?,?,?)",
                   ("0123456789abcdef", "a", time.time() - 40 * 86400, time.time() - 40 * 86400, "[]", "{}", "[]"))
    h.db().commit()
    h.save_recording("0123456789abcdef", b"\x00\x00" * 320, 16000, 2)
    assert h.list()[0]["recording"]
    assert h.prune(30) == 1 and h.list() == [] and not (tmp_path / "recordings" / "0123456789abcdef.wav").exists()
    assert h.save_recording("../evil", b"\x00\x00", 16000, 1) is None


def test_a_recorded_phone_call(cfg, agents_dir, tmp_path):
    """With recordings on, a jambonz call leaves a stereo WAV next to its history entry."""
    import hashlib
    import hmac
    secret, public = "wh_secret_for_tests_only", "https://calls.example.com"
    engine = Engine(agents_dir, dataclasses.replace(cfg, jambonz_webhook_secret=secret, call_recordings="on"),
                    public_url=public, twilio_hang_up=True, data_dir=tmp_path / "data",
                    speech_factory=lambda: (vf.FakeSTT(), vf.FakeTTS()))
    call = {"call_sid": "jz-9", "direction": "inbound", "from": "+15550100", "to": "+15550199"}
    body = json.dumps(call, separators=(",", ":")).encode()
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    with TestClient(create_app(engine), base_url=public) as client:
        r = client.post("/v1/jambonz/call?agent=front-desk", content=body,
                        headers={"Content-Type": "application/json", "Jambonz-Signature": f"t={ts},v1={sig}"})
        token = r.json()[0]["url"].split("?t=", 1)[1]
        with client.websocket_connect(f"/v1/jambonz/stream?t={token}", subprotocols=["audio.jambonz.org"]) as ws:
            ws.send_text(json.dumps({"callSid": "jz-9", "sampleRate": 8000, "metadata": {"t": token}}))
            deadline = time.time() + 20
            while time.time() < deadline:
                if ws.receive().get("bytes"):
                    break
            for _ in range(50):                                    # a second of the caller (the resampler keeps ~0.1 s)
                ws.send_bytes(b"\x10\x00" * 160)
            time.sleep(0.5)
            session = next(iter(engine.sessions.values()))
            assert session.record_to is not None
        deadline = time.time() + 15
        while time.time() < deadline and engine.history.get(session.id) is None:
            time.sleep(0.1)
        row = engine.history.get(session.id)
        assert row and row["channel"] == "phone" and row["contact"] == "+15550100"
        path = engine.history.recording_path(session.id)
        assert path is not None and row["recording"]
        with wave.open(str(path)) as w:
            assert w.getnchannels() == 2 and w.getframerate() == 16000 and w.getnframes() > 0
        audio = client.get(f"/v1/calls/{session.id}/recording", headers={"Host": "127.0.0.1"})   # on this computer
        assert audio.status_code == 200 and audio.headers["content-type"] == "audio/wav"
