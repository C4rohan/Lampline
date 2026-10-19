"""MCP servers and connector actions (Telegram, WhatsApp, email, Google Calendar), against fakes."""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json

import httpx
import pytest

from vk import tools as toolspec
from vpengine.connectors import ConnectorError, Connectors, open_days
from vpengine.mcp import McpClient, to_definitions
from vpengine.services import Config
from vpengine.tools import ToolRunner, parse_all


# --------------------------------------------------------------------- fakes

class FakeMcp:
    """An MCP server: JSON replies, or server-sent events when sse=True."""

    def __init__(self, sse: bool = False):
        self.sse = sse
        self.calls: list[dict] = []
        self.sessions = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(200)
        msg = json.loads(request.content)
        self.calls.append({"method": msg.get("method"), "headers": dict(request.headers), "params": msg.get("params")})
        if request.headers.get("authorization") != "Bearer mcp-key-123456":
            return httpx.Response(401)
        if "id" not in msg:
            return httpx.Response(202)
        method = msg["method"]
        if method == "initialize":
            self.sessions += 1
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
            return self._reply(msg, result, {"mcp-session-id": f"s{self.sessions}"})
        assert request.headers.get("mcp-session-id") == f"s{self.sessions}"
        if method == "tools/list":
            if not (msg.get("params") or {}).get("cursor"):
                return self._reply(msg, {"tools": [{"name": "find-slots", "description": "Open slots for a day",
                                                    "inputSchema": {"type": "object", "properties": {"day": {"type": "string"}},
                                                                    "required": ["day"]}}], "nextCursor": "p2"})
            return self._reply(msg, {"tools": [{"name": "book", "inputSchema": {"type": "object", "properties": {"time": {"type": "string"}}}}]})
        if method == "tools/call":
            name, args = msg["params"]["name"], msg["params"]["arguments"]
            if name == "find-slots":
                return self._reply(msg, {"content": [{"type": "text", "text": json.dumps({"slots": ["9:30", "14:00"], "day": args["day"]})}]})
            return self._reply(msg, {"content": [{"type": "text", "text": "no such time"}], "isError": True})
        return self._reply(msg, None, error={"code": -32601, "message": "unknown method"})

    def _reply(self, msg, result, headers=None, error=None):
        body = {"jsonrpc": "2.0", "id": msg["id"], **({"error": error} if error else {"result": result})}
        if self.sse:
            text = "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"}) + "\n\n"
            text += "event: message\ndata: " + json.dumps(body) + "\n\n"
            return httpx.Response(200, headers={"content-type": "text/event-stream", **(headers or {})}, text=text)
        return httpx.Response(200, json=body, headers=headers or {})


MCP_TOOL = {"name": "clinic", "description": "Clinic scheduling server",
            "mcp": {"url": "https://mcp.example.test/mcp", "headers": {"Authorization": "Bearer {{secrets.CLINIC_MCP}}"},
                    "tools": []},
            "mock": {"find_slots": {"slots": ["mock 9:00"]}}}


@pytest.mark.parametrize("sse", [False, True])
async def test_mcp_client_lists_and_calls(sse):
    server = FakeMcp(sse)
    client = McpClient("https://mcp.example.test/mcp", {"Authorization": "Bearer mcp-key-123456"}, transport=httpx.MockTransport(server))
    listed = to_definitions(await client.list_tools())
    assert [t["name"] for t in listed] == ["find_slots", "book"] and listed[0]["remote"] == "find-slots"
    ok, data = await client.call_tool("find-slots", {"day": "2026-10-20"})
    assert ok and data == {"slots": ["9:30", "14:00"], "day": "2026-10-20"}
    ok, data = await client.call_tool("book", {"time": "3"})
    assert not ok and data == "no such time"
    assert server.sessions == 1
    assert server.calls[1]["method"] == "notifications/initialized"
    assert server.calls[2]["headers"]["mcp-protocol-version"] == "2025-06-18"


async def test_mcp_tools_in_an_agent():
    server = FakeMcp()
    definition = {**MCP_TOOL, "mcp": {**MCP_TOOL["mcp"], "tools": to_definitions(
        await McpClient("https://mcp.example.test/mcp", {"Authorization": "Bearer mcp-key-123456"},
                        transport=httpx.MockTransport(server)).list_tools())}}
    assert [m for s, m in toolspec.problems(definition) if s == "error"] == []
    tools = parse_all([definition])
    assert set(tools) == {"find_slots", "book"} and tools["find_slots"].group == "clinic"
    schema = tools["find_slots"].schema()
    assert schema.required == ["day"] and "day" in schema.properties

    live = ToolRunner({"CLINIC_MCP": "mcp-key-123456"}, transport=httpx.MockTransport(server))
    r = await live.run(tools["find_slots"], {"day": "2026-10-21"}, {})
    assert r.ok and r.data["day"] == "2026-10-21" and r.request == "MCP mcp.example.test find-slots"
    r = await live.run(tools["find_slots"], {}, {})
    assert not r.ok and "day" in r.error
    await live.aclose()

    mocked = await ToolRunner(mock=True).run(tools["find_slots"], {"day": "x"}, {})
    assert mocked.ok and mocked.mock and mocked.data == {"slots": ["mock 9:00"]}
    remote_client = await ToolRunner({}, live=False).run(tools["book"], {}, {})
    assert not remote_client.ok and "live connections" in remote_client.error

    wrong = await ToolRunner({"CLINIC_MCP": "nope-nope-nope"}, transport=httpx.MockTransport(server)).run(tools["find_slots"], {"day": "x"}, {})
    assert not wrong.ok and "refused the credentials" in wrong.error


def test_mcp_definition_rules():
    bad = {**MCP_TOOL, "mcp": {"url": "https://{{host}}/mcp", "headers": {"X-Key": "{{caller_id}}"}, "tools": [{"name": "a b"}]},
           "method": "GET"}
    errors = [m for s, m in toolspec.problems(bad) if s == "error"]
    assert any("address" in e for e in errors) and any("caller_id" in e for e in errors)
    assert any("letters, digits" in e for e in errors) and any("not method" in e for e in errors)


# --------------------------------------------------------------------- connectors

class FakeServices:
    """Telegram, WhatsApp (Graph API), Google token, Calendar and Gmail."""

    def __init__(self):
        self.seen: list[httpx.Request] = []
        self.busy: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        url = str(request.url)
        if url.startswith("https://api.telegram.org/botTG-TOKEN-123456/sendMessage"):
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
        if url.startswith("https://api.telegram.org/botTG-TOKEN-123456/getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": [
                {"update_id": 10, "message": {"text": "hi there", "chat": {"id": 42, "type": "private"}, "from": {"first_name": "Sam"}}},
                {"update_id": 11, "message": {"text": "group chat", "chat": {"id": -5, "type": "group"}}}]})
        if url.startswith("https://graph.facebook.com/v21.0/PNID/messages"):
            body = json.loads(request.content)
            if body["to"] == "15550000000":
                return httpx.Response(400, json={"error": {"code": 131047, "message": "Re-engagement message"}})
            return httpx.Response(200, json={"messages": [{"id": "wamid.1"}]})
        if url == "https://oauth2.googleapis.com/token":
            return httpx.Response(200, json={"access_token": "ya29.fake-access", "expires_in": 3600})
        assert request.headers.get("authorization", "").startswith("Bearer ya29.fake-access") or "api.telegram" in url
        if url == "https://www.googleapis.com/calendar/v3/freeBusy":
            return httpx.Response(200, json={"calendars": {"primary": {"busy": self.busy}}})
        if url.startswith("https://www.googleapis.com/calendar/v3/calendars/primary/events"):
            return httpx.Response(200, json={"id": "evt1", "body": json.loads(request.content)})
        if url == "https://gmail.googleapis.com/gmail/v1/users/me/messages/send":
            return httpx.Response(200, json={"id": "m1"})
        return httpx.Response(404, json={"error": {"message": f"unexpected {url}"}})


def _cfg(**kw) -> Config:
    base = dict(telegram_bot_token="TG-TOKEN-123456", whatsapp_token="WA-TOKEN-123456", whatsapp_phone_number_id="PNID",
                whatsapp_app_secret="app-secret-xyz", google_client_id="cid", google_client_secret="csecret-123456",
                google_refresh_token="1//refresh-123456", timezone="America/New_York", calendar_hours="09:00-12:00",
                calendar_days="mon-sat", email_provider="gmail", google_account="desk@example.test")
    return Config(**{**base, **kw})


async def test_telegram_and_whatsapp():
    fake = FakeServices()
    c = Connectors(_cfg(), transport=httpx.MockTransport(fake))
    assert (await c.send_telegram(42, "hello"))["sent"]
    with pytest.raises(ConnectorError, match="chat id"):
        await c.send_telegram("42; drop", "x")
    got, offset = await c.telegram_updates(None)
    assert [(m.sender, m.text, m.name) for m in got] == [("42", "hi there", "Sam")] and offset == 12
    sent = await c.send_whatsapp("+1 (555) 123-4567", "hi")
    assert sent["to"] == "+15551234567" and json.loads(fake.seen[-1].content)["to"] == "15551234567"
    with pytest.raises(ConnectorError, match="24-hour window") as exc:
        await c.send_whatsapp("+15550000000", "hi")
    assert "WA-TOKEN" not in str(exc.value)

    body = json.dumps({"entry": [{"changes": [{"value": {"contacts": [{"wa_id": "15551234567", "profile": {"name": "Ana"}}],
                                                         "messages": [{"from": "15551234567", "type": "text", "text": {"body": "hola"}}]}}]}]}).encode()
    sig = "sha256=" + hmac.new(b"app-secret-xyz", body, hashlib.sha256).hexdigest()
    assert c.whatsapp_signature_ok(body, sig) and not c.whatsapp_signature_ok(body, "sha256=00")
    msgs = c.whatsapp_messages(json.loads(body))
    assert [(m.sender, m.text, m.name) for m in msgs] == [("+15551234567", "hola", "Ana")]


async def test_calendar_free_times_and_booking():
    fake = FakeServices()
    c = Connectors(_cfg(), transport=httpx.MockTransport(fake))
    day = (dt.date.today() + dt.timedelta(days=7))
    while day.weekday() == 6:
        day += dt.timedelta(days=1)
    fake.busy = [{"start": f"{day}T13:30:00Z", "end": f"{day}T15:00:00Z"}]      # 9:30-11:00 New York (EDT/EST aside)
    free = await c.free_times(day.isoformat(), 30)
    assert free["open"] and free["times"][0] == "9:00 AM" and len(free["times"]) >= 2
    sunday = day + dt.timedelta(days=(6 - day.weekday()))
    assert (await c.free_times(sunday.isoformat()))["open"] is False

    fake.busy = []
    booked = await c.book(f"{day}T10:00", "Cleaning for Sam", 30, "sam@example.test")
    assert booked["booked"] and booked["event_id"] == "evt1"
    event = json.loads(fake.seen[-1].content)
    assert event["attendees"] == [{"email": "sam@example.test"}] and fake.seen[-1].url.params["sendUpdates"] == "all"
    assert (await c.book(f"{day}T16:00", "Late", 30))["reason"] == "outside opening hours"
    fake.busy = [{"start": "2000-01-01T00:00:00Z", "end": "2100-01-01T00:00:00Z"}]
    assert (await c.book(f"{day}T10:00", "Taken", 30))["reason"] == "that time is taken"
    assert open_days("mon-fri,sun") == {0, 1, 2, 3, 4, 6}


async def test_gmail_and_smtp_send():
    fake = FakeServices()
    c = Connectors(_cfg(), transport=httpx.MockTransport(fake))
    out = await c.send_email("Sam <sam@example.test>", "Your booking", "See you Tuesday.")
    raw = json.loads(fake.seen[-1].content)["raw"]
    text = base64.urlsafe_b64decode(raw).decode()
    assert out["sent"] and "To: sam@example.test" in text and "Subject: Your booking" in text

    sent = []

    class FakeSMTP:
        def __init__(self, host, port, **kw):
            self.host, self.port = host, port
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def login(self, user, password):
            assert (user, password) == ("desk@example.test", "mail-pass-123456")
        def send_message(self, msg):
            sent.append(msg)

    smtp = Connectors(_cfg(email_provider="smtp", email_address="desk@example.test", email_password="mail-pass-123456",
                           smtp_host="smtp.example.test"), smtp_factory=FakeSMTP)
    await smtp.send_email("sam@example.test", "Hi", "Body")
    assert sent[0]["To"] == "sam@example.test"
    with pytest.raises(ConnectorError, match="email address"):
        await smtp.send_email("not-an-address", "x", "y")


async def test_connector_tools_in_a_conversation():
    fake = FakeServices()
    connectors = Connectors(_cfg(), transport=httpx.MockTransport(fake))
    tools = parse_all([
        {"name": "text_back", "connector": "telegram.send", "description": "Send the caller a Telegram message."},
        {"name": "confirm", "connector": "whatsapp.send", "with": {"to": "{{caller_phone}}"}, "mock": {"sent": True}},
        {"name": "open_times", "connector": "calendar.free_times"},
    ])
    assert [p.name for p in tools["text_back"].params] == ["text"]      # the recipient is never the model's
    assert [p.name for p in tools["open_times"].params] == ["day", "duration_minutes"]

    in_telegram = ToolRunner({}, connectors=connectors, channel={"kind": "telegram", "to": "42"})
    r = await in_telegram.run(tools["text_back"], {"text": "Booked!", "chat_id": "999"}, {})
    assert r.ok and json.loads(fake.seen[-1].content) == {"chat_id": "42", "text": "Booked!"}

    on_a_call = ToolRunner({}, connectors=connectors)
    r = await on_a_call.run(tools["text_back"], {"text": "x"}, {})
    assert not r.ok and "no chat_id" in r.error
    r = await on_a_call.run(tools["confirm"], {"text": "See you at 9"}, {"caller_phone": "+15551234567"})
    assert r.ok and json.loads(fake.seen[-1].content)["to"] == "15551234567"
    for _ in range(4):
        await on_a_call.run(tools["confirm"], {"text": "again"}, {"caller_phone": "+15551234567"})
    r = await on_a_call.run(tools["confirm"], {"text": "spam"}, {"caller_phone": "+15551234567"})
    assert not r.ok and "already sent 5" in r.error

    mocked = await ToolRunner(mock=True, connectors=connectors).run(tools["confirm"], {"text": "x"}, {})
    assert mocked.ok and mocked.mock
    no_mock = await ToolRunner({}, live=False).run(tools["open_times"], {"day": "2026-10-20"}, {})
    assert not no_mock.ok and no_mock.mock


def test_connector_definition_rules():
    errs = lambda t: [m for s, m in toolspec.problems(t) if s == "error"]  # noqa: E731
    warns = lambda t: [m for s, m in toolspec.problems(t) if s == "warning"]  # noqa: E731
    assert errs({"name": "x", "connector": "fax.send"})
    assert any("no argument" in e for e in errs({"name": "x", "connector": "email.send", "with": {"cc": "a"}}))
    assert any("secrets" in e for e in errs({"name": "x", "connector": "email.send", "with": {"to": "{{secrets.X}}"}}))
    assert any("connector tool takes" in e for e in errs({"name": "x", "connector": "email.send", "url": "https://a.test"}))
    assert any("message conversation" in w for w in warns({"name": "x", "connector": "email.send"}))
    assert errs({"name": "x", "connector": "email.send", "with": {"to": "{{caller_email}}"}}) == []


class StatelessMcp(FakeMcp):
    """A server on the 2026-07-28 spec: no initialize, the version rides on every request."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        msg = json.loads(request.content)
        self.calls.append({"method": msg.get("method"), "headers": dict(request.headers), "params": msg.get("params")})
        if msg.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "Method not found"}})
        assert request.headers["mcp-protocol-version"] == "2026-07-28" and request.headers["mcp-method"] == msg["method"]
        assert msg["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        if msg["method"] == "server/discover":
            return self._reply(msg, {"capabilities": {"tools": {}}})
        if msg["method"] == "tools/call":
            assert request.headers["mcp-name"] == msg["params"]["name"]
        self.sessions = 0
        return super().__call__(request) if msg["method"] != "tools/list" else self._reply(
            msg, {"tools": [{"name": "find-slots", "inputSchema": {"type": "object"}}]})

    def _reply(self, msg, result, headers=None, error=None):
        return super()._reply(msg, result, None, error)


async def test_mcp_client_falls_back_to_stateless_servers():
    server = StatelessMcp()
    client = McpClient("https://mcp.example.test/mcp", {"Authorization": "Bearer mcp-key-123456"}, transport=httpx.MockTransport(server))
    assert [t["name"] for t in await client.list_tools()] == ["find-slots"]
    assert [c["method"] for c in server.calls[:3]] == ["initialize", "server/discover", "tools/list"]
