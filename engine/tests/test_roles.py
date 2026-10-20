"""Users and roles: named keys with admin, builder and viewer roles."""

from __future__ import annotations

import json

import httpx
import pytest

from vpengine import keys
from vpengine.server import Engine, create_app
from vpengine.settings import SettingsStore

OWNER = "owner-key-123456"


@pytest.fixture
async def c(cfg, agents_dir, tmp_path):
    store = SettingsStore(tmp_path / "cfg" / "settings.json", env={})
    engine = Engine(agents_dir, cfg, api_key=OWNER, store=store, data_dir=tmp_path / "data")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost",
                                 timeout=30) as client:
        client.engine = engine
        client.store_dir = tmp_path / "cfg"
        yield client
    await engine.shutdown()


def as_key(k):
    return {"X-API-Key": k}


async def test_owner_creates_keys_and_roles_apply(c):
    assert (await c.get("/v1/me", headers=as_key(OWNER))).json() == {"name": "owner", "role": "admin", "auth": True}
    made = {}
    for name, role in (("Ana", "admin"), ("Ben", "builder"), ("Vic", "viewer")):
        r = (await c.post("/v1/keys", json={"name": name, "role": role}, headers=as_key(OWNER))).json()
        assert r["key"].startswith("lpk_") and r["role"] == role and r["hint"] == r["key"][-4:]
        made[role] = r
    saved = (c.store_dir / "keys.json").read_text()
    assert all(m["key"] not in saved for m in made.values())              # only hashes on disk
    assert oct((c.store_dir / "keys.json").stat().st_mode)[-3:] == "600"
    listed = (await c.get("/v1/keys", headers=as_key(OWNER))).json()["keys"]
    assert {k["name"] for k in listed} == {"Ana", "Ben", "Vic"} and all("hash" not in k and "key" not in k for k in listed)

    viewer, builder, admin = (as_key(made[r]["key"]) for r in ("viewer", "builder", "admin"))
    assert (await c.get("/v1/me", headers=viewer)).json() == {"name": "Vic", "role": "viewer", "auth": True}
    # everyone can read and try agents out
    for h in (viewer, builder, admin):
        assert (await c.get("/v1/agents", headers=h)).status_code == 200
        assert (await c.get("/v1/calls", headers=h)).status_code == 200
    chat = await c.post("/v1/chat/sessions", json={"agent": "front-desk"}, headers=viewer)
    assert chat.status_code == 200
    await c.post(f"/v1/chat/sessions/{chat.json()['session_id']}/end", headers=viewer)
    # a viewer can't change agents; a builder can
    agent = (await c.get("/v1/agents/front-desk", headers=builder)).json()
    r = await c.put("/v1/agents/front-desk", json=agent, headers=viewer)
    assert r.status_code == 403 and "viewer" in r.json()["detail"]
    assert (await c.post("/v1/data", json={"name": "people"}, headers=viewer)).status_code == 403
    assert (await c.post("/v1/data", json={"name": "people"}, headers=builder)).status_code == 200
    # settings, secrets and keys are for admins
    for h, code in ((builder, 403), (viewer, 403)):
        assert (await c.put("/v1/settings", json={"brand_name": "X"}, headers=h)).status_code == code
        assert (await c.put("/v1/secrets/SOME_KEY", json={"value": "v"}, headers=h)).status_code == code
        assert (await c.get("/v1/keys", headers=h)).status_code == code
        assert (await c.post("/v1/keys", json={"name": "Zed", "role": "admin"}, headers=h)).status_code == code
    assert (await c.put("/v1/secrets/SOME_KEY", json={"value": "v"}, headers=admin)).status_code == 200
    assert (await c.get("/v1/keys", headers=admin)).status_code == 200
    # revoking a key locks it out at once
    assert (await c.delete(f"/v1/keys/{made['viewer']['id']}", headers=admin)).json() == {"revoked": made["viewer"]["id"]}
    assert (await c.get("/v1/agents", headers=viewer)).status_code == 401
    assert (await c.get("/v1/agents", headers=as_key("lpk_not-a-real-key"))).status_code == 401


async def test_bad_requests(c):
    h = as_key(OWNER)
    assert (await c.post("/v1/keys", json={"name": "", "role": "admin"}, headers=h)).status_code == 422
    assert (await c.post("/v1/keys", json={"name": "Ok", "role": "root"}, headers=h)).status_code == 422
    assert (await c.post("/v1/keys", json={"name": "<script>", "role": "viewer"}, headers=h)).status_code == 422
    assert (await c.post("/v1/keys", json={"name": "Ok", "role": "viewer"}, headers=h)).status_code == 200
    assert (await c.post("/v1/keys", json={"name": "ok", "role": "viewer"}, headers=h)).status_code == 422   # same name
    assert (await c.delete("/v1/keys/nope", headers=h)).status_code == 404


async def test_named_keys_need_the_owner_key(cfg, agents_dir, tmp_path):
    engine = Engine(agents_dir, cfg, data_dir=tmp_path / "data")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost") as client:
        assert (await client.get("/v1/me")).json() == {"name": "owner", "role": "admin", "auth": False}
        r = await client.post("/v1/keys", json={"name": "Ana", "role": "admin"})
        assert r.status_code == 409 and "VPE_API_KEY" in r.json()["detail"]
    await engine.shutdown()


def test_role_rules():
    assert keys.allowed("viewer", "GET", "/v1/agents") and not keys.allowed("viewer", "GET", "/v1/keys")
    assert keys.allowed("viewer", "POST", "/v1/chat/sessions/abc/messages")
    assert not keys.allowed("viewer", "POST", "/v1/agents") and not keys.allowed("viewer", "DELETE", "/v1/calls/x")
    assert keys.allowed("builder", "POST", "/v1/agents") and not keys.allowed("builder", "DELETE", "/v1/calls/x")
    assert not keys.allowed("builder", "POST", "/v1/settings/check/openai")
    assert keys.allowed("admin", "DELETE", "/v1/keys/x")
    store = keys.KeyStore(None)
    rec, key = store.create("Ana", "viewer")
    assert store.match(key)["id"] == rec["id"] and store.match(key + "x") is None and store.match("nope") is None
    assert json.dumps(store.list()).count(key) == 0


async def test_remote_editing_is_opt_in(cfg, agents_dir, tmp_path):
    """Through a proxy, changes need VPE_REMOTE_EDITING=on; settings and secrets never leave this computer."""
    proxied = {"X-Forwarded-For": "203.0.113.9"}
    for remote in (False, True):
        store = SettingsStore(tmp_path / f"cfg{remote}" / "settings.json", env={})
        engine = Engine(agents_dir, cfg, api_key=OWNER, store=store, data_dir=tmp_path / f"data{remote}",
                        remote_editing=remote)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost") as c:
            builder = {"X-API-Key": (await c.post("/v1/keys", json={"name": "Ben", "role": "builder"}, headers=as_key(OWNER))).json()["key"]}
            r = await c.post("/v1/data", json={"name": "people"}, headers={**builder, **proxied})
            assert r.status_code == (200 if remote else 403), r.text
            for h in (builder, as_key(OWNER)):
                assert (await c.put("/v1/settings", json={"brand_name": "X"}, headers={**h, **proxied})).status_code == 403
                assert (await c.put("/v1/secrets/K", json={"value": "v"}, headers={**h, **proxied})).status_code == 403
        await engine.shutdown()
    assert keys.always_local("POST", "/v1/agents/a/triggers/t/run") and not keys.always_local("PUT", "/v1/agents/a")
