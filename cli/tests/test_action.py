"""The vk GitHub Action's lint-and-test script (.github/actions/vk/run.sh), run as CI runs it:
real `vk` commands against the fake engine served over HTTP."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from conftest import API_KEY, FakeEngine, make_folder

RUN = Path(__file__).resolve().parents[2] / ".github" / "actions" / "vk" / "run.sh"
VK = shutil.which("vk")
pytestmark = pytest.mark.skipif(VK is None or shutil.which("bash") is None, reason="needs bash and the vk command")


@pytest.fixture
def served():
    """The fake engine on a real port, as the action's script needs one."""
    fake = FakeEngine()

    class Handler(BaseHTTPRequestHandler):
        def _go(self):
            n = int(self.headers.get("Content-Length") or 0)
            request = httpx.Request(self.command, "http://engine" + self.path, headers=dict(self.headers),
                                    content=self.rfile.read(n) if n else b"")
            reply = fake.handler(request)
            self.send_response(reply.status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(reply.content)

        do_GET = do_POST = _go

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
def repo(tmp_path):
    make_folder(tmp_path, {"billing": {"name": "billing question", "turns": [{"user": "hi"}, {"user": "billing"}],
                                       "expect": {"reaches": ["Billing"]}}}, name="desk")
    make_folder(tmp_path, {"wrong": {"name": "wants reschedule", "turns": [{"user": "hi"}, {"user": "billing"}],
                                     "expect": {"reaches": ["Reschedule"]}}}, name="broken")
    make_folder(tmp_path, None, name="no-tests")
    return tmp_path


def action(repo: Path, **inputs: str):
    summary = repo / "summary.md"
    env = {k: v for k, v in os.environ.items() if not k.startswith(("INPUT_", "VK_ENGINE"))}
    env.update({f"INPUT_{k.upper()}": v for k, v in inputs.items()}, GITHUB_STEP_SUMMARY=str(summary), VK=VK)
    p = subprocess.run(["bash", str(RUN)], cwd=repo, env=env, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr, summary.read_text() if summary.exists() else ""


def test_lint_only_finds_every_agent_folder(repo):
    code, out, summary = action(repo)
    assert code == 0, out
    assert "Agent folders: agents/broken agents/desk agents/no-tests" in out
    assert summary.count("✅ clean") == 3


def test_a_folder_of_folders_works_too(repo):
    code, out, _ = action(repo, agents="agents")
    assert code == 0 and "agents/desk" in out


def test_real_looking_personal_data_fails_strict_and_is_annotated(repo):
    prompt = next((repo / "agents" / "desk" / "prompts").glob("*.md"))
    prompt.write_text(prompt.read_text() + "\nCall Jane at 410-555-2368.\n")
    code, out, summary = action(repo, agents="agents/desk")
    assert code == 1
    assert "::error file=agents/desk/agent.yaml,title=VK204::" in out
    assert "❌ problems" in summary
    code, out, _ = action(repo, agents="agents/desk", strict="false")
    assert code == 0 and "::warning file=agents/desk/agent.yaml,title=VK204::" in out


def test_tests_run_per_folder_and_one_failure_fails_the_step(repo, served):
    code, out, summary = action(repo, test="true", engine_url=served, engine_key=API_KEY)
    assert code == 1
    assert "| `agents/desk` | ✅ clean | ✅ 1 passed, 0 failed |" in summary
    assert "| `agents/broken` | ✅ clean | ❌ 0 passed, 1 failed |" in summary
    assert "| `agents/no-tests` | ✅ clean | no tests |" in summary


def test_junit_reports_one_file_per_agent_and_prints_why(repo, served):
    code, out, summary = action(repo, agents="agents/broken agents/desk", test="true", junit="reports",
                                engine_url=served, engine_key=API_KEY)
    assert code == 1
    assert sorted(p.name for p in (repo / "reports").iterdir()) == ["broken.xml", "desk.xml"]
    assert "FAIL  wants reschedule: expected to reach 'Reschedule'" in out
    assert "✅ 1 of 1 passed" in summary


def test_coverage_and_extra_args(repo, served):
    code, out, _ = action(repo, agents="agents/desk", test="true", coverage="true", args="-k billing",
                          engine_url=served, engine_key=API_KEY)
    assert code == 0, out
    assert "coverage:" in out and "never reached" in out


@pytest.mark.parametrize("inputs, message", [
    ({"agents": "nope/*"}, "no agent folders"),
    ({"test": "true", "target": "dfcx", "google_agent": "projects/p/locations/l/agents/a"}, "one agent folder"),
    ({"test": "true", "coverage": "true", "junit": "r"}, "coverage and junit"),
])
def test_bad_inputs_stop_with_a_clear_error(repo, inputs, message):
    code, out, _ = action(repo, **inputs)
    assert code == 2 and "::error::" in out and message in out
