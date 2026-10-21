"""A tougher Test Caller: noise, another voice, speaking styles, cutting in, long pauses, caller-side timing,
and replaying a past call."""

from __future__ import annotations

import asyncio
import shutil
import sqlite3
import time
from pathlib import Path

import httpx
import numpy as np
import pytest
from pipecat.frames.frames import (BotStoppedSpeakingFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame,
                                   OutputAudioRawFrame, TTSSpeakFrame, VADUserStartedSpeakingFrame,
                                   VADUserStoppedSpeakingFrame)
from pipecat.tests.utils import SleepFrame, run_test

import voicefakes as vf
from vpengine import evals, stress
from vpengine.server import Engine, create_app
from vpengine.services import Config, with_voice
from vpengine.stress import CallerClock, NoiseMixer, PauseGate, Stress, StressError

TEMPLATE = Path(__file__).resolve().parents[2] / "cli" / "src" / "vk" / "templates" / "test-caller"
RUN = {"caller_agent": "test-caller", "method": "speaker", "number": "+15550101234",
       "persona": "Alex, who wants to book a bike tune-up", "goal": "book a tune-up for Saturday",
       "criteria": ["The agent asks for the caller's name"]}


class FakeFlow:
    class current:
        name = "Caller"


class FakeSession:
    def __init__(self):
        self.events, self.queued, self.flow = [], [], FakeFlow()

    def emit(self, e):
        self.events.append(e)

    async def queue(self, frames):
        self.queued += frames


async def until(cond, timeout=20.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.05)


@pytest.fixture
async def setup(cfg, agents_dir, tmp_path):
    shutil.copytree(TEMPLATE, agents_dir / "test-caller")
    engine = Engine(agents_dir, cfg, data_dir=tmp_path / "data", speech_factory=lambda: (vf.FakeSTT(), vf.FakeTTS()))
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost", timeout=30)
    yield engine, c
    await c.aclose()
    await engine.shutdown()


# ---------------------------------------------------------------------- options

def test_options_are_checked_and_repeatable():
    s = Stress.parse({"noise": "street", "level": "high", "voice": "bf_emma", "style": "hesitant",
                      "interrupt": "often", "pauses": "several", "seed": 42})
    assert s.active and s.as_dict()["seed"] == 42 and "um" in s.persona_note()
    assert s.pause_turns() == Stress.parse({"pauses": "several", "seed": 42}).pause_turns()   # same seed, same plan
    plan = s.pause_turns()
    assert len(plan) == 3 and min(plan) >= 2 and all(6 <= v <= 10 for v in plan.values())
    assert len(Stress.parse({"pauses": "once", "seed": 1}).pause_turns()) == 1
    assert not Stress.parse({}).active and Stress.parse({}).seed > 0                            # a seed is always kept
    for bad in ({"noise": "jet"}, {"level": "loud"}, {"style": "angry"}, {"interrupt": "always"},
                {"voice": "a voice; rm -rf"}, {"voice": "x" * 100}):
        with pytest.raises(StressError):
            Stress.parse(bad)


def test_another_voice_goes_to_the_tts_in_use():
    assert with_voice(Config(tts="kokoro"), "bf_emma").kokoro_voice == "bf_emma"
    assert with_voice(Config(tts="deepgram"), "aura-2-orion-en").tts_voice == "aura-2-orion-en"
    assert with_voice(Config(tts="fish"), "abc123").fish_voice == "abc123"
    assert with_voice(Config(tts="kokoro"), "") == Config(tts="kokoro")


# ---------------------------------------------------------------------- noise

@pytest.mark.parametrize("kind", stress.NOISES)
def test_noise_sits_below_the_voice_at_the_chosen_level(kind):
    rate = 16000
    t = np.arange(rate) / rate
    voice = (6000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)
    for level, db in stress.LEVELS.items():
        m = NoiseMixer(Stress(noise=kind, level=level, seed=3))
        mixed = np.frombuffer(m.mix(voice.tobytes(), rate), dtype=np.int16).astype(np.float32)
        noise = mixed - voice.astype(np.float32)
        snr = 20 * np.log10(np.sqrt(np.mean(voice.astype(np.float32) ** 2)) / np.sqrt(np.mean(noise ** 2)))
        assert abs(snr + db) < 1.5, (kind, level, snr)
    silence = NoiseMixer(Stress(noise=kind, level="low", seed=3)).mix(b"\x00\x00" * 320, rate)
    assert np.abs(np.frombuffer(silence, dtype=np.int16)).max() > 0           # quiet stretches still have the room


async def test_noise_is_mixed_into_audio_going_out():
    frame = OutputAudioRawFrame(audio=(b"\x10\x27" * 480), sample_rate=24000, num_channels=1)
    before = frame.audio
    down, _ = await run_test(NoiseMixer(Stress(noise="car", level="high")), frames_to_send=[frame],
                             expected_down_frames=[OutputAudioRawFrame])
    assert down[0].audio != before and len(down[0].audio) == len(before)


# ---------------------------------------------------------------------- pauses, cutting in, timing

async def test_a_chosen_reply_waits_before_it_is_spoken():
    s = Stress(pauses="once", seed=1)
    turn, wait = next(iter(s.pause_turns().items()))
    gate, session = PauseGate(FakeSession(), s), None
    gate._plan = {2: 0.4}                                  # the second reply waits 0.4 s (shortened for the test)
    session = gate._session
    start = time.monotonic()
    frames = [LLMFullResponseStartFrame(), LLMFullResponseEndFrame(), LLMFullResponseStartFrame(), LLMFullResponseEndFrame()]
    await run_test(gate, frames_to_send=frames)
    assert time.monotonic() - start >= 0.4
    assert session.events == [{"type": "stress", "kind": "pause", "seconds": 0.4}]
    assert 2 <= turn and 6 <= wait <= 10


async def test_the_caller_times_the_agent_and_cuts_in():
    session = FakeSession()
    clock = CallerClock(session, Stress(interrupt="often", seed=5))
    clock._rng.random = lambda: 0.0                        # always cut in
    clock._rng.uniform = lambda a, b: 0.05                 # quickly
    frames = [VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame(),        # the agent's greeting: never cut
              BotStoppedSpeakingFrame(), SleepFrame(0.3),                           # the caller answered
              VADUserStartedSpeakingFrame(), SleepFrame(0.3),                       # the agent replies ... caller cuts in
              VADUserStoppedSpeakingFrame()]
    await run_test(clock, frames_to_send=frames)
    lat = [e["ms"] for e in session.events if e["type"] == "agent_latency"]
    assert len(lat) == 1 and 50 <= lat[0] <= 400, lat     # 0.3 s waited, less voice detection's 0.2 s start
    cut = [e for e in session.events if e.get("kind") == "interruption"]
    assert len(cut) == 1 and cut[0]["text"] in stress.INTERJECTIONS
    assert any(e["type"] == "bot_speech" and e.get("interruption") for e in session.events)
    assert isinstance(session.queued[0], TTSSpeakFrame)


async def test_no_cut_in_when_the_agent_stops_first():
    session = FakeSession()
    clock = CallerClock(session, Stress(interrupt="often", seed=5))
    clock._rng.random = lambda: 0.0
    clock._rng.uniform = lambda a, b: 0.5
    frames = [VADUserStartedSpeakingFrame(), VADUserStoppedSpeakingFrame(),
              VADUserStartedSpeakingFrame(), SleepFrame(0.1), VADUserStoppedSpeakingFrame(), SleepFrame(0.6)]
    await run_test(clock, frames_to_send=frames)
    assert not session.queued and not any(e.get("kind") == "interruption" for e in session.events)


# ---------------------------------------------------------------------- runs over the API

async def test_a_run_keeps_its_caller_options_and_they_reach_the_call(setup):
    from aiortc import RTCPeerConnection, RTCSessionDescription

    engine, c = setup
    bad = await c.post("/v1/evals", json={**RUN, "caller": {"noise": "jet"}})
    assert bad.status_code == 422 and "caller" in bad.json()["detail"]
    run = (await c.post("/v1/evals", json={**RUN, "caller": {"noise": "room", "level": "low", "style": "terse",
                                                              "interrupt": "sometimes", "seed": 7}})).json()
    assert run["options"]["noise"] == "room" and run["options"]["seed"] == 7
    plain = (await c.post("/v1/evals", json={**RUN, "caller": {"level": "low"}})).json()
    assert plain["options"] is None                       # nothing that makes it tougher: a plain run

    pc = RTCPeerConnection()
    pc.addTransceiver("audio", direction="sendrecv")
    await pc.setLocalDescription(await pc.createOffer())
    r = await c.post(f"/v1/evals/{run['id']}/offer", json={"sdp": pc.localDescription.sdp, "type": "offer"})
    assert r.status_code == 200, r.text
    await pc.setRemoteDescription(RTCSessionDescription(sdp=r.json()["sdp"], type=r.json()["type"]))
    await until(lambda: run["id"] in engine.eval_live)
    session = engine.eval_live[run["id"]]
    assert session.stress.noise == "room" and "short" in session.flow.variables["persona"]   # the style reaches the caller
    await until(lambda: session.task is not None, timeout=20)
    names = [type(p).__name__ for p in session.pipeline.processors]
    assert "NoiseMixer" in names and "CallerClock" in names and "PauseGate" not in names
    await asyncio.sleep(0.5)
    session.emit({"type": "agent_latency", "ms": 900})
    session.emit({"type": "agent_latency", "ms": 1500})
    await session.queue([vf.transcription("Thanks for calling Northside Bikes. How can I help?")])
    await until(lambda: any(e["type"] == "bot_speech" for e in session.events))
    await pc.close()
    await until(lambda: (engine.evals.get(run["id"]) or {}).get("status") in evals.DONE, timeout=30)
    done = (await c.get(f"/v1/evals/{run['id']}")).json()
    assert done["agent_ms"]["turns"] >= 2 and done["agent_ms"]["max"] >= 1500


async def test_replay_a_run_or_a_kept_call(setup):
    engine, c = setup
    run = engine.evals.create(caller_agent="test-caller", method="speaker", number="+15550101234",
                              persona="Alex", goal="book", criteria=["asks the name"], max_minutes=5,
                              options={"noise": "car", "level": "medium", "voice": "", "style": "", "interrupt": "",
                                       "pauses": "", "seed": 9})
    engine.evals.update(run["id"], status="failed", transcript=[
        {"who": "agent", "text": "Hi, how can I help?"}, {"who": "caller", "text": "I need a tune-up."},
        {"who": "agent", "text": "Sure, when?"}, {"who": "caller", "text": "Hold on a second", "interruption": True},
        {"who": "caller", "text": "Saturday."}])
    again = (await c.post("/v1/evals/replay", json={"run_id": run["id"], "exact": False})).json()
    assert again["persona"] == "Alex" and again["caller"]["seed"] == 9 and again["number"] == "+15550101234"
    exact = (await c.post("/v1/evals/replay", json={"run_id": run["id"]})).json()
    assert exact["persona"].startswith("Alex\n\nReplay a past call") and "1. I need a tune-up." in exact["persona"]
    assert "2. Saturday." in exact["persona"] and "Hold on" not in exact["persona"]   # cut-ins come from the seed
    assert exact["criteria"] == ["asks the name"]
    twice = evals.replay_persona(["Hello"], exact["persona"].split("\n\nReplay a past call")[0])
    assert twice.count("Replay a past call") == 1

    # a real call kept in the history, graded against checks
    engine.history.db().execute(
        "INSERT INTO calls (id, agent, agent_name, channel, started, ended, steps, collected, events, turns) "
        "VALUES ('0123456789abcdef', 'front-desk', 'Front desk', 'phone', ?, ?, '[]', '{}', ?, 1)",
        (time.time(), time.time(), '[{"type": "bot_speech", "text": "Hello"}, {"type": "user_speech", "text": "Can I come Thursday?"}]'))
    engine.history.db().commit()
    engine.history.save_checks("front-desk", goal="Books the visit", criteria=["Asks for a name"], enabled=True)
    draft = (await c.post("/v1/evals/replay", json={"call_id": "0123456789abcdef"})).json()
    assert "1. Can I come Thursday?" in draft["persona"] and draft["goal"] == "Books the visit"
    assert draft["criteria"] == ["Asks for a name"] and draft["number"] == ""
    assert (await c.post("/v1/evals/replay", json={"call_id": "ffffffffffffffff"})).status_code == 404
    assert (await c.post("/v1/evals/replay", json={})).status_code == 422


def test_a_version_2_store_is_upgraded(tmp_path):
    store = evals.EvalStore(tmp_path)
    store.db()
    store.db().executescript("ALTER TABLE runs DROP COLUMN options; ALTER TABLE runs DROP COLUMN latencies; "
                             "PRAGMA user_version = 2;")
    store.db().execute("INSERT INTO runs (id, created, caller_agent, method, number, persona, goal, criteria, "
                       "max_minutes, status) VALUES ('ev_oldrun12345', 1, 'test-caller', 'speaker', '+15550101234', "
                       "'p', 'g', '[]', 5, 'passed')")
    store.db().commit()
    store.close()
    again = evals.EvalStore(tmp_path)
    old = again.get("ev_oldrun12345")
    assert old["status"] == "passed" and old["options"] is None and old["agent_ms"] is None
    assert again.db().execute("PRAGMA user_version").fetchone()[0] == evals.SCHEMA_VERSION == 3
    cols = {r[1] for r in sqlite3.connect(tmp_path / "evals.sqlite3").execute("PRAGMA table_info(runs)")}
    assert {"options", "latencies"} <= cols
