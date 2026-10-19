"""The builder's API: read, save and create agent folders."""

import json

import httpx
import pytest
import yaml

from vpengine.server import Engine, create_app


@pytest.fixture
async def c(cfg, agents_dir, tmp_path, monkeypatch):
    monkeypatch.setattr("vpengine.builder.CACHE", tmp_path / "cache")
    engine = Engine(agents_dir, cfg)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost",
                                 timeout=30) as client:
        client.agents_dir = agents_dir
        client.cache = tmp_path / "cache"
        yield client


async def test_read_agent_with_inline_prompts_and_findings(c):
    a = (await c.get("/v1/agents/front-desk")).json()
    router = next(n for n in a["definition"]["nodes"] if n["data"]["name"] == "Router")
    assert "pick the matching path" in router["data"]["prompt"]          # prompt text, not a file pointer
    assert a["context_variables"]["business_name"] == "Riverside Clinic"
    assert a["findings"] == []


async def test_save_moves_renames_and_rewires(c):
    a = (await c.get("/v1/agents/front-desk")).json()
    d = a["definition"]
    billing = next(n for n in d["nodes"] if n["data"]["name"] == "Billing")
    billing["data"]["name"] = "Payments"
    billing["data"]["prompt"] = "Help with payments."
    billing["position"] = {"x": 999.4, "y": 10}
    d["edges"] = [e for e in d["edges"] if e["target"] != billing["id"]]   # nothing reaches it any more
    r = await c.put("/v1/agents/front-desk", json={"name": "Front Desk 2", "definition": d,
                                                     "context_variables": a["context_variables"]})
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["name"] == "Front Desk 2"
    assert any(f["code"] == "VK008" and "Payments" in f["where"] for f in saved["findings"])
    folder = c.agents_dir / "front-desk"
    doc = yaml.safe_load((folder / "agent.yaml").read_text())
    node = next(n for n in doc["nodes"] if n["data"]["name"] == "Payments")
    assert node["position"] == {"x": 999, "y": 10} and node["data"]["prompt"] == {"file": "prompts/payments.md"}
    assert (folder / "prompts/payments.md").read_text() == "Help with payments.\n"
    assert not (folder / "prompts/billing.md").exists()                     # old prompt file removed
    assert doc["setup"]["real_data"] is False                               # local sections kept
    history = list((c.cache / "history" / "front-desk").glob("*.json"))
    assert len(history) == 1 and "prompts/billing.md" in json.loads(history[0].read_text())


@pytest.mark.parametrize("definition,message", [
    ({"nodes": "x", "edges": []}, "nodes and edges"),
    ({"nodes": [{"id": "1", "type": "rocket", "data": {}}], "edges": []}, "known type"),
    ({"nodes": [{"id": "1", "type": "agentNode", "data": {}}, {"id": "1", "type": "agentNode", "data": {}}],
      "edges": []}, "unique id"),
    ({"nodes": [{"id": "1", "type": "agentNode", "data": {}}],
      "edges": [{"id": "e", "source": "1", "target": "9", "data": {}}]}, "missing step"),
])
async def test_bad_definitions_are_refused(c, definition, message):
    r = await c.put("/v1/agents/front-desk", json={"name": "x", "definition": definition})
    assert r.status_code == 422 and message in r.json()["detail"]


async def test_bad_names_and_variables(c):
    a = (await c.get("/v1/agents/front-desk")).json()
    assert (await c.put("/v1/agents/front-desk", json={"name": "", "definition": a["definition"]})).status_code == 422
    r = await c.put("/v1/agents/front-desk", json={"name": "x", "definition": a["definition"],
                                                     "context_variables": {"bad name": "1"}})
    assert r.status_code == 422


async def test_create_blank_and_from_template(c):
    r = await c.post("/v1/agents", json={"name": "My New Agent"})
    assert r.status_code == 200 and r.json()["slug"] == "my-new-agent" and r.json()["findings"] == []
    kinds = sorted(n["type"] for n in r.json()["definition"]["nodes"])
    assert kinds == ["agentNode", "agentNode", "endCall", "globalNode", "startCall"]
    again = (await c.post("/v1/agents", json={"name": "My New Agent"})).json()
    assert again["slug"] == "my-new-agent-2"
    t = (await c.post("/v1/agents", json={"name": "Spanish desk", "template": "bilingual-front-desk"})).json()
    assert t["name"] == "Spanish desk" and (c.agents_dir / "spanish-desk" / "tests").is_dir()
    assert (await c.post("/v1/agents", json={"name": "x", "template": "nope"})).status_code == 422
    assert "front-desk" in (await c.get("/v1/templates")).json()["templates"]


async def test_new_agent_answers_a_chat(c):
    slug = (await c.post("/v1/agents", json={"name": "Chat me"})).json()["slug"]
    r = (await c.post("/v1/chat/sessions", json={"agent": slug})).json()
    assert r["events"][1]["text"] == "Thanks for calling Example Clinic. How can I help you today?"
    # Start greets, then (after_greeting) hands straight to the root agent, which takes the first words.
    assert r["events"][2]["type"] == "tool_call_result" and r["events"][2]["result"]["next_step"] == "Root agent"
    await c.post(f"/v1/chat/sessions/{r['session_id']}/end")


async def test_check_lints_a_draft_without_saving(c):
    a = (await c.get("/v1/agents/front-desk")).json()
    before = (c.agents_dir / "front-desk" / "agent.yaml").read_text()
    d = a["definition"]
    d["edges"].append({"id": "bad", "source": d["edges"][0]["target"], "target": d["edges"][0]["source"],
                       "data": {"label": "", "condition": ""}})
    r = await c.post("/v1/agents/front-desk/check", json={"name": a["name"], "definition": d,
                                                           "context_variables": a["context_variables"]})
    codes = {f["code"] for f in r.json()["findings"]}
    assert {"VK005", "VK007"} <= codes
    assert (c.agents_dir / "front-desk" / "agent.yaml").read_text() == before


async def test_saving_is_local_only(c):
    a = (await c.get("/v1/agents/front-desk")).json()
    r = await c.put("/v1/agents/front-desk", json={**a}, headers={"cf-connecting-ip": "203.0.113.1"})
    assert r.status_code == 403
    assert (await c.post("/v1/agents", json={"name": "x"}, headers={"x-forwarded-for": "1.2.3.4"})).status_code == 403
    assert (await c.get("/v1/agents/../etc")).status_code in (400, 404)


async def test_describe_it_builds_an_agent(c):
    r = await c.post("/v1/agents/generate", json={"description": "A front desk for a small dental office that books appointments."})
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["slug"] == "hilltop-dental-desk" and a["context_variables"]["business_hours"].startswith("Monday")
    names = [n["data"]["name"] for n in a["definition"]["nodes"]]
    assert names == ["Start", "Root agent", "Appointments", "Questions", "Goodbye", "Global instructions"]
    appt = next(n for n in a["definition"]["nodes"] if n["data"]["name"] == "Appointments")
    assert appt["data"]["tools"] == ["find_open_times"] and appt["data"]["extraction_variables"][0]["name"] == "preferred_day"
    assert a["tools"][0]["mock"] == {"times": ["9:30 AM", "2:00 PM"]} and a["tools"][0]["url"] == ""
    assert [f for f in a["findings"] if f["severity"] == "error"] == []
    chat = (await c.post("/v1/chat/sessions", json={"agent": a["slug"], "mock_tools": True})).json()
    assert chat["events"][1]["text"].startswith("Thanks for calling Hilltop Dental")
    await c.post(f"/v1/chat/sessions/{chat['session_id']}/end")
    assert (await c.post("/v1/agents/generate", json={"description": "hi"})).status_code == 422
    assert (await c.post("/v1/agents/generate", json={"description": "A front desk agent please"},
                         headers={"cf-connecting-ip": "1.2.3.4"})).status_code == 403


async def test_import_a_cx_agent_studio_export(c):
    import io
    import json
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        files = {
            "Lakeside_Library/app.json": {"displayName": "Lakeside Library", "rootAgent": "Desk"},
            "Lakeside_Library/agents/Desk/Desk.json": {"displayName": "Desk", "instruction": "agents/Desk/instruction.txt",
                                                       "childAgents": ["Renewals"], "tools": ["end_session"]},
            "Lakeside_Library/agents/Desk/instruction.txt": "Greet the caller and ask how you can help with the library.",
            "Lakeside_Library/agents/Renewals/Renewals.json": {"displayName": "Renewals", "description": "Renewing borrowed books.",
                                                               "instruction": "Ask for the book title and renew it."},
        }
        for rel, doc in files.items():
            z.writestr(rel, doc if isinstance(doc, str) else json.dumps(doc))
    r = await c.post("/v1/agents/import?source=auto&filename=library.zip", content=buf.getvalue())
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["slug"] == "lakeside-library" and a["import"]["source"] == "CX Agent Studio"
    assert [n["data"]["name"] for n in a["definition"]["nodes"]] == ["Desk", "Renewals", "Start", "End session"]
    assert [f for f in a["findings"] if f["severity"] == "error"] == []
    chat = (await c.post("/v1/chat/sessions", json={"agent": a["slug"], "mock_tools": True})).json()
    assert chat["session_id"]
    await c.post(f"/v1/chat/sessions/{chat['session_id']}/end")

    assert (await c.post("/v1/agents/import", content=b"not a zip")).status_code == 422
    assert (await c.post("/v1/agents/import", content=b"")).status_code == 422
    assert (await c.post("/v1/agents/import?source=other", content=buf.getvalue())).status_code == 422
    assert (await c.post("/v1/agents/import", content=buf.getvalue(), headers={"x-forwarded-for": "1.2.3.4"})).status_code == 403


async def test_save_keeps_triggers_and_hook_nodes(c):
    a = (await c.get("/v1/agents/front-desk")).json()
    a["definition"]["nodes"].append({"id": "api", "type": "trigger", "position": {"x": -600, "y": 0},
                                     "data": {"name": "API Trigger", "enabled": True, "trigger_path": "remind"}})
    a["triggers"] = [{"id": "notes", "type": "after_call", "send": {"via": "email", "to": "desk@example.test", "text": "{{summary}}"}}]
    r = await c.put("/v1/agents/front-desk", json=a)
    assert r.status_code == 200, r.text
    saved = yaml.safe_load((c.agents_dir / "front-desk" / "agent.yaml").read_text())
    assert saved["triggers"][0]["id"] == "notes" and any(n["type"] == "trigger" for n in saved["nodes"])
    back = (await c.get("/v1/agents/front-desk")).json()
    assert back["triggers"] == a["triggers"] and [f for f in back["findings"] if f["severity"] == "error"] == []
    listed = next(x for x in (await c.get("/v1/agents")).json()["agents"] if x["slug"] == "front-desk")
    assert "API Trigger" not in listed["steps"]
    no_triggers = {k: v for k, v in back.items() if k != "triggers"}       # an older client keeps them
    assert (await c.put("/v1/agents/front-desk", json=no_triggers)).status_code == 200
    assert (await c.get("/v1/agents/front-desk")).json()["triggers"] == a["triggers"]
