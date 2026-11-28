"""The vk GitHub Action's lint-and-test script (.github/actions/vk/run.sh), run as CI runs it:
real `vk` commands against the fake engine served over HTTP."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import API_KEY, make_folder
from fake_engine_http import serve_in_background

ACTION = Path(__file__).resolve().parents[2] / ".github" / "actions" / "vk"
RUN = ACTION / "run.sh"
VK = shutil.which("vk")
if VK is None and os.environ.get("CI"):        # in CI these must run, not skip quietly
    raise RuntimeError("test_action.py needs the vk command on PATH (pip install -e cli)")
pytestmark = pytest.mark.skipif(VK is None or shutil.which("bash") is None, reason="needs bash and the vk command")


@pytest.fixture
def served():
    """The fake engine on a real port, as the action's script needs one."""
    httpd, url = serve_in_background()
    yield url
    httpd.shutdown()


@pytest.fixture
def repo(tmp_path):
    make_folder(tmp_path, {"billing": {"name": "billing question", "turns": [{"user": "hi"}, {"user": "billing"}],
                                       "expect": {"reaches": ["Billing"]}}}, name="desk")
    make_folder(tmp_path, {"wrong": {"name": "wants reschedule", "turns": [{"user": "hi"}, {"user": "billing"}],
                                     "expect": {"reaches": ["Reschedule"]}}}, name="broken")
    make_folder(tmp_path, None, name="no-tests")
    return tmp_path


def action(repo: Path, script: Path = RUN, extra_env: dict | None = None, **inputs: str):
    summary = repo / "summary.md"
    summary.unlink(missing_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("INPUT_", "VK_ENGINE", "GITHUB_", "LAMPLINE_"))}
    env.update({f"INPUT_{k.upper()}": v for k, v in inputs.items()}, GITHUB_STEP_SUMMARY=str(summary), VK=VK,
               **(extra_env or {}))
    p = subprocess.run(["bash", str(script)], cwd=repo, env=env, capture_output=True, text=True, timeout=120)
    return p.returncode, p.stdout + p.stderr, summary.read_text() if summary.exists() else ""


def fork_event(repo: Path, head: str = "someone/Lampline") -> dict:
    event = repo / "event.json"
    event.write_text(json.dumps({"pull_request": {"head": {"repo": {"full_name": head}}}}))
    return {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(event), "GITHUB_REPOSITORY": "C4rohan/Lampline"}


def test_lint_only_finds_every_agent_folder(repo):
    code, out, summary = action(repo)
    assert code == 0, out
    assert "Agent folders:\n  agents/broken\n  agents/desk\n  agents/no-tests\n" in out
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
    assert sorted(p.name for p in (repo / "reports").iterdir()) == ["agents_broken.xml", "agents_desk.xml"]
    assert "FAILURE wants reschedule: expected to reach 'Reschedule'" in out
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


# --- found in review: each of these used to pass quietly ---------------------------------

def test_a_folder_with_a_space_is_checked_not_skipped(repo):
    make_folder(repo, None, name="my agent")
    prompt = next((repo / "agents" / "my agent" / "prompts").glob("*.md"))
    prompt.write_text(prompt.read_text() + "\nCall Jane at 410-555-2368.\n")
    code, out, summary = action(repo)                      # the default agents/* glob
    assert code == 1
    assert "  agents/my agent\n" in out and "| `agents/my agent` | ❌ problems |" in summary
    code, out, summary = action(repo, agents="agents/desk\nagents/my agent")   # one entry per line
    assert code == 1 and "| `agents/my agent` | ❌ problems |" in summary and "agents/desk" in summary


def test_true_or_false_inputs_reject_anything_else(repo):
    code, out, _ = action(repo, lint="yes")
    assert code == 2 and "input lint is true or false; got 'yes'" in out
    code, out, summary = action(repo, lint="True")         # any case of true/false is fine
    assert code == 0 and summary.count("✅ clean") == 3


def test_tests_on_with_no_tests_anywhere_is_an_error(repo):
    code, out, _ = action(repo, agents="agents/no-tests", test="true")
    assert code == 2 and "none of the agent folders has tests/*.yaml" in out


def test_same_named_folders_get_their_own_junit_report(repo, served):
    make_folder(repo / "other", {"t": {"name": "billing question", "turns": [{"user": "hi"}, {"user": "billing"}],
                                       "expect": {"reaches": ["Billing"]}}}, name="desk")
    code, out, _ = action(repo, agents="agents/desk other/agents/desk", test="true", junit="reports",
                          engine_url=served, engine_key=API_KEY)
    assert code == 0, out
    assert sorted(p.name for p in (repo / "reports").iterdir()) == ["agents_desk.xml", "other_agents_desk.xml"]


def test_an_unreachable_engine_leaves_no_empty_report(repo):
    code, out, summary = action(repo, agents="agents/desk", test="true", junit="reports",
                                engine_url="http://127.0.0.1:9", engine_key=API_KEY)
    assert code == 1
    assert not list((repo / "reports").glob("*.xml"))
    assert "no report: the tests didn't run" in summary


def test_model_errors_are_reported_as_engine_errors(repo, served):
    (repo / "agents" / "desk" / "tests" / "boom.yaml").write_text("name: model breaks\nturns:\n  - user: boom\n")
    code, out, summary = action(repo, agents="agents/desk", test="true", junit="reports",
                                engine_url=served, engine_key=API_KEY)
    assert code == 1
    assert "ERROR   model breaks: engine error: LLM provider returned 400" in out
    assert "1 of 2 passed, 1 engine error(s)" in summary


def test_skipped_tests_say_why_and_lint_still_runs(repo):
    code, out, summary = action(repo, extra_env={"LAMPLINE_ACTION_SKIP_TESTS": "a pull request from a fork has no ANTHROPIC_API_KEY"},
                                agents="agents/desk", test="true")
    assert code == 0
    assert "::warning::not running tests: a pull request from a fork has no ANTHROPIC_API_KEY" in out
    assert "| `agents/desk` | ✅ clean | ⚠️ skipped (a pull request from a fork has no ANTHROPIC_API_KEY) |" in summary


def test_a_fork_pull_request_without_a_google_token_skips_google_tests(repo):
    code, out, summary = action(repo, extra_env=fork_event(repo), agents="agents/desk", test="true",
                                target="dfcx", google_agent="projects/p/locations/global/agents/a")
    assert code == 0 and "has no Google token" in summary


# --- the engine step's own checks (no engine is started in these) --------------------------

@pytest.mark.parametrize("inputs, code, message", [
    ({"start_engine": "false"}, 0, ""),
    ({"start_engine": "yes", "model": "anthropic:x"}, 2, "input start-engine is true or false"),
    ({"start_engine": "true"}, 2, "start-engine needs a model"),
    ({"start_engine": "true", "model": "qwen3"}, 2, "model is provider:model"),
    ({"start_engine": "true", "model": "mistral:large"}, 2, "model provider is ollama, anthropic, openai or google"),
    ({"start_engine": "true", "model": "anthropic:claude-haiku-5-5"}, 2, "needs ANTHROPIC_API_KEY in env"),
])
def test_engine_step_checks_its_inputs(repo, inputs, code, message):
    env = {"ANTHROPIC_API_KEY": "", "PY": shutil.which("python3") or "python3"}
    got, out, _ = action(repo, script=ACTION / "engine.sh", extra_env=env, **inputs)
    assert got == code and message in out


def test_engine_step_skips_tests_on_a_fork_pull_request_without_the_key(repo):
    genv = repo / "github_env"
    env = {**fork_event(repo), "GITHUB_ENV": str(genv), "ANTHROPIC_API_KEY": "", "PY": shutil.which("python3") or "python3"}
    code, out, _ = action(repo, script=ACTION / "engine.sh", extra_env=env,
                          start_engine="true", model="anthropic:claude-haiku-5-5")
    assert code == 0 and "::warning::" in out
    assert genv.read_text().startswith("LAMPLINE_ACTION_SKIP_TESTS=a pull request from a fork has no ANTHROPIC_API_KEY")


def test_a_fork_pull_request_testing_on_an_engine_url_without_its_key_skips(repo):
    code, out, summary = action(repo, extra_env=fork_event(repo), agents="agents/desk", test="true",
                                engine_url="http://127.0.0.1:9")
    assert code == 0 and "has no engine-key" in summary


def test_the_engine_steps_skip_is_cleared_for_later_steps(repo):
    genv = repo / "github_env"
    code, _, _ = action(repo, extra_env={"LAMPLINE_ACTION_SKIP_TESTS": "a pull request from a fork has no X",
                                         "GITHUB_ENV": str(genv)}, agents="agents/desk", test="true")
    assert code == 0 and genv.read_text() == "LAMPLINE_ACTION_SKIP_TESTS=\n"


def test_a_malformed_event_file_is_not_a_fork_and_prints_no_traceback(repo):
    bad = repo / "event.json"
    bad.write_text("not json")
    env = {"GITHUB_EVENT_NAME": "pull_request", "GITHUB_EVENT_PATH": str(bad), "GITHUB_REPOSITORY": "C4rohan/Lampline"}
    code, out, _ = action(repo, extra_env=env, agents="agents/desk", test="true", target="dfcx",
                          google_agent="projects/p/locations/global/agents/a")
    assert "Traceback" not in out and "has no Google token" not in out
