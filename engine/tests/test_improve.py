"""Improve an agent from its test cases: run them as text chats, propose prompt changes, keep only what helps."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from conftest import mini_definition, write_agent
from vpengine import evals, improve
from vpengine.server import Engine, create_app

CASE = {"name": "Parking question", "persona": "Sam, driving in for an appointment",
        "goal": "find out about parking", "criteria": ["The agent mentions parking validation"]}


async def until(cond, timeout=30.0):
    end = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > end:
            raise AssertionError("timed out waiting")
        await asyncio.sleep(0.05)


@pytest.fixture
async def setup(cfg, agents_dir, tmp_path, monkeypatch):
    write_agent(agents_dir / "mini", mini_definition(), {"business_name": "Maple Paws Vet"})
    engine = Engine(agents_dir, cfg, data_dir=tmp_path / "data")
    c = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost", timeout=30)

    async def caller(llm, persona, goal, said):              # one question, then hang up
        asked = any(x["who"] == "caller" for x in said)
        return (None, True) if asked else ("I have a parking question", False)

    async def grader(llm, run, said):                        # passes when the agent mentions validation
        ok = any(x["who"] == "agent" and "validation" in x["text"].lower() for x in said)
        return {"goal_met": ok, "summary": "", "criteria": [
            {"criterion": run["criteria"][0], "passed": ok, "reason": "" if ok else "never mentioned validation"}]}

    monkeypatch.setattr(improve, "caller_line", caller)
    monkeypatch.setattr(evals, "grade", grader)
    case = engine.evals.save_cases([CASE])[0]
    yield engine, c, case
    await c.aclose()
    await engine.shutdown()


def _proposal(prompt):
    async def propose(llm, definition, failures):
        assert failures and failures[0]["results"]["criteria"][0]["reason"] == "never mentioned validation"
        return improve.clean_edits(definition, [{"step_id": "2", "prompt": prompt, "why": "callers ask about it"}]), "x"
    return propose


async def _finish(c, jid):
    out = {}

    async def done():
        out.update((await c.get(f"/v1/improve/{jid}")).json())
        return out["status"] != "running"
    end = asyncio.get_running_loop().time() + 60
    while not await done():
        assert asyncio.get_running_loop().time() < end, out.get("log")
        await asyncio.sleep(0.1)
    return out


async def test_a_change_that_fixes_a_failing_case_is_kept_and_applied(setup, monkeypatch):
    engine, c, case = setup
    monkeypatch.setattr(improve, "propose", _proposal(
        "Answer questions about parking. Mention that parking validation is free at the front desk."))
    r = await c.post("/v1/agents/mini/improve", json={"case_ids": [case["id"]], "rounds": 2})
    assert r.status_code == 200, r.text
    assert (await c.post("/v1/agents/mini/improve", json={})).status_code in (409, 200)
    job = await _finish(c, r.json()["id"])
    assert job["status"] == "done", job["log"]
    assert job["baseline"]["score"]["passed"] == 0 and job["best"]["score"]["passed"] == 1
    assert job["improved"] and job["attempts"][0]["kept"]
    [edit] = job["edits"]
    assert edit["step_id"] == "2" and edit["before"] == "Answer questions about parking." and "validation" in edit["prompt"]
    # the test chats ran with mocked tools against a copy: the agent on disk hasn't changed yet
    assert "validation" not in (await c.get("/v1/agents/mini")).json()["definition"]["nodes"][1]["data"]["prompt"]
    r = await c.post(f"/v1/improve/{job['id']}/apply")
    assert r.status_code == 200, r.text
    nodes = {n["id"]: n for n in (await c.get("/v1/agents/mini")).json()["definition"]["nodes"]}
    assert "validation" in nodes["2"]["data"]["prompt"] and nodes["3"]["data"]["prompt"] == "Say goodbye."
    assert (await c.post(f"/v1/improve/{job['id']}/apply")).status_code == 409


async def test_a_change_that_doesnt_help_isnt_kept(setup, monkeypatch):
    engine, c, case = setup
    monkeypatch.setattr(improve, "propose", _proposal("Answer questions about parking briefly."))
    job = await _finish(c, (await c.post("/v1/agents/mini/improve", json={"rounds": 1})).json()["id"])
    assert job["status"] == "done" and not job["improved"] and not job["attempts"][0]["kept"]
    assert (await c.post(f"/v1/improve/{job['id']}/apply")).status_code == 409


async def test_an_agent_edited_meanwhile_isnt_overwritten(setup, monkeypatch):
    engine, c, case = setup
    monkeypatch.setattr(improve, "propose", _proposal("Answer questions about parking. Mention parking validation."))
    job = await _finish(c, (await c.post("/v1/agents/mini/improve", json={})).json()["id"])
    assert job["improved"]
    doc = (await c.get("/v1/agents/mini")).json()
    doc["definition"]["nodes"][2]["data"]["prompt"] = "Say goodbye warmly."
    assert (await c.put("/v1/agents/mini", json=doc)).status_code == 200
    r = await c.post(f"/v1/improve/{job['id']}/apply")
    assert r.status_code == 409 and "changed" in r.json()["detail"]


async def test_requests_are_checked(setup):
    engine, c, case = setup
    assert (await c.post("/v1/agents/nope/improve", json={})).status_code == 404
    assert (await c.post("/v1/agents/mini/improve", json={"case_ids": ["tc_missing12345"]})).status_code == 422
    assert (await c.post("/v1/agents/mini/improve", json={"rounds": 9})).status_code == 422
    assert (await c.get("/v1/improve/im_nothinghere1")).status_code == 404
    assert (await c.get("/v1/improve/../etc")).status_code == 404


def test_only_safe_edits_are_tried():
    d = mini_definition()
    raw = [{"step_id": "nope", "prompt": "x", "why": ""},                                  # no such step
           {"step_id": "3", "prompt": "Say goodbye.", "why": ""},                          # no change
           {"step_id": "9", "prompt": "You are Ava. Be warm.", "why": ""},                 # drops {{business_name}}
           {"step_id": "2", "prompt": "Answer parking questions; offer validation.", "why": "w"},
           {"step_id": "2", "prompt": "a second edit to the same step", "why": ""},
           {"step_id": "1", "prompt": "Greet the caller warmly at {{business_name}}.", "why": ""}]
    edits = improve.clean_edits(d, raw)
    assert [e["step_id"] for e in edits] == ["2", "1"]
    changed = improve.apply_edits(d, edits)
    assert changed["nodes"][1]["data"]["prompt"].startswith("Answer parking") and d["nodes"][1]["data"]["prompt"] == \
        "Answer questions about parking."


def test_better_means_more_checks_and_no_case_lost():
    def r(cid, ok, checks):
        return {"case_id": cid, "verdict": "passed" if ok else "failed",
                "results": {"goal_met": ok, "criteria": [{"passed": p} for p in checks]}}
    old = [r("a", True, [True]), r("b", False, [False, False])]
    assert improve.better(old, [r("a", True, [True]), r("b", False, [True, False])])
    assert not improve.better(old, [r("a", False, [True]), r("b", True, [True, True])])   # a stopped passing
    assert not improve.better(old, old)


async def test_a_model_reply_that_isnt_usable_changes_nothing(setup):
    """The real proposer, against the stand-in model: its answer has no usable edits, so the run ends cleanly."""
    engine, c, case = setup
    job = await _finish(c, (await c.post("/v1/agents/mini/improve", json={"rounds": 1})).json()["id"])
    assert job["status"] == "done" and not job["improved"], job["log"]
    assert job["attempts"][0]["edits"] == [] and "no usable changes" in job["log"][-2]
