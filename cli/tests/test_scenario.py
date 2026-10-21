import json
import re

import pytest
import yaml

from conftest import load_fixture, make_folder
from vk import agentfs, scenario
from vk.lint import lint


RESCHEDULE = {
    "name": "caller reschedules",
    "turns": [
        {"user": "I want to reschedule my visit", "expect": {"reaches": "Reschedule"}},
        {"user": "Friday is fine, so it's rescheduled, thanks"},
    ],
    "expect": {
        "reaches": ["Router", "Reschedule", "Goodbye"],
        "not_reaches": "Billing",
        "calls_tool": "rescheduled",
        "says": ["hi sam", "friday"],
        "not_says": "account number",
        "ends": True,
    },
}


def test_passing_scenario(run, engine, tmp_path):
    folder = make_folder(tmp_path, {"reschedule": RESCHEDULE})
    code, out, err = run("test", str(folder))
    assert code == 0, out + err
    assert "PASS  caller reschedules" in out and "Greeting → Router → Reschedule → Goodbye" in out
    assert "1 passed, 0 failed" in out


def test_failures_are_explained_with_transcript(run, engine, tmp_path):
    bad = {"name": "wrong expectations", "turns": [{"user": "billing question please"}],
           "expect": {"reaches": ["Billing", "Router"], "says": "goodbye", "calls_tool": "find_slots",
                      "not_reaches": "Billing", "ends": True}}
    folder = make_folder(tmp_path, {"bad": bad})
    code, out, _ = run("test", str(folder))
    assert code == 1
    assert "expected to reach 'Router'" in out            # order matters: Router came before Billing
    assert "should not reach 'Billing'" in out
    assert "expected the agent to say 'goodbye'" in out
    assert "expected a call to tool 'find_slots'; tools called: greeted, billing" in out
    assert "expected the conversation to end; it did not end" in out
    assert "caller: billing question please" in out        # transcript printed on failure
    assert "0 passed, 1 failed" in out


def test_per_turn_expectations(run, engine, tmp_path):
    doc = {"name": "per turn", "turns": [{"user": "hello", "expect": {"reaches": "Billing"}}]}
    folder = make_folder(tmp_path, {"t": doc})
    code, out, _ = run("test", str(folder))
    assert code == 1 and "turn 1: expected to reach 'Billing'" in out


def test_turns_after_the_call_ended(run, engine, tmp_path):
    doc = {"name": "too long", "turns": [{"user": "reschedule"}, {"user": "rescheduled"}, {"user": "one more thing"}]}
    folder = make_folder(tmp_path, {"t": doc})
    code, out, _ = run("test", str(folder))
    assert code == 1 and "turn 3: the conversation had already ended" in out


def test_engine_errors_fail_the_test(run, engine, tmp_path):
    folder = make_folder(tmp_path, {"t": {"name": "err", "turns": [{"user": "boom"}]}})
    code, out, _ = run("test", str(folder))
    assert code == 1 and "engine error: LLM provider returned 400" in out


def test_runs_the_folder_as_it_is_on_disk(run, engine, tmp_path):
    folder = make_folder(tmp_path, {"r": RESCHEDULE})
    router = folder / "prompts/router.md"
    router.write_text(router.read_text() + "Be brief.\n")
    code, out, _ = run("test", str(folder))
    assert code == 0, out
    assert "Be brief." in json.dumps(engine.started[0]["definition"])
    assert engine.started[0]["mock_tools"] is True


def test_needs_the_engine_key(run, engine, tmp_path, monkeypatch):
    monkeypatch.setenv("VK_ENGINE_KEY", "wrong")
    code, _, err = run("test", str(make_folder(tmp_path, {"r": RESCHEDULE})))
    assert code == 1 and "API key" in err


def test_unfinished_sessions_are_ended(run, engine, tmp_path):
    folder = make_folder(tmp_path, {"t": {"name": "open", "turns": [{"user": "hello"}]}})
    run("test", str(folder))
    assert engine.chats and all(s["ended_by_client"] for s in engine.chats.values())


def test_context_overrides_and_defaults(run, engine, tmp_path):
    doc = {"name": "ctx", "context": {"first_name": "Riley"}, "turns": [], "expect": {"says": "hi riley"}}
    folder = make_folder(tmp_path, {"t": doc})
    assert run("test", str(folder))[0] == 0


def test_filter_and_json_output(run, engine, tmp_path):
    other = {"name": "billing path", "turns": [{"user": "billing"}], "expect": {"reaches": "Billing"}}
    folder = make_folder(tmp_path, {"r": RESCHEDULE, "b": other})
    code, out, _ = run("test", str(folder), "-k", "billing", "--format", "json")
    results = json.loads(out)
    assert code == 0 and [r["name"] for r in results] == ["billing path"]
    assert results[0]["steps"] == ["Greeting", "Router", "Billing"]
    assert {"who": "caller", "text": "billing"} in results[0]["transcript"]


def test_no_tests_found(run, engine, tmp_path):
    folder = make_folder(tmp_path, {})
    code, _, err = run("test", str(folder))
    assert code == 1 and "no tests found" in err


@pytest.mark.parametrize("doc,message", [
    ({"turns": [{"user": "hi"}], "expect": {"reach": "Router"}}, "unknown expect key(s) reach"),
    ({"turns": [{"say": "hi"}]}, "turns must be a list"),
    ({"turns": [], "expect": {"ends": "yes"}}, "expect.ends must be true or false"),
    ("just a string", "expected a mapping"),
])
def test_bad_test_files(tmp_path, doc, message):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/x.yaml").write_text(yaml.safe_dump(doc))
    with pytest.raises(scenario.TestFileError, match=re.escape(message)):
        scenario.load(tmp_path)


def test_lint_flags_tests_naming_missing_steps(tmp_path):
    a = agentfs.Agent(name="Front Desk", definition=load_fixture(), context_variables={"first_name": "Sam"})
    agentfs.write(a, tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/x.yaml").write_text(yaml.safe_dump(
        {"turns": [{"user": "hi", "expect": {"reaches": "Payments"}}],
         "expect": {"reaches": ["router", "5"], "not_reaches": "Refunds"}}))
    found = [f for f in lint(agentfs.load(tmp_path), tmp_path) if f.code == "VK110"]
    assert sorted(f.message.split('"')[1] for f in found) == ["Payments", "Refunds"]
    assert all(f.severity == "error" and f.where == "tests/x.yaml" for f in found)


def test_lint_reports_broken_test_file(tmp_path):
    agentfs.write(agentfs.Agent(name="x", definition=load_fixture(), context_variables={"first_name": "S"}), tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/x.yaml").write_text("turns: [user: hi")
    assert any(f.code == "VK110" and "not valid YAML" in f.message for f in lint(agentfs.load(tmp_path), tmp_path))


MODEL_CALLER = {
    "name": "model caller reschedules",
    "caller": {"persona": "Sam, in a hurry, needs a new appointment day", "goal": "reschedule to Friday",
               "opening": "Hi there, quick question."},
    "expect": {"reaches": ["Router", "Reschedule", "Goodbye"], "ends": True},
}


def test_a_model_plays_the_caller(run, engine, tmp_path):
    engine.caller_script = ["I want to reschedule my visit", "Friday is fine, so it's rescheduled, thanks"]
    folder = make_folder(tmp_path, {"model": MODEL_CALLER})
    code, out, err = run("test", str(folder), "-v")
    assert code == 0, out + err
    assert "PASS  model caller reschedules  (3 turn(s), model caller" in out
    assert "caller: Hi there, quick question." in out                      # the opening line goes first, unasked
    first = engine.caller_asks[0]
    assert first["persona"].startswith("Sam") and first["goal"] == "reschedule to Friday"
    assert [x["who"] for x in first["transcript"]][:2] == ["agent", "caller"]   # it sees the conversation so far
    assert len(engine.caller_asks) == 2                                      # the call ended: no more asking


def test_a_model_caller_that_gives_up_or_runs_long(run, engine, tmp_path):
    engine.caller_script = []                                                # done straight after the opening
    folder = make_folder(tmp_path, {"model": {**MODEL_CALLER, "caller": {**MODEL_CALLER["caller"], "max_turns": 2}}})
    code, out, _ = run("test", str(folder))
    assert code == 1 and "expected to reach 'Reschedule'" in out
    engine.caller_script = ["hello", "hello again", "and again"]
    folder = make_folder(tmp_path / "b", {"model": {"name": "long", "caller": {"persona": "Sam, chatty", "max_turns": 2}}})
    code, out, _ = run("test", str(folder), "-v")
    assert code == 0 and "stopped after 2 caller turns" in out and engine.caller_script == ["and again"]


@pytest.mark.parametrize("caller, message", [
    ({"persona": "Sam"}, "persona"),
    ({"persona": "Sam, busy", "max_turns": 99}, "max_turns"),
    ({"persona": "Sam, busy", "mood": "x"}, "unknown caller key"),
    ("just a string", "mapping"),
])
def test_bad_caller_blocks(tmp_path, caller, message):
    folder = make_folder(tmp_path, {"x": {"name": "x", "caller": caller}})
    with pytest.raises(scenario.TestFileError, match=message):
        scenario.load(folder)
    folder = make_folder(tmp_path / "both", {"x": {"name": "x", "caller": {"persona": "Sam, busy"}, "turns": [{"user": "hi"}]}})
    with pytest.raises(scenario.TestFileError, match="not both"):
        scenario.load(folder)
