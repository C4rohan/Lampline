"""The engine's own records and knowledge: storage, search, the tools agents use, and the API."""

from __future__ import annotations

import io
import json
import zipfile

import httpx
import numpy as np
import pytest

from conftest import write_agent
from vk import agentfs
from vk.lint import lint
from vpengine.embed import Embedder
from vpengine.library import Library, LibraryError, chunk, extract_text
from vpengine.server import Engine, create_app

FAQ = """# Hours

We are open 8 to 6, Monday to Saturday. We're closed on Sundays and public holidays.

# Parking

Free parking is behind the building; the entrance is on Elm Street.

# Insurance

We accept most PPO plans and Medicare. Bring your insurance card to every visit.
"""


def fake_embedder() -> Embedder:
    """An "Ollama" that embeds by shared topic words, so meaning search is testable without a model."""
    topics = [("open", "hours", "weekend", "saturday", "sunday", "closed"), ("car", "park", "parking", "drive"),
              ("insurance", "medicare", "plan", "card", "cover")]

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        data = []
        for i, text in enumerate(body["input"]):
            t = text.lower()
            vec = [float(sum(w in t for w in words)) for words in topics] + [0.1]
            data.append({"index": i, "embedding": vec})
        return httpx.Response(200, json={"data": data})

    return Embedder("ollama", "http://embed.test/v1", "topic-words", transport=httpx.MockTransport(handler))


@pytest.fixture
def lib(tmp_path):
    out = Library(tmp_path / "data", fake_embedder())
    yield out
    out.close()


def docx(paragraphs: list[tuple[str, str]]) -> bytes:
    def para(style: str, text: str) -> str:
        head = '<w:pPr><w:pStyle w:val="%s"/></w:pPr>' % style if style else ""
        return f"<w:p>{head}<w:r><w:t>{text}</w:t></w:r></w:p>"
    body = "".join(para(style, text) for style, text in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document><w:body>{body}</w:body></w:document>')
    return buf.getvalue()


def pdf(line: str) -> bytes:
    """A one-page PDF with a line of text, with a proper cross-reference table."""
    content = f"BT /F1 12 Tf 20 100 Td ({line}) Tj ET".encode()
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 144]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
            b"<</Length %d>>stream\n" % len(content) + content + b"\nendstream", b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj" % i + o + b"endobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)
    return bytes(out)


# --- records --------------------------------------------------------------------------

def test_collections_and_records(lib):
    lib.create_collection("appointments", "Upcoming visits", key="confirmation")
    a = lib.put("appointments", {"confirmation": "HC-4271", "name": "Sam Lee", "status": "confirmed"})
    lib.put("appointments", {"confirmation": "HC-1180", "name": "Ana Ruiz", "status": "pending"})
    assert lib.find("appointments", {"confirmation": "hc 4271"})[0]["name"] == "Sam Lee"   # case, spaces, dashes
    again = lib.put("appointments", {"confirmation": "HC4271", "status": "cancelled"}, merge=True)  # same key: updated
    assert again["id"] == a["id"] and again["name"] == "Sam Lee" and again["status"] == "cancelled"
    assert lib.records("appointments")["total"] == 2
    assert lib.records("appointments", search="Ruiz")["total"] == 1
    info = lib.collections()[0]
    assert info["count"] == 2 and info["key"] == "confirmation" and "status" in info["fields"]
    assert lib.delete("appointments", a["id"]) and lib.get("appointments", a["id"]) is None
    assert lib.find("appointments", {"confirmation": ""}) == []
    with pytest.raises(LibraryError):
        lib.create_collection("Bad Name")
    with pytest.raises(LibraryError):
        lib.put("appointments", {"bad key": 1})
    with pytest.raises(LibraryError):
        lib.find("nope", {"a": 1})
    lib.delete_collection("appointments")
    assert lib.collections() == []


def test_import_csv_and_json(lib):
    lib.create_collection("members", key="member_id")
    assert lib.import_rows("members", b"Member ID,Plan\n1001,Gold\n1002,Silver\n", "m.csv") == {"imported": 2}
    assert lib.find("members", {"member_id": "1001"})[0]["plan"] == "Gold"        # headers become field names
    lib.import_rows("members", json.dumps([{"member_id": "1001", "plan": "Bronze"}]).encode(), "m.json")
    assert lib.records("members")["total"] == 2                     # same key: replaced, not added
    with pytest.raises(LibraryError):
        lib.import_rows("members", b"{not json", "m.json")


# --- knowledge ------------------------------------------------------------------------

def test_reading_documents():
    html = b"<html><head><style>x{}</style><script>alert(1)</script></head><body><h1>Hours</h1><p>Open at 8.</p></body></html>"
    text = extract_text("page.html", html)
    assert "alert" not in text and "# Hours" in text and "Open at 8." in text
    word = extract_text("faq.docx", docx([("Heading1", "Parking"), ("", "Behind the building &amp; free.")]))
    assert word.startswith("# Parking") and "Behind the building & free." in word
    assert "Plan: Gold" in extract_text("plans.csv", b"Plan,Price\nGold,10\n")
    assert "Flu shots are free on Fridays." in extract_text("flu.pdf", pdf("Flu shots are free on Fridays."))
    with pytest.raises(LibraryError, match="couldn't read that PDF"):
        extract_text("broken.pdf", b"%PDF-1.4 nothing here")
    with pytest.raises(LibraryError):
        extract_text("tool.exe", b"MZ")
    with pytest.raises(LibraryError):
        extract_text("x.docx", b"not a zip")
    parts = chunk(FAQ + "\n\n" + "A long line of words. " * 200)
    assert parts[0].startswith("Hours\n") and all(len(p) <= 1300 for p in parts)


async def test_knowledge_search_by_words_and_meaning(lib):
    lib.create_base("clinic-faq", "Hours, parking, insurance")
    doc = await lib.add_document("clinic-faq", "faq.md", FAQ.encode())
    assert doc["passages"] == 3 and doc["embedded"] == 3
    hits = await lib.search("clinic-faq", "Do you take Medicare?", 1)
    assert hits[0]["text"].startswith("Insurance") and hits[0]["source"] == "faq.md"
    hits = await lib.search("clinic-faq", "where do I leave the car", 1)    # no shared words; same meaning
    assert hits[0]["text"].startswith("Parking") and hits[0]["similarity"] > 0.5
    assert await lib.search("clinic-faq", "pizza toppings", 3) == []
    assert lib.bases()[0] == {"name": "clinic-faq", "description": "Hours, parking, insurance", "docs": 1,
                              "passages": 3, "embedded": 3}
    assert lib.delete_document("clinic-faq", doc["id"]) and lib.base("clinic-faq")["documents"] == []
    assert await lib.search("clinic-faq", "Medicare", 3) == []


async def test_keyword_search_without_embeddings(tmp_path):
    lib = Library(tmp_path / "d", Embedder("off"))
    lib.create_base("faq")
    await lib.add_document("faq", "faq.md", FAQ.encode())
    assert (await lib.search("faq", "parking entrance", 1))[0]["text"].startswith("Parking")
    assert lib.bases()[0]["embedded"] == 0
    lib.embedder = fake_embedder()                                  # turned on later: index what's there
    assert await lib.embed_missing("faq") == 3
    lib.close()


# --- in a conversation ---------------------------------------------------------------

def agent_with_lookups() -> dict:
    return {
        "nodes": [
            {"id": "start", "type": "startCall", "data": {"name": "Start", "greeting": "Harbor Clinic.", "prompt": "Greet."}},
            {"id": "find", "type": "agentNode", "data": {
                "name": "Find visit", "mode": "scripted", "interpret": "exact", "on_done": ["find_visit"],
                "form": [{"name": "confirmation", "type": "text", "ask": "What's your confirmation number?"}]}},
            {"id": "ok", "type": "endCall", "data": {"name": "Confirmed", "mode": "scripted",
                                                     "say": "{{patient}}, you're confirmed for {{visit_day}}."}},
            {"id": "help", "type": "agentNode", "data": {"name": "Help desk", "prompt": "Answer questions.",
                                                         "tools": ["search_clinic_faq", "log_callback"]}},
        ],
        "edges": [
            {"id": "e0", "source": "start", "target": "find", "data": {"label": "go", "condition": "now", "after_greeting": True}},
            {"id": "e1", "source": "find", "target": "ok", "data": {"label": "found", "condition": "-", "when": 'visit_status == "confirmed"'}},
            {"id": "e2", "source": "find", "target": "help", "data": {"label": "other", "condition": "-", "otherwise": True}},
            {"id": "e3", "source": "help", "target": "ok", "data": {"label": "done", "condition": "The caller is done."}},
        ],
    }


TOOLS = [
    {"name": "find_visit", "connector": "data.lookup", "description": "Find the visit.",
     "with": {"collection": "appointments", "field": "confirmation", "value": "{{confirmation}}", "fields": ["name", "day", "status"]},
     "save": {"visit_status": "records.0.status", "patient": "records.0.name", "visit_day": "records.0.day"}},
    {"name": "search_clinic_faq", "connector": "knowledge.search", "description": "Search the clinic FAQ about parking, hours and insurance.",
     "with": {"base": "clinic-faq"}},
    {"name": "log_callback", "connector": "data.save", "description": "Log a callback request.",
     "with": {"collection": "callbacks", "values": {"for": "{{patient}}", "reason": "{{reason}}"}, "ask": ["reason"]}},
]


@pytest.fixture
async def engine(cfg, tmp_path):
    made = []

    def make(definition):
        folder = write_agent(tmp_path / "agents" / "clinic", definition, {})
        a = agentfs.load(folder)
        a.tools = TOOLS
        agentfs.write(a, folder)
        eng = Engine(tmp_path / "agents", cfg, data_dir=tmp_path / "data")
        eng.library.embedder = fake_embedder()
        made.append(eng)
        return eng
    yield make
    for eng in made:
        for s in list(eng.sessions.values()):
            await s.close()
        await eng.shutdown()


def said(events):
    return [e["text"] for e in events if e.get("type") == "bot_speech"]


def test_lint_of_data_tools():
    a = agentfs.Agent(name="x", definition=agent_with_lookups(), tools=TOOLS)
    assert [f for f in lint(a) if f.severity == "error"] == []
    bad = json.loads(json.dumps(TOOLS))
    bad[0]["with"].pop("collection")
    bad[1]["with"]["base"] = "Clinic FAQ"
    bad[2]["with"]["ask"] = ["why"]
    d = agent_with_lookups()
    d["nodes"][1]["data"]["on_entry"] = ["no_such_tool"]
    msgs = [(f.code, f.severity, f.message) for f in lint(agentfs.Agent(name="x", definition=d, tools=bad))]
    assert any("needs collection" in m for _, _, m in msgs)
    assert any("with.base" in m for _, _, m in msgs)
    assert any("why isn't used" in m for _, _, m in msgs)
    assert ("VK133", "error", "on_entry runs no_such_tool, which this agent does not define") in msgs
    bad = json.loads(json.dumps(TOOLS))
    bad[0]["with"]["fields"] = ["status"]
    msgs = [m for _, _, m in [(f.code, f.severity, f.message) for f in lint(agentfs.Agent(name="x", definition=agent_with_lookups(), tools=bad))]]
    assert any("save.patient reads name, which with.fields leaves out" in m for m in msgs)


async def test_a_page_looks_up_a_record_and_routes_on_it(engine):
    eng = engine(agent_with_lookups())
    eng.library.create_collection("appointments", key="confirmation")
    eng.library.put("appointments", {"confirmation": "HC-4271", "name": "Sam", "day": "Tuesday", "status": "confirmed",
                                     "notes": "not for the model"})
    s = eng.new_text(eng.load_agent("clinic"), {})
    opening = await s.open()
    assert said(opening)[-1] == "What's your confirmation number?"
    events = await s.send("hc 4271")
    assert said(events) == ["Sam, you're confirmed for Tuesday."]
    looked = next(e for e in events if e["type"] == "tool_call_result" and e["function_name"] == "find_visit")
    assert "not for the model" not in json.dumps(looked)            # only the listed fields come back
    assert s.flow.ended
    await s.close()


async def test_knowledge_and_saving_from_an_llm_step(engine):
    eng = engine(agent_with_lookups())
    eng.library.create_collection("appointments")
    eng.library.create_collection("callbacks")
    eng.library.create_base("clinic-faq")
    await eng.library.add_document("clinic-faq", "faq.md", FAQ.encode())
    s = eng.new_text(eng.load_agent("clinic"), {})
    await s.open()
    await s.send("ZZ-0000")                                         # not found: otherwise → Help desk
    assert s.flow.current.name == "Help desk"
    events = await s.send("is there parking at the clinic")
    assert any("Elm Street" in t for t in said(events))             # the fake model reads back the tool's answer
    events = await s.send("please log a callback request about my bill")
    assert [r["data"]["reason"] for r in eng.library.records("callbacks")["records"]]
    await s.close()


async def test_the_api(cfg, tmp_path):
    eng = Engine(tmp_path / "agents", cfg, data_dir=tmp_path / "data")
    eng.library.embedder = Embedder("off")
    app = create_app(eng)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)), base_url="http://localhost") as c:
        assert (await c.post("/v1/data", json={"name": "members", "key": "member_id"})).status_code == 200
        r = await c.post("/v1/data/members/import", params={"filename": "m.csv"}, content=b"member_id,phone\n1001,312-555-0101\n")
        assert r.json()["imported"] == 1 and r.json()["warnings"] == []      # 555-01xx is an obvious fake
        r = await c.post("/v1/data/members/records", json={"data": {"member_id": "1002", "ssn": "219-09-9999"}})
        assert r.json()["warnings"]                                  # looks like a real SSN: flagged, not refused
        assert (await c.get("/v1/data/members")).json()["total"] == 2
        assert (await c.post("/v1/data/members/lookup", json={"field": "member_id", "value": "1001"})).json()["records"]
        assert (await c.get("/v1/data/nope")).status_code == 404
        assert (await c.post("/v1/knowledge", json={"name": "faq"})).status_code == 200
        r = await c.post("/v1/knowledge/faq/documents", params={"filename": "faq.md"}, content=FAQ.encode())
        assert r.json()["document"]["passages"] == 3
        r = await c.post("/v1/knowledge/faq/search", json={"query": "medicare"})
        assert r.json()["results"][0]["text"].startswith("Insurance")
        assert (await c.post("/v1/knowledge/faq/documents", params={"filename": "x.exe"}, content=b"MZ")).status_code == 422
        st = (await c.get("/v1/knowledge")).json()
        assert st["bases"][0]["passages"] == 3 and st["embeddings"]["mode"] == cfg.embeddings
    remote = httpx.ASGITransport(app=app, client=("203.0.113.9", 5000))
    async with httpx.AsyncClient(transport=remote, base_url="http://localhost") as c:
        assert (await c.get("/v1/data")).status_code == 403          # only from this computer
        assert (await c.get("/v1/knowledge")).status_code == 403
    await eng.shutdown()
