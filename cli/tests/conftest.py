from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
import pytest

FIXTURES = Path(__file__).parent / "fixtures"
API_KEY = "test-key"
ENGINE = "http://engine.test"


def load_fixture(name: str = "front_desk.json") -> dict:
    return json.loads((FIXTURES / name).read_text())


class FakeEngine:
    """In-memory stand-in for the engine's text chat (/v1/chat/...): a keyword "model" that follows
    an edge when its label appears in the caller's message."""

    def __init__(self):
        self.chats: dict[str, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.started: list[dict] = []
        self.caller_script: list[str] = []      # what the "model" caller says, in order; then it's done
        self.caller_asks: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append((request.method, request.url.path))
        if request.headers.get("X-API-Key") != API_KEY:
            return httpx.Response(401, json={"detail": "missing or wrong API key"})
        path = request.url.path.removeprefix("/v1")
        parts = path.strip("/").split("/")
        body = json.loads(request.content) if request.content else {}
        if request.method == "GET" and path == "/health":
            return httpx.Response(200, json={"ok": True, "config": {"llm": "fake", "model": "keywords"}})
        if request.method == "POST" and path == "/tests/caller":
            self.caller_asks.append(body)
            line = self.caller_script.pop(0) if self.caller_script else None
            return httpx.Response(200, json={"line": line, "done": line is None})
        if request.method == "POST" and path == "/chat/sessions":
            return httpx.Response(200, json=self._start(body))
        if parts[:2] == ["chat", "sessions"] and len(parts) == 4:
            sess = self.chats.get(parts[2])
            if sess is None:
                return httpx.Response(404, json={"detail": "no such session"})
            if parts[3] == "messages":
                if sess["ended"]:
                    return httpx.Response(409, json={"detail": "the conversation has ended"})
                return httpx.Response(200, json=self._reply(sess, body["text"]))
            if parts[3] == "end":
                sess["ended"] = sess["ended_by_client"] = True
                return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": f"no route {request.method} {path}"})

    def _start(self, body):
        self.started.append(body)
        sid = f"s{len(self.chats) + 1}"
        definition = body["definition"]
        start = next(n for n in definition["nodes"] if n["type"] == "startCall")
        ctx = {**(body.get("context_variables") or {}), **(body.get("context") or {})}
        greeting = start["data"].get("greeting", "")
        for k, v in ctx.items():
            greeting = greeting.replace("{{" + k + "}}", str(v))
        sess = {"id": sid, "definition": definition, "node": start["id"], "ended": False, "ended_by_client": False}
        self.chats[sid] = sess
        return self._out(sess, [self._ev("node_transition", node_id=start["id"], node_name=start["data"]["name"]),
                                self._ev("bot_speech", text=greeting)])

    @staticmethod
    def _ev(kind, **payload):
        return {"type": kind, **payload}

    @staticmethod
    def _out(sess, events):
        return {"session_id": sess["id"], "events": events, "ended": sess["ended"], "current_step": sess["node"]}

    def _reply(self, sess, text):
        definition = sess["definition"]
        nodes = {n["id"]: n for n in definition["nodes"]}
        events, said = [], text.lower()
        if "boom" in said:
            events.append(self._ev("execution_error", message="LLM provider returned 400"))
        for _ in range(4):
            outs = [e for e in definition["edges"] if e["source"] == sess["node"]]
            node = nodes[sess["node"]]
            pick = outs[0] if node["type"] == "startCall" and len(outs) == 1 else next(
                (e for e in outs if e["data"]["label"].lower() in said), None)
            if pick is None:
                break
            tool = re.sub(r"[^a-z0-9]", "_", pick["data"]["label"].lower())
            events.append(self._ev("tool_call_started", function_name=tool, tool_call_id="t", arguments={}))
            sess["node"] = pick["target"]
            target = nodes[pick["target"]]
            events.append(self._ev("node_transition", node_id=target["id"], node_name=target["data"]["name"]))
            if target["type"] == "endCall":
                sess["ended"] = True
                break
        if "lookup" in said:
            events.append(self._ev("tool_call_started", function_name="find_slots", tool_call_id="u", arguments={}))
        events.append(self._ev("bot_speech", text=f"[{nodes[sess['node']]['data']['name']}] noted: {text}"))
        return self._out(sess, events)


def make_folder(tmp_path: Path, tests: dict[str, dict] | None = None, context: dict | None = None,
                name: str = "front-desk") -> Path:
    """An agent folder from the front-desk fixture, with tests/*.yaml."""
    import yaml
    from vk import agentfs

    folder = tmp_path / "agents" / name
    agentfs.write(agentfs.Agent(name="Front Desk", definition=load_fixture(),
                                context_variables={"first_name": "Sam"} if context is None else context), folder)
    if tests is not None:
        (folder / "tests").mkdir(exist_ok=True)
        for test_name, doc in tests.items():
            (folder / "tests" / f"{test_name}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return folder


@pytest.fixture
def engine():
    return FakeEngine()


@pytest.fixture
def run(engine, monkeypatch, tmp_path, capsys):
    """Run the CLI against the fake engine; returns (exit code, stdout, stderr)."""
    from vk import cli

    monkeypatch.setenv("VK_ENGINE_URL", ENGINE)
    monkeypatch.setenv("VK_ENGINE_KEY", API_KEY)
    monkeypatch.chdir(tmp_path)
    transport = httpx.MockTransport(engine.handler)

    def _run(*argv: str):
        code = cli.main(list(argv), transport=transport)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run
