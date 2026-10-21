"""Things found in the pre-publication security review, kept fixed."""

from __future__ import annotations

import httpx
import pytest

from conftest import write_agent
from vpengine.server import Engine, MESSAGE_STARTS, create_app


@pytest.fixture
async def app_and_engine(cfg, tmp_path):
    eng = Engine(tmp_path / "agents", cfg, api_key="k-123", data_dir=tmp_path / "data")
    yield create_app(eng), eng
    await eng.shutdown()


def client(app, host="127.0.0.1"):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=(host, 5000)), base_url="http://localhost")


async def test_pages_cannot_be_framed(app_and_engine):
    app, _ = app_and_engine
    async with client(app) as c:
        r = await c.get("/")
    assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"


async def test_the_key_never_goes_in_a_url(app_and_engine):
    app, _ = app_and_engine
    async with client(app) as c:
        assert (await c.get("/v1/agents", params={"key": "k-123"})).status_code == 401
        assert (await c.get("/v1/agents", headers={"X-API-Key": "k-123"})).status_code == 200
        # The live event stream takes a token for its one session, not the API key (proxy logs keep URLs).
        assert (await c.get("/v1/sessions/nope/events", params={"key": "k-123"})).status_code == 401
        assert (await c.get("/v1/sessions/nope/events", params={"token": "guess"})).status_code == 401


async def test_webhooks_refuse_large_bodies_before_checking_signatures(app_and_engine):
    app, _ = app_and_engine
    big = b"x" * (65 * 1024)
    async with client(app, "203.0.113.9") as c:
        codes = {path: (await c.post(path, content=big)).status_code
                 for path in ("/v1/twilio/voice", "/v1/telnyx/voice", "/v1/whatsapp/webhook")}
    assert all(code in (413, 503) for code in codes.values()), codes     # 503: not set up, so never read at all
    assert 413 in codes.values()


async def test_strangers_writing_in_cannot_take_every_line(cfg, tmp_path):
    from fastapi import HTTPException
    write_agent(tmp_path / "agents" / "clinic", {
        "nodes": [{"id": "s", "type": "startCall", "data": {"name": "Start", "greeting": "Hi.", "prompt": "Greet."}},
                  {"id": "e", "type": "endCall", "data": {"name": "End", "prompt": "Bye."}}],
        "edges": [{"id": "x", "source": "s", "target": "e", "data": {"label": "done", "condition": "The caller is done."}}]})
    eng = Engine(tmp_path / "agents", cfg, max_sessions=4, data_dir=tmp_path / "data")
    graph = eng.load_agent("clinic")
    for i in range(2):                                                # half the room for message conversations
        eng.new_text(graph, {}, channel={"kind": "email", "to": f"a{i}@example.com"}, meta={"channel": "email"})
    with pytest.raises(HTTPException) as exc:
        eng.new_text(graph, {}, channel={"kind": "email", "to": "b@example.com"}, meta={"channel": "email"})
    assert exc.value.status_code == 429
    eng.new_text(graph, {})                                           # a call or test chat still gets in
    assert MESSAGE_STARTS >= 1
    await eng.shutdown()


async def test_in_a_container_the_key_is_what_proves_its_this_computer(cfg, tmp_path):
    eng = Engine(tmp_path / "agents", cfg, api_key="k-123", data_dir=tmp_path / "data", container=True, listen_host="0.0.0.0")
    app = create_app(eng)
    async with client(app, "172.18.0.1") as c:                        # Docker's gateway, as a published port shows it
        assert (await c.get("/v1/data")).status_code == 401
        assert (await c.get("/v1/data", headers={"X-API-Key": "k-123"})).status_code == 200
        r = await c.get("/v1/data", headers={"X-API-Key": "k-123", "cf-connecting-ip": "198.51.100.7"})
        assert r.status_code == 403                                   # through a tunnel: never "this computer"
    plain = Engine(tmp_path / "agents", cfg, api_key="k-123", data_dir=tmp_path / "data2")
    async with client(create_app(plain), "172.18.0.1") as c:
        assert (await c.get("/v1/data", headers={"X-API-Key": "k-123"})).status_code == 403   # not in a container
    await eng.shutdown()
    await plain.shutdown()
