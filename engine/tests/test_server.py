import re

import httpx
import pytest

from conftest import mini_definition, write_agent
from vpengine.server import Engine, create_app
from vpengine.services import Config


@pytest.fixture
def make_client(cfg, agents_dir):
    clients = []

    def make(api_key="", cfg_=None, max_sessions=20):
        engine = Engine(agents_dir, cfg_ or cfg, api_key=api_key, max_sessions=max_sessions)
        c = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost",
                              timeout=60)
        clients.append((c, engine))
        return c

    yield make


@pytest.fixture
async def client(make_client):
    c = make_client()
    yield c
    await c.aclose()


async def test_health_has_no_secrets(client, cfg):
    r = (await client.get("/v1/health")).json()
    assert r["ok"] and r["config"]["llm"] == "openai" and not r["auth_required"]
    assert "key" not in str(r["config"]).lower().replace("voice_ready", "")


async def test_agents_listing(client, agents_dir):
    (agents_dir / "broken").mkdir()
    (agents_dir / "broken/agent.yaml").write_text("nodes: [")
    r = (await client.get("/v1/agents")).json()
    slugs = {a["slug"]: a for a in r["agents"]}
    fd = slugs["front-desk"]
    assert fd["name"] == "Front Desk" and "Router" in fd["steps"] and fd["tests"] == 5 and fd["has_persona"]
    router = next(s for s in fd["graph"] if s["name"] == "Router")
    assert router["kind"] == "agent" and {"to": "Appointments", "label": "appointments"}.items() <= router["routes"][0].items()
    assert next(s for s in fd["graph"] if s["kind"] == "start")["greeting"]
    assert "error" in slugs["broken"]


async def test_chat_round_trip(client):
    r = await client.post("/v1/chat/sessions", json={"agent": "front-desk"})
    assert r.status_code == 200, r.text
    body = r.json()
    sid = body["session_id"]
    assert body["current_step"] == "Greeting" and not body["ended"]
    r = (await client.post(f"/v1/chat/sessions/{sid}/messages",
                           json={"text": "I need to move my appointment"})).json()
    assert r["current_step"] == "Appointments"
    info = (await client.get(f"/v1/sessions/{sid}")).json()
    assert info["steps"] == ["Greeting", "Router", "Appointments"] and info["events"]
    r = (await client.post(f"/v1/chat/sessions/{sid}/end")).json()
    assert r["ended"]
    assert (await client.post(f"/v1/chat/sessions/{sid}/messages", json={"text": "hi"})).status_code == 409


async def test_inline_definition(client):
    r = await client.post("/v1/chat/sessions", json={"name": "Mini", "definition": mini_definition(),
                                                     "context_variables": {"business_name": "Riverside"},
                                                     "context": {"business_name": "Hilltop"}})
    assert r.status_code == 200, r.text
    assert r.json()["events"][1]["text"] == "Hello from Hilltop."
    await client.post(f"/v1/chat/sessions/{r.json()['session_id']}/end")


@pytest.mark.parametrize("body,code", [
    ({"agent": "nope"}, 404),
    ({"agent": "../etc"}, 400),
    ({}, 400),
    ({"name": "x", "definition": {"nodes": [], "edges": []}}, 422),
])
async def test_bad_requests(client, body, code):
    assert (await client.post("/v1/chat/sessions", json=body)).status_code == code


async def test_api_key(make_client):
    c = make_client(api_key="s3cret")
    assert (await c.get("/v1/health")).json()["auth_required"]
    assert (await c.get("/v1/agents")).status_code == 401
    assert (await c.get("/v1/agents", headers={"X-API-Key": "wrong"})).status_code == 401
    assert (await c.get("/v1/agents", headers={"X-API-Key": "s3cret"})).status_code == 200
    assert (await c.get("/")).status_code == 200          # the page itself is public; it asks for the key
    await c.aclose()


async def test_session_limit(make_client):
    c = make_client(max_sessions=1)
    first = (await c.post("/v1/chat/sessions", json={"agent": "front-desk"})).json()
    assert (await c.post("/v1/chat/sessions", json={"agent": "front-desk"})).status_code == 429
    await c.post(f"/v1/chat/sessions/{first['session_id']}/end")
    second = await c.post("/v1/chat/sessions", json={"agent": "front-desk"})
    assert second.status_code == 200
    await c.post(f"/v1/chat/sessions/{second.json()['session_id']}/end")
    await c.aclose()


async def test_voice_needs_speech_set_up(client):
    r = await client.post("/v1/calls/webrtc/offer", json={"agent": "front-desk", "sdp": "x", "type": "offer"})
    assert r.status_code == 503 and "Integrations" in r.json()["detail"]


async def test_missing_llm_key_is_a_clear_error(make_client):
    c = make_client(cfg_=Config(llm="anthropic", anthropic_key=""))
    r = await c.post("/v1/chat/sessions", json={"agent": "front-desk"})
    assert r.status_code == 503 and "ANTHROPIC_API_KEY" in r.json()["detail"]
    await c.aclose()


async def test_app_pages_load_nothing_from_elsewhere(client):
    assert (await client.get("/call")).headers["location"] == "/#/test"
    page = await client.get("/")
    assert page.status_code == 200
    assets = re.findall(r'(?:src|href)="(/static/assets/[^"]+)"', page.text)
    assert any(a.endswith(".js") for a in assets) and any(a.endswith(".css") for a in assets)
    bundle = ""
    for asset in assets:
        r = await client.get(asset)
        assert r.status_code == 200, asset
        assert not re.search(r"(src|href)=[\"']https?://|url\(\s*['\"]?https?://|import\s*\(?\s*['\"]https?://", r.text)
        bundle += r.text
    assert "RTCPeerConnection" in bundle                       # browser calls are in the app
    font = await client.get("/static/fonts/ibm-plex-sans-latin-400-normal.woff2")
    assert font.status_code == 200
    assert not re.search(r"(src|href)=[\"']https?://", page.text)


async def test_other_hosts_and_cross_site_writes_are_refused(client):
    r = await client.get("/v1/health", headers={"host": "evil.example"})
    assert r.status_code == 421                       # DNS rebinding
    r = await client.post("/v1/chat/sessions", json={"agent": "front-desk"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = await client.post("/v1/chat/sessions", json={"agent": "front-desk"}, headers={"origin": "http://localhost"})
    assert r.status_code == 200
    await client.post(f"/v1/chat/sessions/{r.json()['session_id']}/end")


def test_normal_browser_call_warnings_are_quiet():
    from vpengine.server import _log_filter
    rec = lambda lvl, msg: {"level": type("L", (), {"name": lvl})(), "message": msg}
    keep = _log_filter("warning")
    assert not keep(rec("WARNING", "No screen video transceiver is available"))
    assert not keep(rec("WARNING", "Media stream error while reading the audio; clearing track."))
    assert keep(rec("WARNING", "Timeout: No audio frame received within the specified time."))
    assert keep(rec("ERROR", "Media stream error while reading the audio"))
    assert _log_filter("debug")(rec("WARNING", "No screen video transceiver is available"))


async def test_logos_are_optional_and_scoped(cfg, agents_dir, tmp_path):
    from vpengine.settings import SettingsStore
    store = SettingsStore(tmp_path / "s.json", env={})
    engine = Engine(agents_dir, cfg, store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost") as c:
        assert (await c.get("/v1/logos")).json()["logos"] == []
        (tmp_path / "logos").mkdir()
        (tmp_path / "logos" / "twilio.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'/>")
        (tmp_path / "secret.txt").write_text("nope")
        assert (await c.get("/v1/logos")).json()["logos"] == ["twilio.svg"]
        r = await c.get("/v1/logos/twilio.svg")
        assert r.status_code == 200 and r.headers["content-type"] == "image/svg+xml" and "sandbox" in r.headers["content-security-policy"]
        for bad in ("..%2Fsecret.txt", "secret.txt", "Twilio.svg", "x.html"):
            assert (await c.get(f"/v1/logos/{bad}")).status_code == 404


async def test_app_files_are_revalidated(client):
    """The built UI keeps the same file names between versions, so browsers must check for a newer copy."""
    r = await client.get("/static/index.html")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"
