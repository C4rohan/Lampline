"""vk mcp: the protocol handshake, each tool, and what it refuses."""

from __future__ import annotations

import io
import json

import httpx
import pytest

from conftest import API_KEY, ENGINE, make_folder
from vk import agentfs
from vk.mcp_server import Server


@pytest.fixture
def server(tmp_path, engine, monkeypatch):
    monkeypatch.chdir(tmp_path)
    make_folder(tmp_path, {"hello": {"name": "says hello", "turns": [{"user": "hello"}]}})
    return Server(tmp_path / "agents", url=ENGINE, key=API_KEY, transport=httpx.MockTransport(engine.handler))


def call(server, name, **arguments):
    reply = server.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                           "params": {"name": name, "arguments": arguments}})
    res = reply["result"]
    text = res["content"][0]["text"]
    return res["isError"], (text if res["isError"] else json.loads(text))


def test_handshake_and_tool_list_over_stdio(server):
    lines = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "nope"},
    ]
    out = io.StringIO()
    server.serve(io.StringIO("\n".join(json.dumps(m) for m in lines) + "\nnot json\n"), out)
    replies = [json.loads(x) for x in out.getvalue().splitlines()]
    assert [r.get("id") for r in replies] == [1, 2, 3, None]          # nothing for the notification
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18" and "tools" in replies[0]["result"]["capabilities"]
    names = {t["name"] for t in replies[1]["result"]["tools"]}
    assert names == {"list_agents", "lint_agent", "run_tests", "read_prompt", "write_prompt", "list_failed_calls"}
    assert replies[2]["error"]["code"] == -32601 and replies[3]["error"]["code"] == -32700
    old = server.handle({"jsonrpc": "2.0", "id": 4, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert old["result"]["protocolVersion"] == "2025-06-18"


def test_list_lint_and_run_tests(server, engine):
    err, out = call(server, "list_agents")
    assert not err and out["agents"] == [{"agent": "front-desk", "name": "Front Desk", "steps": out["agents"][0]["steps"],
                                          "tests": 1}]
    err, out = call(server, "lint_agent", agent="front-desk")
    assert not err and out["errors"] == 0
    err, out = call(server, "run_tests", agent="front-desk", repeat=2)
    assert not err and out["all_passed"] is True
    [r] = out["results"]
    assert r["name"] == "says hello" and r["runs"] == 2 and r["passes"] == 2 and r["transcript"]
    assert sum(1 for m, p in engine.requests if p == "/v1/chat/sessions") == 2


def test_read_and_write_a_prompt(server, tmp_path):
    err, out = call(server, "read_prompt", agent="front-desk", step="Router")
    assert not err and out["step"] == "Router" and {"id", "name", "type"} <= set(out["steps"][0])
    err, out = call(server, "write_prompt", agent="front-desk", step="Router",
                    prompt="Find out why they called. Ask one short question if it isn't clear.")
    assert not err and out["saved"]
    agent = agentfs.load(tmp_path / "agents" / "front-desk")
    router = next(n for n in agent.definition["nodes"] if n["data"]["name"] == "Router")
    assert router["data"]["prompt"].startswith("Find out why they called")


def test_writes_that_are_refused(server, tmp_path):
    folder = tmp_path / "agents" / "front-desk"
    agent = agentfs.load(folder)
    node = next(n for n in agent.definition["nodes"] if n["data"]["name"] == "Router")
    node["data"]["prompt"] = "Greet {{first_name}} and ask how you can help."
    agentfs.write(agent, folder)
    err, text = call(server, "write_prompt", agent="front-desk", step="Router", prompt="Ask how you can help.")
    assert err and "{{first_name}}" in text
    err, text = call(server, "write_prompt", agent="front-desk", step="Router",
                     prompt="Greet {{first_name}}. Their SSN is 219-09-9999.")
    assert err and "personal details" in text
    err, text = call(server, "write_prompt", agent="front-desk", step="Nowhere", prompt="x")
    assert err and "steps:" in text
    after = next(n for n in agentfs.load(folder).definition["nodes"] if n["data"]["name"] == "Router")
    assert after["data"]["prompt"] == "Greet {{first_name}} and ask how you can help."      # refused writes change nothing


@pytest.mark.parametrize("name", ["../etc", "front-desk/../..", "/tmp", "", "nope"])
def test_agent_names_stay_inside_the_folder(server, name):
    err, text = call(server, "lint_agent", agent=name)
    assert err and ("folder name" in text or "no agent folder" in text)


def test_failed_calls_are_summaries_only(tmp_path, monkeypatch):
    make_folder(tmp_path)
    seen = []

    def engine(request):
        seen.append(request)
        return httpx.Response(200, json={"calls": [{
            "id": "c1", "started": 1.0, "channel": "phone", "contact": "+15550101234",
            "transcript": [{"who": "caller", "text": "my name is Sam"}],
            "grade_results": {"summary": "Didn't confirm.", "criteria": [
                {"criterion": "Confirms the booking", "passed": False, "reason": "never confirmed"},
                {"criterion": "Asks the pet's type", "passed": True, "reason": "asked"}]}}]})
    s = Server(tmp_path / "agents", url=ENGINE, key=API_KEY, transport=httpx.MockTransport(engine))
    err, out = call(s, "list_failed_calls", agent="front-desk", limit=5)
    assert not err and seen[0].url.params["grade"] == "failed" and seen[0].headers["x-api-key"] == API_KEY
    [c] = out["calls"]
    assert c["failed_checks"] == [{"check": "Confirms the booking", "why": "never confirmed"}]
    assert "contact" not in c and "transcript" not in c and "+1555" not in json.dumps(out)


def test_engine_unreachable_is_explained(tmp_path):
    make_folder(tmp_path)

    def down(request):
        raise httpx.ConnectError("refused")
    s = Server(tmp_path / "agents", url=ENGINE, key=API_KEY, transport=httpx.MockTransport(down))
    err, text = call(s, "list_failed_calls", agent="front-desk")
    assert err and "is it running" in text
    err, text = call(s, "run_tests", agent="front-desk")
    assert err
