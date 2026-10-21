"""Evals: a Test Caller phones an agent (Google Voice through the bridge extension, or a phone on speaker), and the
engine grades the call."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import httpx
import pytest

import voicefakes as vf
from vpengine import evals
from vpengine.server import Engine, create_app
from vpengine.services import make_llm

TEMPLATE = Path(__file__).resolve().parents[2] / "cli" / "src" / "vk" / "templates" / "test-caller"


async def until(cond, timeout=20.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.05)


@pytest.fixture
async def setup(cfg, agents_dir, tmp_path):
    shutil.copytree(TEMPLATE, agents_dir / "test-caller")
    engine = Engine(agents_dir, cfg, data_dir=tmp_path / "data",
                    speech_factory=lambda: (vf.FakeSTT(), vf.FakeTTS()))
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost", timeout=30)
    yield engine, c
    await c.aclose()
    await engine.shutdown()


RUN = {"caller_agent": "test-caller", "method": "google_voice", "number": "(555) 010-1234",
       "persona": "Sam, a patient who wants to move a cleaning", "goal": "move the cleaning to Friday morning",
       "criteria": ["The agent asks for the caller's name", "The agent offers a Friday time"]}


def test_numbers_and_links():
    assert evals.normalize_number("(555) 010-1234") == "+15550101234"
    assert evals.normalize_number("+44 20 7946 0958") == "+442079460958"
    with pytest.raises(evals.EvalError):
        evals.normalize_number("12")
    assert evals.google_voice_url("+15550101234") == "https://voice.google.com/u/0/calls?a=nc,%2B15550101234"


async def test_a_google_voice_run_gets_a_one_call_bridge(setup):
    engine, c = setup
    r = await c.post("/v1/evals", json=RUN)
    assert r.status_code == 200, r.text
    run = r.json()
    assert run["status"] == "waiting" and run["number"] == "+15550101234" and len(run["criteria"]) == 2
    assert run["bridge"]["open_url"].endswith("%2B15550101234") and len(run["bridge"]["token"]) > 20
    # The extension's own origin is fine on this path; a wrong token is not.
    bad = await c.post("/v1/evals/bridge/offer", json={"sdp": "x", "type": "offer"},
                       headers={"Origin": "chrome-extension://abcdef", "X-Lampline-Bridge": "nope"})
    assert bad.status_code == 401
    # Other writes from the extension's origin are still refused.
    assert (await c.post("/v1/evals", json=RUN, headers={"Origin": "chrome-extension://abcdef"})).status_code == 403
    listed = (await c.get("/v1/evals")).json()["runs"]
    assert [x["id"] for x in listed] == [run["id"]] and "transcript" not in listed[0]


async def test_bad_runs_are_refused(setup):
    _, c = setup
    assert (await c.post("/v1/evals", json={**RUN, "method": "twilio"})).status_code == 422
    assert (await c.post("/v1/evals", json={**RUN, "goal": ""})).status_code == 422
    assert (await c.post("/v1/evals", json={**RUN, "number": "123"})).status_code == 422
    assert (await c.post("/v1/evals", json={**RUN, "caller_agent": "nobody"})).status_code == 404


async def test_a_bridged_call_is_played_and_graded(setup):
    """The extension's offer starts the Test Caller; what the agent says is heard, and the run is graded."""
    from aiortc import RTCPeerConnection, RTCSessionDescription

    engine, c = setup
    run = (await c.post("/v1/evals", json=RUN)).json()
    pc = RTCPeerConnection()
    pc.addTransceiver("audio", direction="sendrecv")
    await pc.setLocalDescription(await pc.createOffer())
    offer = {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}
    headers = {"Origin": "chrome-extension://abcdef", "X-Lampline-Bridge": run["bridge"]["token"]}
    r = await c.post("/v1/evals/bridge/offer", json=offer, headers=headers)
    assert r.status_code == 200, r.text
    await pc.setRemoteDescription(RTCSessionDescription(sdp=r.json()["sdp"], type=r.json()["type"]))
    # The token started one call; it can't start another.
    assert (await c.post("/v1/evals/bridge/offer", json=offer, headers=headers)).status_code == 401
    await until(lambda: run["id"] in engine.eval_live)
    session = engine.eval_live[run["id"]]
    view = (await c.get(f"/v1/evals/{run['id']}")).json()
    assert view["status"] == "in_call" and view["live"]["session_id"] == session.id and view["live"]["events_token"]
    assert session.flow.variables["goal"] == RUN["goal"]          # the run's persona and goal reach the caller
    await until(lambda: session.task is not None, timeout=20)
    await asyncio.sleep(0.5)
    await session.queue([vf.transcription("Thanks for calling Riverside Dental, this is Ava. How can I help?")])
    await until(lambda: any(e["type"] == "bot_speech" for e in session.events))
    await pc.close()                                              # Google Voice hung up
    await until(lambda: (engine.evals.get(run["id"]) or {}).get("status") in evals.DONE, timeout=30)
    done = engine.evals.get(run["id"])
    assert done["status"] in ("passed", "failed"), done
    assert done["transcript"][0] == {"who": "agent", "text": "Thanks for calling Riverside Dental, this is Ava. How can I help?",
                                     "t": done["transcript"][0]["t"]}
    assert [x["criterion"] for x in done["results"]["criteria"]] == RUN["criteria"]
    assert done["duration"] is not None and run["id"] not in engine.eval_live


async def test_a_call_that_hears_nothing_is_an_error_not_a_fail(setup):
    from aiortc import RTCPeerConnection, RTCSessionDescription

    engine, c = setup
    run = (await c.post("/v1/evals", json={**RUN, "method": "speaker"})).json()
    assert "bridge" not in run
    pc = RTCPeerConnection()
    pc.addTransceiver("audio", direction="sendrecv")
    await pc.setLocalDescription(await pc.createOffer())
    r = await c.post(f"/v1/evals/{run['id']}/offer", json={"sdp": pc.localDescription.sdp, "type": "offer"})
    assert r.status_code == 200 and r.json()["events_token"], r.text
    await pc.setRemoteDescription(RTCSessionDescription(sdp=r.json()["sdp"], type=r.json()["type"]))
    await until(lambda: run["id"] in engine.eval_live)
    stopped = (await c.post(f"/v1/evals/{run['id']}/stop")).json()
    assert stopped["id"] == run["id"]
    await until(lambda: (engine.evals.get(run["id"]) or {}).get("status") in evals.DONE, timeout=30)
    done = engine.evals.get(run["id"])
    assert done["status"] == "error" and "heard nothing" in done["error"]
    await pc.close()
    assert (await c.delete(f"/v1/evals/{run['id']}")).json() == {"deleted": run["id"]}


async def test_grading_maps_every_criterion(cfg):
    llm = make_llm(cfg)
    run = {"goal": "move the cleaning", "criteria": ["asks for the name", "offers Friday"]}
    out = await evals.grade(llm, run, [{"who": "agent", "text": "Hi, what's your name?"},
                                       {"who": "caller", "text": "Sam."}])
    assert [x["criterion"] for x in out["criteria"]] == run["criteria"]
    assert set(out) == {"goal_met", "criteria", "summary"}
    assert evals.verdict({"goal_met": True, "criteria": [{"passed": True}]}) == "passed"
    assert evals.verdict({"goal_met": True, "criteria": [{"passed": False}]}) == "failed"


def test_the_store_has_a_schema_version(tmp_path):
    store = evals.EvalStore(tmp_path)
    store.db()
    assert store.db().execute("PRAGMA user_version").fetchone()[0] == evals.SCHEMA_VERSION
    store.db().execute("PRAGMA user_version = 99")
    store.close()
    with pytest.raises(RuntimeError, match="newer Lampline"):
        evals.EvalStore(tmp_path).db()


# -- test cases imported from test scripts ---------------------------------------------------------------

SCRIPT = """TEST CASE TC-01  Balance by phone   (made-up sample)
1. DIAL +1 555 010 2000
2. WAIT FOR PROMPT "Thanks for calling. Say or enter your account number."
3. SEND DTMF 44721#
4. WAIT FOR PROMPT "Your balance is"
5. HANG UP
EXPECTED: balance is read back
"""


class ScriptedLLM:
    """Answers the converter with fixed JSON, the way a model with structured output would."""
    def __init__(self, reply):
        self.reply, self.asked = reply, []

    async def run_inference(self, ctx, **kw):
        self.asked.append((ctx, kw))
        return self.reply


async def test_a_test_script_becomes_test_cases():
    import json
    llm = ScriptedLLM(json.dumps({"cases": [
        {"name": "TC-01 Balance by phone", "persona": "A customer checking a balance. When asked for the account "
         "number, say 4 4 7 2 1.", "goal": "hear the account balance",
         "criteria": ["The system asks for the account number", "The system reads back a balance", "  "],
         "number": "+1 555 010 2000", "notes": "The script presses keys; the caller says the digits instead."},
        {"name": "", "persona": "x", "goal": "y", "criteria": [], "number": "", "notes": ""}]}))
    cases = await evals.convert_script(llm, evals.script_text(SCRIPT), "balance.txt")
    assert len(cases) == 1 and cases[0]["criteria"] == ["The system asks for the account number",
                                                         "The system reads back a balance"]
    assert "balance.txt" in llm.asked[0][0].messages[0]["content"] and llm.asked[0][1]["response_schema"]
    with pytest.raises(evals.EvalError):
        await evals.convert_script(ScriptedLLM("not json"), "TC-02 something")


def test_script_text_refuses_binaries_and_huge_files():
    assert evals.script_text(b"\xef\xbb\xbfTC-01 ok") == "TC-01 ok"
    with pytest.raises(evals.EvalError, match="isn't text"):
        evals.script_text(b"PK\x03\x04\x00\x00binary")
    with pytest.raises(evals.EvalError, match="too long"):
        evals.script_text("x" * (evals.MAX_SCRIPT + 1))


async def test_cases_are_saved_listed_used_and_deleted(setup):
    engine, c = setup
    r = await c.post("/v1/evals/cases", json={"source": "balance.txt", "cases": [
        {"name": "TC-01", "persona": "A customer", "goal": "hear the balance", "criteria": ["Reads a balance"],
         "number": "+1 555 010 2000"}]})
    assert r.status_code == 200, r.text
    case = r.json()["cases"][0]
    assert case["id"].startswith("tc_") and case["number"] == "+15550102000" and case["source"] == "balance.txt"
    assert (await c.get("/v1/evals/cases")).json()["cases"][0]["id"] == case["id"]
    bad = await c.post("/v1/evals/cases", json={"cases": [{"name": "x", "persona": "", "goal": "y"}]})
    assert bad.status_code == 422
    run = (await c.post("/v1/evals", json={**RUN, "case_id": case["id"]})).json()
    assert run["case_id"] == case["id"]
    assert (await c.delete(f"/v1/evals/cases/{case['id']}")).json() == {"deleted": case["id"]}
    assert (await c.get("/v1/evals/cases")).json()["cases"] == []


def test_a_version_1_store_is_upgraded(tmp_path):
    import sqlite3
    db = sqlite3.connect(tmp_path / "evals.sqlite3")
    db.executescript("""CREATE TABLE runs (id TEXT PRIMARY KEY, created REAL NOT NULL, caller_agent TEXT NOT NULL,
        method TEXT NOT NULL, number TEXT NOT NULL, persona TEXT NOT NULL, goal TEXT NOT NULL, criteria TEXT NOT NULL,
        max_minutes INTEGER NOT NULL, status TEXT NOT NULL, session_id TEXT, started REAL, ended REAL,
        ended_reason TEXT, transcript TEXT, results TEXT, summary TEXT, error TEXT, by TEXT);
        INSERT INTO runs (id, created, caller_agent, method, number, persona, goal, criteria, max_minutes, status)
        VALUES ('ev_oldrun12345', 1, 'test-caller', 'speaker', '+15550101234', 'p', 'g', '[]', 5, 'passed');
        PRAGMA user_version = 1;""")
    db.close()
    store = evals.EvalStore(tmp_path)
    assert store.get("ev_oldrun12345")["status"] == "passed"                  # old runs survive
    assert store.db().execute("PRAGMA user_version").fetchone()[0] == 2
    assert store.cases() == []
