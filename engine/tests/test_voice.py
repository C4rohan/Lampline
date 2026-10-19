import asyncio

import httpx
import pytest

import voicefakes as vf
from conftest import mini_definition
from vpengine.graph import Graph
from vpengine.server import Engine, create_app
from vpengine.session import VoiceSession


async def until(cond, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.05)


def names(session, kind, key):
    return [e.get(key) for e in session.events if e["type"] == kind]


@pytest.fixture
async def call(cfg):
    started = []

    async def make(definition=None, **kw):
        g = Graph.from_definition("Mini", definition or mini_definition(speech="One moment."),
                                  {"business_name": "Riverside"})
        tts, transport = vf.FakeTTS(), vf.FakeTransport()
        s = VoiceSession(g, cfg, transport, stt=vf.FakeSTT(), tts=tts, vad=None, **kw)
        runner = asyncio.create_task(s.run())
        started.append((s, runner))
        return s, tts

    yield make
    for s, runner in started:
        await s.stop()
        await asyncio.wait({runner}, timeout=5)


async def test_voice_call_greets_moves_and_hangs_up(call):
    s, tts = await call()
    await until(lambda: tts.spoken)
    assert tts.spoken == ["Hello from Riverside."]

    await s.queue([vf.transcription("I have a parking question")])
    await until(lambda: len(tts.spoken) >= 3)
    assert tts.spoken[1] == "One moment."
    assert tts.spoken[2].startswith("[test model] Answer questions about parking")
    assert names(s, "user_speech", "text") == ["I have a parking question"]
    assert names(s, "node_transition", "node_name") == ["Start", "Help"]

    await s.queue([vf.transcription("thanks, goodbye")])
    await until(lambda: s.finished.is_set())
    assert names(s, "call_ended", "reason") == ["end step reached"]
    assert tts.spoken[-1].startswith("[test model] Say goodbye")


async def test_deferred_start_waits_for_the_caller(call):
    s, tts = await call(autostart=False)
    await asyncio.sleep(0.8)
    assert tts.spoken == [] and not s.events
    await s.begin()
    await s.begin()          # second call is a no-op
    await until(lambda: tts.spoken)
    assert tts.spoken == ["Hello from Riverside."]


async def test_browser_call_over_webrtc(cfg, agents_dir):
    """A real WebRTC handshake (aiortc on both ends) against the offer endpoint."""
    from aiortc import RTCPeerConnection, RTCSessionDescription

    spoken = []

    def speech():
        tts = vf.FakeTTS()
        spoken.append(tts.spoken)
        return vf.FakeSTT(), tts

    engine = Engine(agents_dir, cfg, speech_factory=speech)
    app = create_app(engine)
    pc = RTCPeerConnection()
    pc.addTransceiver("audio", direction="sendrecv")
    pc.addTransceiver("video", direction="sendrecv")
    pc.createDataChannel("messages")
    await pc.setLocalDescription(await pc.createOffer())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost", timeout=30) as c:
        r = await c.post("/v1/calls/webrtc/offer", json={"agent": "front-desk", "sdp": pc.localDescription.sdp,
                                                         "type": pc.localDescription.type})
        assert r.status_code == 200, r.text
        answer = r.json()
        assert answer["type"] == "answer" and answer["session_id"] in engine.sessions
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
        session = engine.sessions[answer["session_id"]]
        try:
            await until(lambda: spoken and spoken[0], timeout=20)
            assert spoken[0][0].startswith("Thanks for calling Riverside Clinic")
            info = (await c.get(f"/v1/sessions/{session.id}")).json()
            assert info["current_step"] == "Greeting"
        finally:
            await pc.close()
            await until(lambda: session.finished.is_set(), timeout=20)
        await engine.shutdown()


async def test_webrtc_offer_unknown_agent(cfg, agents_dir):
    engine = Engine(agents_dir, cfg, speech_factory=lambda: (vf.FakeSTT(), vf.FakeTTS()))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost") as c:
        r = await c.post("/v1/calls/webrtc/offer", json={"agent": "nope", "sdp": "x", "type": "offer"})
    assert r.status_code == 404


async def test_whisper_listening_in_a_call(cfg, fake_url, requests_seen):
    """Local Whisper (OpenAI-style /audio/transcriptions) turns a spoken segment into a step change."""
    from pipecat.frames.frames import InputAudioRawFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame
    from vpengine.services import Config, make_stt

    stt = make_stt(Config(stt="whisper-server", whisper_url=fake_url))
    g = Graph.from_definition("Mini", mini_definition(), {"business_name": "Riverside"})
    tts, transport = vf.FakeTTS(), vf.FakeTransport()
    s = VoiceSession(g, cfg, transport, stt=stt, tts=tts, vad=None)
    runner = asyncio.create_task(s.run())
    try:
        await until(lambda: tts.spoken)
        chunk = b"\x10\x00" * 1600                                   # 0.1 s at 16 kHz
        await s.queue([VADUserStartedSpeakingFrame()])
        await s.queue([InputAudioRawFrame(audio=chunk, sample_rate=16000, num_channels=1) for _ in range(8)])
        await s.queue([VADUserStoppedSpeakingFrame()])
        await until(lambda: "Help" in names(s, "node_transition", "node_name"), timeout=15)
        assert names(s, "user_speech", "text") == ["I have a parking question"]
        assert any(r["path"].endswith("/audio/transcriptions") and r["bytes"] > 1000 for r in requests_seen)
    finally:
        await s.stop()
        await asyncio.wait({runner}, timeout=5)
