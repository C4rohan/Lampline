"""Triggers at run time: message channels, after-call notes, outbound safeguards, schedules,
the API trigger, WhatsApp's webhook, Google sign-in, export and MCP refresh. All services are fakes."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import io
import json
import zipfile
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from conftest import mini_definition
from vk import agentfs
from vpengine.connectors import Connectors, Incoming
from vpengine.server import Engine, create_app
from vpengine.settings import SettingsStore


class Fake:
    """Telegram, Gmail, a CRM webhook, a recipient list, Twilio and Google's token endpoint."""

    def __init__(self):
        self.seen: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        url = str(request.url)
        if "/sendMessage" in url:
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.seen)}})
        if url == "https://oauth2.googleapis.com/token":
            form = parse_qs(request.content.decode())
            if form.get("grant_type") == ["authorization_code"]:
                assert form["code_verifier"][0] and form["code"] == ["the-code"]
                payload = "eyJhbGciOiJub25lIn0." + __import__("base64").urlsafe_b64encode(
                    json.dumps({"email": "desk@example.test"}).encode()).decode().rstrip("=") + ".sig"
                return httpx.Response(200, json={"access_token": "ya29.a", "refresh_token": "1//new-refresh", "expires_in": 3600,
                                                 "id_token": payload})
            return httpx.Response(200, json={"access_token": "ya29.a", "expires_in": 3600})
        if url.endswith("/messages/send"):
            return httpx.Response(200, json={"id": "m1"})
        if url.startswith("https://crm.example.test/"):
            return httpx.Response(204)
        if url.startswith("https://sched.example.test/reminders"):
            assert request.headers["authorization"] == "Bearer sched-key-123456"
            return httpx.Response(200, json=[{"to": "77", "context": {"caller_name": "Lee"}}])
        if "api.twilio.com" in url:
            return httpx.Response(201, json={"sid": "CA123"})
        return httpx.Response(404)


def agent_with(triggers: list[dict], extra_nodes: list[dict] | None = None) -> agentfs.Agent:
    d = mini_definition()
    d["nodes"] += extra_nodes or []
    return agentfs.Agent(name="Parking Desk", definition=d, context_variables={"business_name": "Harbor Garage"},
                         triggers=triggers)


@pytest.fixture
async def setup(cfg, agents_dir, tmp_path):
    made: list[Engine] = []

    def make(triggers, extra_nodes=None, **settings):
        agentfs.write(agent_with(triggers, extra_nodes), agents_dir / "parking")
        c = dataclasses.replace(cfg, **{**dict(telegram_bot_token="TG-TOKEN-123456", google_client_id="cid",
                                                google_client_secret="csecret-123456", google_refresh_token="1//refresh-123456",
                                                email_provider="gmail", outbound="on", quiet_hours=""), **settings})
        store = SettingsStore(tmp_path / "settings.json", env={})
        store.update({"llm": "openai", "model": "fake-model", "base_url": cfg.base_url})
        engine = Engine(agents_dir, c, store=store)
        engine.cfg = c
        fake = Fake()
        engine.connectors = Connectors(c, transport=httpx.MockTransport(fake), save=engine._save_settings)
        engine.automations.transport = httpx.MockTransport(fake)
        engine.automations.phone_transport = httpx.MockTransport(fake)
        engine.fake = fake
        made.append(engine)
        return engine
    yield make
    for engine in made:                       # sessions left open would keep the event loop busy
        await engine.automations.stop()
        for s in list(engine.sessions.values()):
            if hasattr(s, "close"):
                await s.close()
        await engine.shutdown()


def sent_to(fake: Fake, chat: str) -> list[str]:
    return [json.loads(r.content)["text"] for r in fake.seen if "/sendMessage" in str(r.url) and json.loads(r.content)["chat_id"] == chat]


async def wait_for(check, seconds=8.0):
    for _ in range(int(seconds / 0.05)):
        if check():
            return True
        await asyncio.sleep(0.05)
    return False


async def test_telegram_conversation_and_after_call_note(setup):
    engine = setup([{"id": "chat", "type": "message", "channel": "telegram"},
                    {"id": "note", "type": "after_call", "send": {"via": "telegram", "to": "99",
                                                                  "text": "From {{contact}} ({{channel}}): {{details}}"}}])
    assert engine.automations.channel_agent("telegram") == "parking"
    first = await engine.automations.incoming(Incoming("telegram", "42", "Where can I park?", "Sam"))
    assert first[0] == "Hello from Harbor Garage." and len(first) >= 2
    replies = sent_to(engine.fake, "42")
    assert replies and replies[0].startswith("Hello from Harbor Garage.")
    conv = engine.automations.conversations[("telegram", "42")]
    assert conv.session.flow.variables["caller_name"] == "Sam" and conv.session.meta["channel"] == "telegram"
    await engine.automations.incoming(Incoming("telegram", "42", "parking details please"))   # the fake model says goodbye
    assert len(sent_to(engine.fake, "42")) == 2 and conv.session.flow.ended
    assert ("telegram", "42") not in engine.automations.conversations
    assert await wait_for(lambda: sent_to(engine.fake, "99"))                    # the conversation ended: the note goes out
    assert sent_to(engine.fake, "99")[0].startswith("From 42 (telegram):")

    again = await engine.automations.incoming(Incoming("telegram", "42", "Where can I park?"))  # a new conversation
    assert again[0] == "Hello from Harbor Garage."
    conv = engine.automations.conversations[("telegram", "42")]
    conv.last -= 3600                                                           # idle: it's closed
    await engine.automations._expire()
    assert ("telegram", "42") not in engine.automations.conversations and conv.session.finished.is_set()
    assert await wait_for(lambda: len(sent_to(engine.fake, "99")) == 2)
    assert engine.automations.log[-1]["what"] == "send telegram" and engine.automations.log[-1]["to"] == "…"


async def test_after_call_skips_browser_tests_and_runs_webhook_nodes(setup):
    hook = {"id": "w", "type": "webhook", "position": {"x": 0, "y": 0},
            "data": {"name": "Notify CRM", "enabled": True, "http_method": "POST", "endpoint_url": "https://crm.example.test/calls",
                     "custom_headers": [{"key": "X-Token", "value": "{{secrets.CRM_TOKEN}}"}],
                     "payload_template": {"run": "{{workflow_run_id}}", "where": "{{channel}}", "all": "{{gathered_context}}"}}}
    engine = setup([{"id": "mail", "type": "after_call", "when": "always",
                     "send": {"via": "email", "to": "desk@example.test", "subject": "Call", "text": "{{summary}}"}}], [hook])
    engine.store.set_secret("CRM_TOKEN", "crm-token-123456")
    graph = engine.load_agent("parking")

    web = engine.new_text(graph, {}, meta={"agent": "parking", "channel": "web"})
    await web.open()
    await web.close()
    await asyncio.sleep(0.3)
    assert not [r for r in engine.fake.seen if "crm.example" in str(r.url) or "messages/send" in str(r.url)]

    phone = engine.new_text(graph, {"caller_phone": "+15550101234"}, meta={"agent": "parking", "channel": "phone",
                                                                          "contact": "+15550101234"})
    await phone.open()
    await phone.send("Where can I park?")
    await phone.close()
    assert await wait_for(lambda: any("crm.example" in str(r.url) for r in engine.fake.seen)
                          and any("messages/send" in str(r.url) for r in engine.fake.seen))
    crm = next(r for r in engine.fake.seen if "crm.example" in str(r.url))
    body = json.loads(crm.content)
    assert crm.headers["x-token"] == "crm-token-123456" and body["where"] == "phone" and body["run"] == phone.id
    assert body["all"]["caller_phone"] == "+15550101234"


async def test_outbound_safeguards(setup):
    engine = setup([])
    a = engine.automations
    engine.cfg = engine.connectors.cfg = dataclasses.replace(engine.cfg, outbound="off")
    r = await a.outbound("parking", "message", "42", channel="telegram")
    assert not r["ok"] and "off" in r["detail"]

    engine.cfg = engine.connectors.cfg = dataclasses.replace(engine.cfg, outbound="on", quiet_hours="00:00-23:59")
    r = await a.outbound("parking", "message", "42", channel="telegram")
    assert not r["ok"] and "quiet hours" in r["detail"]

    engine.cfg = engine.connectors.cfg = dataclasses.replace(engine.cfg, quiet_hours="", outbound_daily_cap="2")
    dry = await a.outbound("parking", "message", "42", channel="telegram", dry_run=True, text="Hi {{name}}", context={"name": "Jo"})
    assert dry["ok"] and dry["dry_run"] and "Hi Jo" in dry["detail"] and not sent_to(engine.fake, "42")
    r = await a.outbound("parking", "message", "42", channel="telegram", text="Hi {{name}}, your spot is ready.", context={"name": "Jo"})
    assert r["ok"] and sent_to(engine.fake, "42") == ["Hi Jo, your spot is ready."]
    assert ("telegram", "42") in a.conversations                    # their reply continues this conversation
    again = await a.outbound("parking", "message", "42", channel="telegram")
    assert not again["ok"] and "last 10 minutes" in again["detail"]
    await a.outbound("parking", "message", "43", channel="telegram")
    capped = await a.outbound("parking", "message", "44", channel="telegram")
    assert not capped["ok"] and "daily limit" in capped["detail"]
    bad = await a.outbound("parking", "message", "nope", channel="whatsapp", dry_run=True)
    assert not bad["ok"] and "phone number" in bad["detail"]


async def test_outbound_call_with_twilio(setup):
    engine = setup([], twilio_account_sid="AC1", twilio_auth_token="twilio-token-123456", twilio_from_number="+15550009999")
    r = await engine.automations.outbound("parking", "call", "+1 555 010 1234", context={"caller_name": "Sam"})
    assert not r["ok"] and "public address" in r["detail"]
    engine.public_url = "https://tunnel.example.test"
    r = await engine.automations.outbound("parking", "call", "+1 555 010 1234", context={"caller_name": "Sam"})
    assert r["ok"] and "CA123" in r["detail"]
    req = next(x for x in engine.fake.seen if "api.twilio.com" in str(x.url))
    form = parse_qs(req.content.decode())
    assert form["To"] == ["+15550101234"] and form["From"] == ["+15550009999"]
    q = parse_qs(urlsplit(form["Url"][0]).query)
    assert q["agent"] == ["parking"]
    ctx = engine.automations.take_context(q["ctx"][0])
    assert ctx == {"caller_name": "Sam", "caller_phone": "+15550101234"}
    assert engine.automations.take_context(q["ctx"][0]) == {}          # once only


async def test_schedule_api_trigger_and_endpoints(setup):
    trigger_node = {"id": "t", "type": "trigger", "position": {"x": 0, "y": 0},
                    "data": {"name": "API Trigger", "enabled": True, "trigger_path": "remind"}}
    engine = setup([{"id": "rem", "type": "schedule", "cron": "0 17 * * 1-5", "action": "message", "channel": "telegram",
                     "recipients": [{"to": "76"}], "text": "Hi {{caller_name | there}}, see you tomorrow.",
                     "recipients_from": {"url": "https://sched.example.test/reminders",
                                         "headers": {"Authorization": "Bearer {{secrets.SCHED}}"}}}], [trigger_node])
    engine.store.set_secret("SCHED", "sched-key-123456")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost") as c:
        r = (await c.post("/v1/agents/parking/triggers/rem/run", json={"dry_run": True})).json()
        assert [x["ok"] for x in r["results"]] == [True, True]
        assert "Hi there" in r["results"][0]["detail"] and "Hi Lee" in r["results"][1]["detail"]
        assert not sent_to(engine.fake, "76")
        assert (await c.post("/v1/agents/parking/triggers/nope/run", json={})).status_code == 404

        r = await c.post("/v1/public/agent/remind", json={"channel": "telegram", "to": "55", "dry_run": True})
        assert r.status_code == 200 and r.json()["ok"]
        r = await c.post("/v1/public/agent/remind", json={"channel": "telegram", "to": "55", "text": "Your car is ready."})
        assert r.status_code == 200 and sent_to(engine.fake, "55") == ["Your car is ready."]
        assert (await c.post("/v1/public/agent/other", json={"phone_number": "+15550100000"})).status_code == 404
        r = await c.post("/v1/public/agent/remind", json={"phone_number": "+15550100000"})
        assert r.status_code == 409 and "public address" in r.json()["detail"]
        assert (await c.post("/v1/public/agent/remind", json={"to": "55"},
                             headers={"x-forwarded-for": "1.2.3.4"})).status_code == 403

        view = (await c.get("/v1/automation")).json()
        assert view["channels"]["telegram"] is None and view["agents"]["parking"]["api_paths"] == ["remind"]
        assert view["log"][0]["to"] == "…0000" and not view["log"][0]["ok"] and view["outbound"] is True

        data = (await c.get("/v1/agents/parking/export?target=dfcx"))
        assert data.status_code == 200 and data.headers["content-disposition"].endswith('parking-dialogflow-cx.zip"')
        assert "agent.json" in zipfile.ZipFile(io.BytesIO(data.content)).namelist()


async def test_whatsapp_webhook(setup, monkeypatch):
    engine = setup([], whatsapp_verify_token="verify-me-123", whatsapp_app_secret="app-secret-xyz")
    got = []
    monkeypatch.setattr(engine.automations, "incoming", lambda m: got.append(m) or asyncio.sleep(0))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost") as c:
        ok = await c.get("/v1/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "verify-me-123",
                                                         "hub.challenge": "1158201444"})
        assert ok.text == "1158201444"
        assert (await c.get("/v1/whatsapp/webhook", params={"hub.mode": "subscribe", "hub.verify_token": "x"})).status_code == 403
        body = json.dumps({"entry": [{"changes": [{"value": {"messages": [
            {"from": "15550101234", "type": "text", "text": {"body": "hola"}}]}}]}]}).encode()
        assert (await c.post("/v1/whatsapp/webhook", content=body, headers={"x-hub-signature-256": "sha256=00"})).status_code == 403
        sig = "sha256=" + hmac.new(b"app-secret-xyz", body, hashlib.sha256).hexdigest()
        assert (await c.post("/v1/whatsapp/webhook", content=body, headers={"x-hub-signature-256": sig})).status_code == 200
    assert [(m.sender, m.text) for m in got] == [("+15550101234", "hola")]


async def test_google_sign_in(setup):
    engine = setup([], google_refresh_token="")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://127.0.0.1:7860") as c:
        start = (await c.post("/v1/oauth/google/start")).json()
        q = parse_qs(urlsplit(start["url"]).query)
        assert q["redirect_uri"] == ["http://127.0.0.1:7860/v1/oauth/google/callback"]
        assert q["code_challenge_method"] == ["S256"] and q["access_type"] == ["offline"]
        assert "https://www.googleapis.com/auth/calendar.events" in q["scope"][0]
        bad = await c.get("/v1/oauth/google/callback", params={"state": "forged", "code": "the-code"})
        assert "expired" in bad.text
        good = await c.get("/v1/oauth/google/callback", params={"state": q["state"][0], "code": "the-code"})
        assert "Connected desk@example.test" in good.text
        again = await c.get("/v1/oauth/google/callback", params={"state": q["state"][0], "code": "the-code"})
        assert "expired" in again.text                                  # a state works once
    assert engine.cfg.google_refresh_token == "1//new-refresh" and engine.cfg.google_account == "desk@example.test"
    assert engine.store.read()["google_refresh_token"] == "1//new-refresh"


async def test_mcp_refresh_endpoint(setup):
    from test_connectors import FakeMcp
    engine = setup([])
    engine.tool_transport = httpx.MockTransport(FakeMcp())
    engine.store.set_secret("CLINIC_MCP", "mcp-key-123456")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost") as c:
        r = await c.post("/v1/tools/mcp/list", json={"mcp": {"url": "https://mcp.example.test/mcp",
                                                             "headers": {"Authorization": "Bearer {{secrets.CLINIC_MCP}}"}}})
        assert r.status_code == 200 and [t["name"] for t in r.json()["tools"]] == ["find_slots", "book"]
        r = await c.post("/v1/tools/mcp/list", json={"mcp": {"url": "https://{{host}}/mcp"}})
        assert r.status_code == 422
