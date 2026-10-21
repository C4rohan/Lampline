"""Found in the October 2026 review (security, API and observability skills), kept fixed."""

from __future__ import annotations

import dataclasses
import json
import time

import httpx
import pytest

from vk import agentfs
from vpengine import connectors, transfer as tr
from vpengine.server import Engine, create_app
from vpengine.settings import SettingsStore

OWNER = "owner-key-123456"
PROXIED = {"X-Forwarded-For": "203.0.113.9"}


def as_key(k):
    return {"X-API-Key": k}


def client_for(engine, host="127.0.0.1"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine), client=(host, 5000)),
                             base_url="http://localhost", timeout=30)


@pytest.fixture
async def engine(cfg, agents_dir, tmp_path):
    store = SettingsStore(tmp_path / "cfg" / "settings.json", env={})
    eng = Engine(agents_dir, cfg, api_key=OWNER, store=store, data_dir=tmp_path / "data", remote_editing=True)
    yield eng
    for s in list(eng.sessions.values()):
        if hasattr(s, "close"):
            await s.close()
    await eng.shutdown()


async def new_key(c, name, role):
    return as_key((await c.post("/v1/keys", json={"name": name, "role": role}, headers=as_key(OWNER))).json()["key"])


# -- 1. a step id never becomes a file path --------------------------------------------------------

async def test_a_step_id_cannot_write_outside_the_agent_folder(engine, agents_dir, tmp_path):
    async with client_for(engine) as c:
        agent = (await c.get("/v1/agents/front-desk", headers=as_key(OWNER))).json()
        evil = json.loads(json.dumps(agent["definition"]["nodes"][1]))
        evil["id"], evil["data"]["name"], evil["data"]["prompt"] = "x/../../../../PWNED", "", "injected"
        agent["definition"]["nodes"].append(evil)
        r = await c.put("/v1/agents/front-desk", json=agent, headers=as_key(OWNER))
    assert r.status_code == 422 and "step id" in r.json()["detail"]
    assert not list(tmp_path.rglob("PWNED*"))
    assert agentfs.load(agents_dir / "front-desk")          # the folder still loads


def test_the_prompt_file_name_is_always_a_slug(tmp_path):
    d = {"nodes": [{"id": "../../escape", "type": "agentNode", "position": {"x": 0, "y": 0},
                    "data": {"name": "", "prompt": "hi"}}], "edges": []}
    folder = tmp_path / "a"
    written = agentfs.write(agentfs.Agent(name="A", definition=d), folder)
    assert all(not w.startswith("..") and ".." not in w.split("/") for w in written)
    assert not (tmp_path / "escape.md").exists() and list((folder / "prompts").glob("node-escape*.md"))


# -- 2. a remote save can't re-route transfers --------------------------------------------------------

async def test_a_remote_save_cannot_change_where_calls_are_transferred(engine):
    async with client_for(engine) as c:
        b = {**await new_key(c, "Ben", "builder"), **PROXIED}
        agent = (await c.get("/v1/agents/front-desk", headers=b)).json()
        end = next(n for n in agent["definition"]["nodes"] if n["type"] == "endCall")
        end["data"]["transfer_to"] = "sip:loot@attacker.example"
        r = await c.put("/v1/agents/front-desk", json=agent, headers=b)
        assert r.status_code == 403 and "this computer" in r.json()["detail"]
        # The owner on this computer can.
        assert (await c.put("/v1/agents/front-desk", json=agent, headers=as_key(OWNER))).status_code == 200
        # Then a value the address uses is also guarded, and an unrelated one isn't.
        agent = (await c.get("/v1/agents/front-desk", headers=as_key(OWNER))).json()
        end = next(n for n in agent["definition"]["nodes"] if n["type"] == "endCall")
        end["data"]["transfer_to"] = "{{desk}}"
        agent["context_variables"]["desk"] = "+15551234567"
        assert (await c.put("/v1/agents/front-desk", json=agent, headers=as_key(OWNER))).status_code == 200
        agent = (await c.get("/v1/agents/front-desk", headers=b)).json()
        agent["context_variables"]["desk"] = "+19005550100"
        assert (await c.put("/v1/agents/front-desk", json=agent, headers=b)).status_code == 403
        agent = (await c.get("/v1/agents/front-desk", headers=b)).json()
        agent["context_variables"]["opening_hours"] = "9 to 5"
        assert (await c.put("/v1/agents/front-desk", json=agent, headers=b)).status_code == 200


def test_call_context_cannot_override_the_agents_own_transfer_address():
    from vpengine.graph import render

    class Step:
        kind, name, data = "end", "Hand off", {"transfer_to": "{{desk}}"}

    t = tr.build(Step(), {"desk": "+19005550100"}, "s1", render, constants={"desk": "+15551234567"})
    assert t.to == "+15551234567"
    t = tr.build(Step(), {"desk": "+15559876543"}, "s1", render, constants={"desk": ""})   # an empty default fills in
    assert t.to == "+15559876543"


# -- 3. an outbound call never rings twice --------------------------------------------------------------

def phone_engine(cfg, agents_dir, tmp_path, handler):
    c = dataclasses.replace(cfg, outbound="on", quiet_hours="", twilio_account_sid="AC1",
                            twilio_auth_token="twilio-token-123456", twilio_from_number="+15550009999")
    eng = Engine(agents_dir, c, api_key=OWNER, data_dir=tmp_path / "data", public_url="https://tunnel.example.test")
    eng.automations.phone_transport = httpx.MockTransport(handler)
    return eng


async def test_a_timed_out_call_is_not_placed_again(cfg, agents_dir, tmp_path):
    seen = []

    def slow(request):
        seen.append(request)
        raise httpx.ReadTimeout("no answer", request=request)

    eng = phone_engine(cfg, agents_dir, tmp_path, slow)
    first = await eng.automations.outbound("front-desk", "call", "+15550101234")
    assert not first["ok"] and first["unknown"] and "may still ring" in first["detail"]
    again = await eng.automations.outbound("front-desk", "call", "+15550101234")
    assert not again["ok"] and "last 10 minutes" in again["detail"] and len(seen) == 1
    await eng.shutdown()


async def test_a_refused_call_does_not_use_up_the_limits(cfg, agents_dir, tmp_path):
    eng = phone_engine(cfg, agents_dir, tmp_path, lambda r: httpx.Response(400, json={"message": "bad number"}))
    for _ in range(2):
        r = await eng.automations.outbound("front-desk", "call", "+15550101234")
        assert not r["ok"] and "refused" in r["detail"]          # the second try isn't blocked by the first
    await eng.shutdown()


async def test_the_api_trigger_honours_idempotency_keys(cfg, agents_dir, tmp_path):
    calls = []

    def ok(request):
        calls.append(request)
        return httpx.Response(201, json={"sid": f"CA{len(calls)}"})

    folder = agents_dir / "front-desk"
    a = agentfs.load(folder)
    a.definition["nodes"].append({"id": "api", "type": "trigger", "position": {"x": 0, "y": 0},
                                  "data": {"name": "API", "enabled": True, "trigger_path": "remind"}})
    agentfs.write(a, folder)
    eng = phone_engine(cfg, agents_dir, tmp_path, ok)
    async with client_for(eng) as c:
        h = {**as_key(OWNER), "Idempotency-Key": "order-42"}
        body = {"phone_number": "+15550101234"}
        r1 = await c.post("/v1/public/agent/remind", json=body, headers=h)
        r2 = await c.post("/v1/public/agent/remind", json=body, headers=h)
        assert r1.status_code == 200 and r2.json() == r1.json() and len(calls) == 1
        r3 = await c.post("/v1/public/agent/remind", json={"phone_number": "+15550101299"}, headers=h)
        assert r3.status_code == 422 and "different request" in r3.json()["detail"]
        r4 = await c.post("/v1/public/agent/remind", json={**body, "dry_run": "false"}, headers=as_key(OWNER))
        assert r4.status_code == 422 and len(calls) == 1          # "false" as text is refused, not read as true
    await eng.shutdown()


async def test_an_unknown_outcome_answers_504(cfg, agents_dir, tmp_path):
    def slow(request):
        raise httpx.ConnectTimeout("no answer", request=request)

    folder = agents_dir / "front-desk"
    a = agentfs.load(folder)
    a.definition["nodes"].append({"id": "api", "type": "trigger", "position": {"x": 0, "y": 0},
                                  "data": {"name": "API", "enabled": True, "trigger_path": "remind"}})
    agentfs.write(a, folder)
    eng = phone_engine(cfg, agents_dir, tmp_path, slow)
    async with client_for(eng) as c:
        r = await c.post("/v1/public/agent/remind", json={"phone_number": "+15550101234"}, headers=as_key(OWNER))
    assert r.status_code == 504 and "may still ring" in r.json()["detail"]
    await eng.shutdown()


# -- 4 and 7. live sessions: no key in the URL, and only for whoever started them ------------------------

async def test_live_events_use_a_session_token_not_the_key(engine):
    async with client_for(engine) as c:
        chat = (await c.post("/v1/chat/sessions", json={"agent": "front-desk"}, headers=as_key(OWNER))).json()
        sid, token = chat["session_id"], chat["events_token"]
        await c.post(f"/v1/chat/sessions/{sid}/end", headers=as_key(OWNER))
        assert (await c.get(f"/v1/sessions/{sid}/events", params={"key": OWNER})).status_code == 401
        assert (await c.get(f"/v1/sessions/{sid}/events", params={"token": "nope"})).status_code == 401
        r = await c.get(f"/v1/sessions/{sid}/events", params={"token": token})
        assert r.status_code == 200 and "data: " in r.text
        other = (await c.post("/v1/chat/sessions", json={"agent": "front-desk"}, headers=as_key(OWNER))).json()
        await c.post(f"/v1/chat/sessions/{other['session_id']}/end", headers=as_key(OWNER))
        # A token opens only its own session.
        assert (await c.get(f"/v1/sessions/{other['session_id']}/events", params={"token": token})).status_code == 401


async def test_a_conversation_belongs_to_the_key_that_started_it(engine):
    async with client_for(engine) as c:
        vic, val = await new_key(c, "Vic", "viewer"), await new_key(c, "Val", "viewer")
        chat = (await c.post("/v1/chat/sessions", json={"agent": "front-desk"}, headers=vic)).json()
        sid = chat["session_id"]
        assert (await c.get(f"/v1/sessions/{sid}", headers=vic)).status_code == 200
        assert (await c.get(f"/v1/sessions/{sid}", headers=val)).status_code == 403
        assert (await c.post(f"/v1/chat/sessions/{sid}/messages", json={"text": "hi"}, headers=val)).status_code == 403
        assert (await c.get(f"/v1/sessions/{sid}", headers=as_key(OWNER))).status_code == 200   # an admin may
        await c.post(f"/v1/chat/sessions/{sid}/end", headers=vic)


async def test_phone_conversations_follow_the_call_history_rule(cfg, agents_dir, tmp_path):
    eng = Engine(agents_dir, cfg, api_key=OWNER, data_dir=tmp_path / "data")       # remote editing off
    async with client_for(eng) as c:
        vic = await new_key(c, "Vic", "viewer")
        graph = eng.load_agent("front-desk")
        s = eng.new_text(graph, {}, meta={"agent": "front-desk", "channel": "phone", "contact": "+15550100"})
        assert (await c.get(f"/v1/sessions/{s.id}", headers={**vic, **PROXIED})).status_code == 403
        assert (await c.get(f"/v1/sessions/{s.id}", headers=vic)).status_code == 200     # on this computer
        await s.close()
    await eng.shutdown()


# -- 5. "local" fails closed ---------------------------------------------------------------------------

async def test_local_only_actions_fail_closed_behind_a_proxy(cfg, agents_dir, tmp_path):
    store = SettingsStore(tmp_path / "cfg" / "settings.json", env={})
    eng = Engine(agents_dir, cfg, api_key=OWNER, store=store, data_dir=tmp_path / "data")
    async with client_for(eng) as c:
        put = lambda **h: c.put("/v1/settings", json={"brand_name": "X"}, headers={**as_key(OWNER), **h})  # noqa: E731
        assert (await put()).status_code == 200
        assert (await put(**{"X-Real-IP": "203.0.113.9"})).status_code == 403
        assert (await put(Via="1.1 proxy")).status_code == 403
    await eng.shutdown()
    # Once the engine has a public address, a proxy that rewrites Host and adds nothing still can't pass
    # for this computer without the owner key.
    eng = Engine(agents_dir, cfg, api_key=OWNER, store=store, data_dir=tmp_path / "data2",
                 public_url="https://voice.example.test")
    async with client_for(eng) as c:
        admin = await new_key(c, "Ana", "admin")
        assert (await c.put("/v1/settings", json={"brand_name": "Y"}, headers=admin)).status_code == 403
        assert (await c.put("/v1/settings", json={"brand_name": "Y"}, headers=as_key(OWNER))).status_code == 200
        r = await c.put("/v1/settings", json={"brand_name": "Z"}, headers={**as_key(OWNER), "Host": "voice.example.test"})
        assert r.status_code == 403                    # reached by its public name: not this computer
    await eng.shutdown()


# -- 6. WhatsApp: odd payloads are skipped and a redelivery is answered once ------------------------------

def test_whatsapp_payloads_are_parsed_defensively_and_once():
    connectors._WHATSAPP_SEEN.clear()
    msg = {"id": "wamid.1", "from": "15550100", "type": "text", "text": {"body": "hi"}}
    good = {"entry": [{"changes": [{"value": {"messages": [msg], "contacts": [{"wa_id": "15550100", "profile": {"name": "Jo"}}]}}]}]}
    got = connectors.Connectors.whatsapp_messages(good)
    assert [(m.sender, m.text, m.name) for m in got] == [("+15550100", "hi", "Jo")]
    assert connectors.Connectors.whatsapp_messages(good) == []                     # Meta redelivered it
    for odd in ({"entry": "x"}, {"entry": [None, {"changes": [5]}]}, {"entry": [{"changes": [{"value": {"messages": [
            {"type": "text", "text": "not-a-dict"}], "contacts": "x"}}]}]}):
        assert connectors.Connectors.whatsapp_messages(odd) == []


# -- 8. the public health check tells strangers nothing about the setup -------------------------------------

async def test_health_without_a_key_is_minimal(engine):
    async with client_for(engine) as c:
        public = (await c.get("/v1/health")).json()
        assert set(public) == {"ok", "version", "auth_required", "container", "brand"}
        full = (await c.get("/v1/health", headers=as_key(OWNER))).json()
        assert "config" in full and "phone_webhook" in full
        assert (await c.get("/openapi.json")).status_code == 404
