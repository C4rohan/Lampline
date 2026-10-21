"""Real calls graded automatically, response-time spread (p50/p95) and a cost estimate per call."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import sqlite3
import time

import httpx
import pytest

from vpengine import evals, pricing
from vpengine.history import SCHEMA_VERSION, CallHistory
from vpengine.server import Engine, create_app
from vpengine.services import Config


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


# ---------------------------------------------------------------------- pricing

def test_percentile_is_nearest_rank():
    assert pricing.percentile([], 95) is None
    assert pricing.percentile([700], 95) == 700
    turns = list(range(100, 2100, 100))           # 20 turns: 100 ms .. 2000 ms
    assert pricing.percentile(turns, 50) == 1000 and pricing.percentile(turns, 95) == 1900


def test_cost_of_a_cloud_voice_call():
    cfg = Config(stt="deepgram", tts="fish", llm="anthropic", model="claude-haiku-5-5")
    usage = {"stt_seconds": 240.0, "llm_in": 40_000, "llm_out": 2_000, "tts_chars": 3_000}
    est = pricing.estimate(usage, cfg=cfg, minutes=4.1, channel="twilio", voice=True, model=cfg.model)
    assert est["parts"]["speech to text"] == pytest.approx(4 * 0.0077)
    assert est["parts"]["voice"] == pytest.approx(0.003 * 15.0)
    assert est["parts"]["model input"] == pytest.approx(0.04 * 0.10)
    assert est["parts"]["model output"] == pytest.approx(0.002 * 0.50)
    assert est["missing"] == ["phone line"]         # no list price we've checked: left out and named
    assert est["total"] == pytest.approx(sum(est["parts"].values()))
    # your own rates win
    mine = dataclasses.replace(cfg, price_phone_min="0.01", price_llm_in="1", price_llm_out="5")
    est = pricing.estimate(usage, cfg=mine, minutes=4.0, channel="twilio", voice=True, model=cfg.model)
    assert est["missing"] == [] and est["parts"]["phone line"] == pytest.approx(0.04)
    assert est["parts"]["model input"] == pytest.approx(0.04)


def test_local_services_cost_nothing_and_chats_have_no_speech():
    cfg = Config(stt="moonshine", tts="kokoro", llm="ollama", model="llama3.2")
    est = pricing.estimate({"llm_in": 9000, "llm_out": 900, "tts_chars": 500}, cfg=cfg, minutes=2, channel="browser",
                           voice=True, model="llama3.2")
    assert est["total"] == 0 and est["missing"] == []
    chat = pricing.estimate({"llm_in": 1000}, cfg=Config(llm="openai", model="something-new"), minutes=1, channel="chat",
                            voice=False, model="something-new")
    assert "speech to text" not in chat["parts"] and chat["missing"] == ["model input"] and chat["total"] is None


def test_speech_to_speech_is_priced_as_one_model():
    cfg = Config(price_llm_in="32", price_llm_out="64")
    est = pricing.estimate({"llm_in": 10_000, "llm_out": 5_000}, cfg=cfg, minutes=1, channel="browser", voice=True,
                           speech_to_speech=True, model="gpt-realtime")
    assert set(est["parts"]) == {"speech-to-speech model input", "speech-to-speech model output"}
    assert est["total"] == pytest.approx(0.01 * 32 + 0.005 * 64)


def test_price_settings_are_checked():
    from vpengine.services import Config as C, ConfigError
    with pytest.raises(ConfigError):
        C.load(env={}, saved={"price_stt_min": "cheap"})
    assert C.load(env={}, saved={"price_stt_min": "0.004"}).price_stt_min == "0.004"


# ---------------------------------------------------------------------- history

def test_an_old_history_file_upgrades_in_place(tmp_path):
    db = sqlite3.connect(tmp_path / "calls.sqlite3")
    db.executescript("""
        CREATE TABLE calls (id TEXT PRIMARY KEY, agent TEXT, agent_name TEXT, channel TEXT, contact TEXT,
            started REAL, ended REAL, outcome TEXT, transferred_to TEXT,
            steps TEXT, collected TEXT, events TEXT, turns INTEGER, response_ms INTEGER);
    """)
    db.execute("INSERT INTO calls (id, agent, started, ended, steps, collected, events, turns, response_ms) "
               "VALUES ('0123456789abcdef', 'front-desk', ?, ?, '[]', '{}', '[]', 2, 800)", (time.time(), time.time()))
    db.commit()
    db.close()
    h = CallHistory(tmp_path)
    old = h.list()[0]
    assert old["response_ms"] == 800 and old["p95_ms"] is None and old["cost"] is None and old["grade"] == ""
    assert h.db().execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert h.set_grade("0123456789abcdef", "failed", {"summary": "x", "criteria": []})
    assert h.list(grade="failed")[0]["grade_results"]["summary"] == "x" and h.list(grade="passed") == []
    h.close()
    h2 = CallHistory(tmp_path)                       # opening again changes nothing
    assert h2.list()[0]["grade"] == "failed"


def test_a_newer_history_file_is_refused(tmp_path):
    db = sqlite3.connect(tmp_path / "calls.sqlite3")
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.close()
    with pytest.raises(RuntimeError, match="newer Lampline"):
        CallHistory(tmp_path).db()


def test_checks_are_validated(tmp_path):
    h = CallHistory(tmp_path)
    with pytest.raises(ValueError):
        h.save_checks("front-desk", goal="", criteria=["  "], enabled=True)
    saved = h.save_checks("front-desk", goal="Books the visit", criteria=["Asks for a name"] * 20, enabled=True)
    assert len(saved["criteria"]) == 12 and saved["enabled"]
    assert h.save_checks("front-desk", goal="", criteria=[], enabled=False)["enabled"] is False   # off may be empty
    assert h.delete_checks("front-desk") and h.checks() == []


# ---------------------------------------------------------------------- end to end over the API

async def _a_chat(c, text="I'd like to book a cleaning"):
    sid = (await c.post("/v1/chat/sessions", json={"agent": "front-desk"})).json()["session_id"]
    await c.post(f"/v1/chat/sessions/{sid}/messages", json={"text": text})
    await c.post(f"/v1/chat/sessions/{sid}/end")
    return sid


async def test_calls_are_graded_against_the_agents_checks(c, monkeypatch):
    seen = []

    async def fake_grade(llm, run, said):
        seen.append((run, said))
        return {"goal_met": True, "criteria": [{"criterion": run["criteria"][0], "passed": False, "reason": "never asked"}],
                "summary": "Booked, but didn't ask for a name."}
    monkeypatch.setattr(evals, "grade", fake_grade)

    # no checks yet: kept, not graded
    sid = await _a_chat(c)
    assert await _wait(lambda: c.engine.history.get(sid) is not None)
    await asyncio.sleep(0.2)
    assert c.engine.history.get(sid)["grade"] == "" and not seen

    r = await c.put("/v1/call-checks/front-desk", json={"goal": "The visit is booked",
                                                        "criteria": ["The agent asks for the caller's name"], "enabled": True})
    assert r.status_code == 200 and r.json()["criteria"] == ["The agent asks for the caller's name"]
    assert (await c.put("/v1/call-checks/no-such-agent", json={"goal": "x", "enabled": True})).status_code == 404
    assert (await c.put("/v1/call-checks/front-desk", json={"criteria": "a string", "enabled": True})).status_code == 422
    assert (await c.get("/v1/call-checks")).json()["checks"][0]["agent"] == "front-desk"

    sid2 = await _a_chat(c, "Can I come in on Tuesday?")
    assert await _wait(lambda: (c.engine.history.get(sid2) or {}).get("grade") == "failed")
    run, said = seen[-1]
    assert run["goal"] == "The visit is booked" and any(x["text"] == "Can I come in on Tuesday?" for x in said)
    call = (await c.get(f"/v1/calls/{sid2}")).json()
    assert call["grade_results"]["criteria"][0]["reason"] == "never asked"
    assert [x["id"] for x in (await c.get("/v1/calls", params={"grade": "failed"})).json()["calls"]] == [sid2]

    # grade an older call by hand, against other checks
    r = (await c.post(f"/v1/calls/{sid}/grade", json={"goal": "Answers", "criteria": ["Is polite"]})).json()
    assert r["grade"] == "failed" and seen[-1][0]["criteria"] == ["Is polite"]

    # turned off: new calls aren't graded
    await c.put("/v1/call-checks/front-desk", json={"goal": "", "criteria": [], "enabled": False})
    n = len(seen)
    sid3 = await _a_chat(c)
    assert await _wait(lambda: c.engine.history.get(sid3) is not None)
    await asyncio.sleep(0.2)
    assert len(seen) == n and c.engine.history.get(sid3)["grade"] == ""
    assert (await c.delete("/v1/call-checks/front-desk")).json() == {"deleted": "front-desk"}
    assert (await c.post(f"/v1/calls/{sid3}/grade")).status_code == 409


async def test_a_grader_that_fails_marks_the_call_not_the_agent(c, monkeypatch):
    async def broken(llm, run, said):
        raise TimeoutError("model too slow")
    monkeypatch.setattr(evals, "grade", broken)
    await c.put("/v1/call-checks/front-desk", json={"goal": "Books", "enabled": True})
    sid = await _a_chat(c)
    assert await _wait(lambda: (c.engine.history.get(sid) or {}).get("grade") == "error")
    assert "Couldn't grade" in c.engine.history.get(sid)["grade_results"]["summary"]
    assert (await c.get("/v1/calls", params={"grade": "failed"})).json()["calls"] == []


async def test_usage_cost_and_summary(c):
    c.engine.cfg = dataclasses.replace(c.engine.cfg, price_llm_in="2", price_llm_out="8")
    sid = await _a_chat(c)
    assert await _wait(lambda: c.engine.history.get(sid) is not None)
    call = c.engine.history.get(sid)
    detail = call["cost_detail"]
    assert detail["usage"]["llm_in"] > 0, "the fake model reports token usage"
    assert call["cost"] == pytest.approx(detail["usage"]["llm_in"] / 1e6 * 2 + detail["usage"]["llm_out"] / 1e6 * 8, abs=1e-6)
    s = (await c.get("/v1/calls/summary", params={"agent": "front-desk"})).json()
    assert s["calls"] == 1 and s["costed"] == 1 and s["cost_per_call"] == pytest.approx(round(call["cost"], 4), abs=1e-4)


def test_latency_spread_is_kept_per_call(tmp_path):
    h = CallHistory(tmp_path)

    class G:
        name, steps = "Front desk", {}

    class F:
        visited, variables = [], {}
        def collected(self):
            return []

    class S:
        id, meta, created, error = "fedcba9876543210", {"agent": "front-desk", "channel": "web"}, time.time() - 60, None
        graph, flow = G(), F()
        events = [{"type": "user_speech", "text": "hi"}] + [{"type": "turn_metrics", "response_ms": ms}
                                                           for ms in (600, 700, 800, 900, 3000)]
    row = h.save(S(), {"total": 0.0123, "parts": {}, "missing": []})
    assert (row["p50_ms"], row["p95_ms"], row["max_ms"], row["response_ms"]) == (800, 3000, 3000, 1200)
    assert row["cost"] == 0.0123
    s = h.summary(agent="front-desk")
    assert s["p95_ms"] == 3000 and s["turns"] == 1 and json.loads(
        h.db().execute("SELECT latencies FROM calls").fetchone()[0]) == [600, 700, 800, 900, 3000]
