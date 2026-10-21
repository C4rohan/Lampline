"""vk test --target dfcx|ces: the same test files against a live Google agent (here: a fake Google API)."""

import json

import httpx
import pytest

from conftest import API_KEY, ENGINE, make_folder
from vk import google_targets as gt

DFCX = "projects/demo-project/locations/us-central1/agents/demo-agent"
CES = "projects/demo-project/locations/us/apps/demo-app"

SCRIPTED = {
    "name": "caller reschedules",
    "context": {"first_name": "Sam"},
    "turns": [{"user": "I want to reschedule my visit", "expect": {"reaches": "Reschedule"}},
              {"user": "Friday is fine, thanks, bye"}],
    "expect": {"reaches": ["Reschedule"], "calls_tool": "find_slots", "says": "friday", "ends": True},
}


class FakeGoogle:
    """Just enough of detectIntent and runSession: keyword routing, a tool call, and an ending."""

    def __init__(self):
        self.seen: list[tuple[str, dict, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.seen.append((str(request.url), body, dict(request.headers)))
        if request.headers.get("authorization") != "Bearer test-token":
            return httpx.Response(401, json={"error": {"message": "Request had invalid authentication credentials."}})
        if ":detectIntent" in str(request.url):
            text = body["queryInput"]["text"]["text"].lower()
            if "reschedule" in text:
                return httpx.Response(200, json={"queryResult": {
                    "currentPage": {"name": "x/pages/p1", "displayName": "Reschedule"},
                    "traceBlocks": [{"playbookTraceMetadata": {"playbook": "x/playbooks/b1", "displayName": "Scheduler"},
                                     "actions": [{"toolUse": {"tool": "x/tools/t1", "displayName": "find_slots"}}]}],
                    "responseMessages": [{"text": {"text": ["Which day works?"]}}]}})
            return httpx.Response(200, json={"queryResult": {
                "currentPage": {"name": "x/pages/END_SESSION", "displayName": "End Session"},
                "responseMessages": [{"text": {"text": ["Friday it is. Goodbye!"]}}, {"endInteraction": {}}]}})
        if ":runSession" in str(request.url):
            text = next(i["text"] for i in body["inputs"] if "text" in i).lower()
            if "reschedule" in text:
                return httpx.Response(200, json={"outputs": [
                    {"diagnosticInfo": {"messages": [{"role": "agent", "chunks": [
                        {"agentTransfer": {"targetAgent": "x/agents/a2", "displayName": "Reschedule"}}]}]}},
                    {"toolCalls": {"toolCalls": [{"tool": "x/tools/t1", "displayName": "find_slots", "args": {}}]}},
                    {"text": "Which day works?", "turnCompleted": True}]})
            return httpx.Response(200, json={"outputs": [{"text": "Friday it is. Goodbye!"}, {"endSession": {}}]})
        return httpx.Response(404, json={"error": {"message": "no such method"}})


@pytest.fixture
def google(engine, monkeypatch, tmp_path, capsys):
    from vk import cli

    fake = FakeGoogle()
    monkeypatch.setenv("VK_ENGINE_URL", ENGINE)
    monkeypatch.setenv("VK_ENGINE_KEY", API_KEY)
    monkeypatch.setenv("VK_GOOGLE_TOKEN", "test-token")
    monkeypatch.chdir(tmp_path)

    def route(request):
        return fake(request) if request.url.host.endswith("googleapis.com") else engine.handler(request)

    def _run(*argv):
        code = cli.main(list(argv), transport=httpx.MockTransport(route))
        out = capsys.readouterr()
        return code, out.out, out.err
    fake.run = _run
    return fake


def test_a_dialogflow_cx_agent(google, tmp_path):
    folder = make_folder(tmp_path, {"r": SCRIPTED})
    code, out, err = google.run("test", str(folder), "--target", "dfcx", "--agent", DFCX, "-v")
    assert code == 0, out + err
    assert "== Dialogflow CX " + DFCX in out and "PASS  caller reschedules" in out and "Scheduler → Reschedule" in out
    url, body, headers = google.seen[0]
    assert url.startswith("https://us-central1-dialogflow.googleapis.com/v3/" + DFCX + "/sessions/vk-")
    assert url.endswith(":detectIntent") and headers["x-goog-user-project"] == "demo-project"
    assert body["queryInput"] == {"text": {"text": "I want to reschedule my visit"}, "languageCode": "en"}
    assert body["queryParams"]["parameters"]["first_name"] == "Sam"
    assert "queryParams" not in google.seen[1][1]                            # context only on the first turn


def test_a_cx_agent_studio_app(google, tmp_path):
    folder = make_folder(tmp_path, {"r": SCRIPTED})
    code, out, err = google.run("test", str(folder), "--target", "ces", "--agent", CES, "--deployment", "prod-1")
    assert code == 0, out + err
    url, body, _ = google.seen[0]
    assert url.startswith("https://ces.googleapis.com/v1/" + CES + "/sessions/vk-") and url.endswith(":runSession")
    assert body["config"]["deployment"] == CES + "/deployments/prod-1"
    assert body["inputs"][0] == {"variables": {"first_name": "Sam"}} and body["inputs"][1] == {"text": "I want to reschedule my visit"}


def test_side_by_side_with_the_engine(google, tmp_path):
    folder = make_folder(tmp_path, {"r": SCRIPTED})
    code, out, _ = google.run("test", str(folder), "--target", "engine,dfcx", "--agent", DFCX)
    assert "== engine " in out and "== Dialogflow CX" in out and "Side by side" in out
    assert "caller reschedules" in out.split("Side by side")[1]


def test_a_model_caller_on_google_uses_the_engine_model(google, engine, tmp_path):
    engine.caller_script = ["I want to reschedule my visit", "Friday is fine, bye"]
    doc = {"name": "model", "caller": {"persona": "Sam, busy, wants a new day"}, "expect": {"reaches": "Reschedule", "ends": True}}
    folder = make_folder(tmp_path, {"m": doc})
    code, out, err = google.run("test", str(folder), "--target", "dfcx", "--agent", DFCX)
    assert code == 0, out + err and engine.caller_asks


def test_mistakes_are_explained(google, tmp_path, monkeypatch):
    folder = make_folder(tmp_path, {"r": SCRIPTED})
    assert "--agent names the Google agent" in google.run("test", str(folder), "--target", "dfcx")[2]
    assert "projects/PROJECT/locations/LOCATION/agents" in google.run("test", str(folder), "--target", "dfcx", "--agent", "agents/x")[2]
    assert "--target is engine, dfcx or ces" in google.run("test", str(folder), "--target", "lex")[2]
    monkeypatch.setenv("VK_GOOGLE_TOKEN", "wrong")
    code, out, _ = google.run("test", str(folder), "--target", "dfcx", "--agent", DFCX)
    assert code == 1 and "invalid authentication" in out


def test_the_token_comes_from_gcloud_when_not_set(monkeypatch):
    monkeypatch.delenv("VK_GOOGLE_TOKEN", raising=False)
    monkeypatch.setattr(gt.shutil, "which", lambda _: None)
    with pytest.raises(gt.ApiError, match="gcloud auth login"):
        gt.access_token()
