"""Live-agent transfer: an end step hands the caller to a person, with what the agent collected."""

import json
import re
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import httpx
import pytest
from starlette.testclient import TestClient

import voicefakes as vf
from conftest import write_agent
from vpengine import transfer as tr
from vpengine.graph import Graph, render
from vpengine.server import Engine, create_app
from vpengine.session import TextSession
from vpengine.twilio import signature

PUBLIC = "https://calls.example.com"


def end_step(**data):
    return SimpleNamespace(kind="end", name="Front desk", data={"name": "Front desk", **data})


# -- what goes on the wire -------------------------------------------------------------------------

def test_targets_must_be_a_number_or_a_sip_address():
    assert tr.valid_target("+15551234567") and tr.valid_target("sip:desk@pbx.example.com")
    assert tr.valid_target("sip:4000@10.0.0.5:5060;transport=tcp") and tr.valid_target("sips:queue@cc.example.com")
    for bad in ("5551234567", "+1 555 123 4567", "desk@pbx", "sip:desk", "tel:+15551234567", "sip:a@b c"):
        assert not tr.valid_target(bad), bad
    with pytest.raises(ValueError, match="isn't a phone number"):
        tr.build(end_step(transfer_to="555-1234"), {}, "s1", render)
    assert tr.build(end_step(), {}, "s1", render) is None
    assert tr.build(SimpleNamespace(kind="agent", name="x", data={"transfer_to": "+15551234567"}), {}, "s1", render) is None


def test_details_are_safe_one_line_ascii_headers():
    variables = {"business_name": "Riverside", "caller_name": "José\r\nX-Evil: 1", "is_new": True,
                 "times": ["9am", "10am"], "notes": "x" * 500, "empty": "", "queue": "billing"}
    t = tr.build(end_step(transfer_to="sip:{{queue}}@pbx.example.com", transfer_details=["caller_name", "is_new", "times", "notes", "empty"],
                          transfer_uui="acct {{queue}}"), variables, "s1", render)
    assert t.to == "sip:billing@pbx.example.com"
    h = t.headers()
    assert h["X-Lampline-Session"] == "s1"
    assert h["X-Lampline-caller_name"] == "Jose X-Evil: 1"            # no header injection, no accents lost to ?
    assert h["X-Lampline-is_new"] == "yes" and h["X-Lampline-times"] == "9am, 10am"
    assert len(h["X-Lampline-notes"]) == 200 and "X-Lampline-empty" not in h
    assert t.uui == "acct billing" and t.uui_header() == "acct billing".encode().hex() + ";encoding=hex"


def test_without_a_list_it_sends_what_the_call_collected():
    t = tr.build(end_step(transfer_to="+15551234567"), {"business_name": "Riverside", "caller_name": "Sam"}, "s1",
                 render, collected=["caller_name"])
    assert t.details == {"caller_name": "Sam"}


def test_each_carrier_gets_its_own_form():
    sip = tr.Transfer(to="sip:desk@pbx.example.com", session_id="s1", details={"caller_name": "Sam R"}, uui="42")
    twiml = sip.twiml()
    uri = unquote(re.search(r"<Sip>(.*)</Sip>", twiml).group(1).replace("&amp;", "&"))
    q = parse_qs(urlsplit(uri).query)
    assert uri.startswith("sip:desk@pbx.example.com?") and q["X-Lampline-caller_name"] == ["Sam R"]
    assert q["X-Lampline-Session"] == ["s1"] and q["User-to-User"] == ["3432;encoding=hex"]
    assert 'answerOnBridge="true"' in twiml
    phone = tr.Transfer(to="+15551234567", session_id="s1", details={"caller_name": "Sam"})
    assert "<Dial answerOnBridge=\"true\"><Number>+15551234567</Number></Dial>" in phone.twiml()

    dial, hangup = sip.jambonz_verbs()
    assert dial["target"] == [{"type": "sip", "sipUri": "sip:desk@pbx.example.com"}] and hangup == {"verb": "hangup"}
    assert dial["headers"]["X-Lampline-caller_name"] == "Sam R" and dial["headers"]["User-to-User"].endswith(";encoding=hex")
    assert "headers" not in phone.jambonz_verbs()[0]                  # the phone network carries no headers

    body = sip.telnyx_body()
    assert {"name": "X-Lampline-Session", "value": "s1"} in body["custom_headers"]
    assert body["sip_headers"] == [{"name": "User-to-User", "value": "3432;encoding=hex"}]
    assert phone.telnyx_body() == {"to": "+15551234567"}


def test_pending_transfers_are_claimed_once_and_expire():
    p = tr.PendingTransfers(ttl=0.05)
    t = tr.Transfer(to="+15551234567", session_id="s1")
    p.add("CA1", t)
    assert p.pop("CA2") is None and p.pop("CA1") is t and p.pop("CA1") is None
    p.add("CA1", t)
    time.sleep(0.1)
    assert p.pop("CA1") is None


# -- a call that reaches the step ------------------------------------------------------------------

def handoff_definition(**transfer):
    """Start greets, then hands straight to a scripted end step that transfers."""
    return {
        "nodes": [
            {"id": "1", "type": "startCall", "data": {"name": "Start", "prompt": "Greet.", "greeting": "Hello."}},
            {"id": "2", "type": "endCall", "data": {"name": "Front desk", "mode": "scripted",
                                                     "say": "Connecting you now.", **transfer}},
        ],
        "edges": [{"id": "a", "source": "1", "target": "2", "data": {"label": "hand over", "after_greeting": True}}],
    }


async def test_a_chat_reports_the_transfer_and_ends(cfg):
    g = Graph.from_definition("Mini", handoff_definition(transfer_to="+15551234567"), {"business_name": "Riverside"})
    s = TextSession(g, cfg, variables={"caller_name": "Sam"})
    try:
        events = await s.open()
    finally:
        await s.close()
    (t,) = [e for e in events if e["type"] == "transfer"]
    assert t["to"] == "+15551234567" and t["details"] == {"caller_name": "Sam"} and t["session_id"] == s.id
    (end,) = [e for e in events if e["type"] == "call_ended"]
    assert end["reason"] == "transferred to +15551234567" and end["transfer"]["to"] == "+15551234567"
    assert s.transfer_to.to == "+15551234567"


async def test_a_bad_address_hangs_up_and_says_why(cfg):
    g = Graph.from_definition("Mini", handoff_definition(transfer_to="the front desk"), {})
    s = TextSession(g, cfg)
    try:
        events = await s.open()
    finally:
        await s.close()
    assert any("isn't a phone number" in e.get("message", "") for e in events if e["type"] == "execution_error")
    assert [e["reason"] for e in events if e["type"] == "call_ended"] == ["end step reached"]


@pytest.fixture
def handoff_agent(agents_dir):
    write_agent(agents_dir / "handoff", handoff_definition(transfer_to="sip:desk@pbx.example.com",
                                                           transfer_details=["caller_phone"]), name="Handoff")
    return "handoff"


def wait_for(cond, timeout=20):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        time.sleep(0.05)
    assert cond()


def test_twilio_closes_the_stream_and_dials_the_desk(cfg, agents_dir, handoff_agent):
    from test_twilio import TOKEN, start_msg, stream_token
    engine = Engine(agents_dir, cfg, public_url=PUBLIC, twilio_auth_token=TOKEN, twilio_hang_up=True,
                    speech_factory=lambda: (vf.FakeSTT(), vf.FakeTTS()))
    with TestClient(create_app(engine), base_url=PUBLIC) as c:
        form = {"CallSid": "CA9", "AccountSid": "AC1", "From": "+15550100", "To": "+15550199"}
        path = f"/v1/twilio/voice?agent={handoff_agent}"
        twiml = c.post(path, content=urlencode(form), headers={
            "Content-Type": "application/x-www-form-urlencoded", "X-Twilio-Signature": signature(TOKEN, PUBLIC + path, form)}).text
        assert twiml.endswith(f'</Connect><Redirect method="POST">{PUBLIC}/v1/twilio/after</Redirect></Response>')
        with c.websocket_connect("/v1/twilio/stream") as ws:
            ws.send_text(json.dumps({"event": "connected", "protocol": "Call", "version": "1.0.0"}))
            ws.send_text(json.dumps(start_msg(stream_token(twiml), call_sid="CA9")))
            wait_for(lambda: any(s.finished.is_set() for s in engine.sessions.values()))
        session = next(iter(engine.sessions.values()))
        assert session.transfer_to.to == "sip:desk@pbx.example.com"

        after = {"CallSid": "CA9", "AccountSid": "AC1", "CallStatus": "in-progress"}
        sign = {"Content-Type": "application/x-www-form-urlencoded",
                "X-Twilio-Signature": signature(TOKEN, PUBLIC + "/v1/twilio/after", after)}
        dial = c.post("/v1/twilio/after", content=urlencode(after), headers=sign).text
        assert "<Dial answerOnBridge=\"true\"><Sip>sip:desk@pbx.example.com?" in dial
        assert "X-Lampline-caller_phone=%2B15550100" in dial              # the caller's number, as a detail
        assert c.post("/v1/twilio/after", content=urlencode(after), headers=sign).text.endswith("<Hangup/></Response>")
        assert c.post("/v1/twilio/after", content=urlencode(after), headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 403


def test_jambonz_dials_the_desk_when_the_stream_ends(cfg, agents_dir, handoff_agent):
    from dataclasses import replace
    from test_jambonz import CALL, SECRET, sign
    engine = Engine(agents_dir, replace(cfg, jambonz_webhook_secret=SECRET), public_url=PUBLIC, twilio_hang_up=True,
                    speech_factory=lambda: (vf.FakeSTT(), vf.FakeTTS()))
    with TestClient(create_app(engine), base_url=PUBLIC) as c:
        body = json.dumps(CALL, separators=(",", ":")).encode()
        listen = c.post(f"/v1/jambonz/call?agent={handoff_agent}", content=body,
                        headers={"Content-Type": "application/json", **sign(body)}).json()[0]
        token = listen["url"].split("?t=", 1)[1]
        with c.websocket_connect(f"/v1/jambonz/stream?t={token}", subprotocols=["audio.jambonz.org"]) as ws:
            ws.send_text(json.dumps({"callSid": "jz-1", "sampleRate": 8000, "metadata": {"t": token}}))
            wait_for(lambda: any(s.finished.is_set() for s in engine.sessions.values()))
        done = json.dumps({"call_sid": "jz-1", "listen_status": "completed"}).encode()
        verbs = c.post("/v1/jambonz/done", content=done, headers={"Content-Type": "application/json", **sign(done)}).json()
        assert verbs[0]["verb"] == "dial" and verbs[0]["target"] == [{"type": "sip", "sipUri": "sip:desk@pbx.example.com"}]
        assert verbs[0]["headers"]["X-Lampline-caller_phone"] == "+15550100"
        assert c.post("/v1/jambonz/done", content=done, headers={"Content-Type": "application/json", **sign(done)}).json() == [{"verb": "hangup"}]


def test_telnyx_asks_call_control_to_transfer(cfg, agents_dir, handoff_agent, monkeypatch):
    from dataclasses import replace
    from test_telnyx import PUBKEY, sign
    sent = []

    async def fake_post(self, url, **kw):
        finished = any(s.finished.is_set() for s in engine.sessions.values())
        sent.append((url, kw.get("json"), kw.get("headers"), finished))
        return httpx.Response(200, json={"data": {"result": "ok"}})

    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    engine = Engine(agents_dir, replace(cfg, telnyx_public_key=PUBKEY, telnyx_api_key="KEY_test"), public_url=PUBLIC,
                    twilio_hang_up=True, speech_factory=lambda: (vf.FakeSTT(), vf.FakeTTS()))
    with TestClient(create_app(engine), base_url=PUBLIC) as c:
        body = urlencode({"CallSid": "v3:abc", "From": "+15550100", "To": "+15550199"}).encode()
        texml = c.post(f"/v1/telnyx/voice?agent={handoff_agent}", content=body,
                       headers={"Content-Type": "application/x-www-form-urlencoded", **sign(body)}).text
        token = re.search(r"\?t=([^\"&]+)", texml).group(1)
        start = {"event": "start", "sequence_number": "1", "stream_id": "st-1",
                 "start": {"call_control_id": "v3:ctl", "media_format": {"encoding": "PCMU", "sample_rate": 8000, "channels": 1}}}
        with c.websocket_connect(f"/v1/telnyx/stream?t={token}") as ws:
            ws.send_text(json.dumps({"event": "connected", "version": "1.0.0"}))
            ws.send_text(json.dumps(start))
            wait_for(lambda: any(s.finished.is_set() for s in engine.sessions.values()))
            wait_for(lambda: any(s[0].endswith("/actions/transfer") for s in sent))
    transfers = [s for s in sent if s[0].endswith("/actions/transfer")]
    assert transfers, sent
    url, payload, headers, after_the_call = transfers[0]
    assert after_the_call                       # found in review: only once the agent's last line has played
    assert url == "https://api.telnyx.com/v2/calls/v3:ctl/actions/transfer" and headers["Authorization"] == "Bearer KEY_test"
    assert payload["to"] == "sip:desk@pbx.example.com"
    assert {"name": "X-Lampline-caller_phone", "value": "+15550100"} in payload["custom_headers"]
    assert not [s for s in sent if s[0].endswith("/actions/hangup")]     # the transfer has the call, no hang-up


def test_a_variable_inside_an_address_cant_add_parameters_or_hosts():
    from types import SimpleNamespace
    from vpengine import transfer
    from vpengine.graph import render
    step = SimpleNamespace(kind="end", name="Desk", data={"transfer_to": "sip:{{ext}}@pbx.example.com"})
    t = transfer.build(step, {"ext": "1000;transport=tcp?X-Lampline-Session=forged&Foo=bar@evil.example"}, "sid", render)
    assert t.to == "sip:1000transporttcpX-Lampline-SessionforgedFoobarevil.example@pbx.example.com"
    whole = SimpleNamespace(kind="end", name="Desk", data={"transfer_to": "{{desk}}"})
    assert transfer.build(whole, {"desk": "sip:billing@pbx.example.com"}, "sid", render).to == "sip:billing@pbx.example.com"
