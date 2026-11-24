import json
from pathlib import Path
from xml.etree import ElementTree

import pytest
import yaml

from conftest import make_folder


def test_lint_clean_folder(run, tmp_path):
    folder = make_folder(tmp_path)
    code, out, _ = run("lint", str(folder))
    assert code == 0 and "no problems found" in out


def test_lint_blocks_personal_data(run, tmp_path):
    folder = make_folder(tmp_path)
    billing = folder / "prompts/billing.md"
    billing.write_text(billing.read_text() + "Test caller SSN 219-09-9999.\n")
    code, out, _ = run("lint", str(folder))
    assert code == 1 and "VK201" in out


def test_lint_json_output_and_strict(run, tmp_path):
    folder = str(make_folder(tmp_path, context={}))      # {{first_name}} now unprovided -> warning
    code, out, _ = run("lint", folder, "--format", "json")
    assert code == 0 and json.loads(out)[0]["code"] == "VK107"
    assert run("lint", folder, "--strict")[0] == 1


def test_lint_several_folders(run, tmp_path):
    a, b = make_folder(tmp_path, name="a"), make_folder(tmp_path, name="b")
    code, out, _ = run("lint", str(a), str(b))
    assert code == 0 and f"== {a}" in out and f"== {b}" in out


def test_lint_a_folder_of_agents(run, tmp_path):
    """`vk lint agents` lints every agent folder directly inside agents/."""
    make_folder(tmp_path, name="a")
    make_folder(tmp_path, name="b")
    code, out, _ = run("lint", "agents")
    assert code == 0 and out.count("no problems found") == 2


def test_lint_folder_of_agents_still_fails(run, tmp_path):
    folder = make_folder(tmp_path, name="a")
    make_folder(tmp_path, name="b")
    billing = folder / "prompts/billing.md"
    billing.write_text(billing.read_text() + "Test caller SSN 219-09-9999.\n")
    code, out, _ = run("lint", "agents")
    assert code == 1 and "VK201" in out


def test_lint_folder_without_agents(run, tmp_path):
    (tmp_path / "empty").mkdir()
    code, _, err = run("lint", "empty")
    assert code == 1 and "no agent.yaml here" in err


def test_errors_are_friendly(run, tmp_path):
    code, _, err = run("lint", "does-not-exist")
    assert code == 1 and "agent.yaml not found" in err


def test_graph_draws_appointment_scheduling_template(run):
    assert run("templates", "install", "appointment-scheduling")[0] == 0
    code, out, err = run("graph", "agents/appointment-scheduling")
    assert code == 0 and not err
    lines = out.splitlines()
    assert lines[:12] == [
        "flowchart TD",
        '    s1["Greeting"]:::startStep',
        '    s2["Collect details"]:::agentStep',
        '    s3["Confirm"]:::agentStep',
        '    s4["Request booked"]:::endStep',
        '    s5["Not scheduling"]:::endStep',
        '    s6["Persona"]:::globalStep',
        '    s1 -->|"wants appointment"| s2',
        '    s1 -->|"other request"| s5',
        '    s2 -->|"details collected"| s3',
        '    s3 -->|"details confirmed"| s4',
        '    s3 -->|"details need changes"| s2',
    ]
    rules = [line.split() for line in lines[12:]]
    assert len(rules) == 4
    assert all(keyword == "classDef" for keyword, _, _ in rules)
    assert {name for _, name, _ in rules} == {"startStep", "agentStep", "endStep", "globalStep"}
    assert len({style for _, _, style in rules}) == 4


def test_graph_escapes_names_and_route_labels(run, tmp_path):
    name = '选择 "time" | [text] #34; <b>&</b> \\ ` %%{init}%%\r\nnext\rlast\nline'
    escaped = (
        "选择 #34;time#34; #124; #91;text#93; #35;34; #60;b#62;#38;#60;/b#62; "
        "#92; #96; #37;#37;{init}#37;#37; next last line"
    )
    doc = {
        "vk_format": 1,
        "nodes": [{"id": "end", "type": "startCall", "data": {"name": name}},
                  {"id": "a | b", "type": "customStep"}],
        "edges": [{"source": "end", "target": "a | b", "data": {"label": name}},
                  {"source": "end", "target": "a | b"}],
    }
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(doc, allow_unicode=True), encoding="utf-8")
    code, out, err = run("graph", ".")
    assert code == 0 and not err
    assert out.splitlines()[1:5] == [
        f'    s1["{escaped}"]:::startStep',
        '    s2["a #124; b"]',
        f'    s1 -->|"{escaped}"| s2',
        "    s1 --> s2",
    ]


def test_graph_reports_missing_folder(run):
    code, out, err = run("graph", "does-not-exist")
    assert code == 1 and not out
    assert "agent.yaml not found" in err


@pytest.mark.parametrize(("nodes", "edges", "error"), [
    ([{"type": "startCall"}], [], "step 1 needs an ID"),
    ([{"id": 1}, {"id": "1"}], [], "duplicate step ID '1'"),
    ([{"id": "a"}], [{"source": "a", "target": "missing"}], "route 1 ('a' to 'missing')"),
])
def test_graph_rejects_invalid_step_ids(run, tmp_path, nodes, edges, error):
    doc = {"vk_format": 1, "nodes": nodes, "edges": edges}
    (tmp_path / "agent.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    code, out, err = run("graph", ".")
    assert code == 1 and not out
    assert error in err


def test_engine_unreachable_is_explained(run, tmp_path, monkeypatch):
    from vk import cli

    folder = make_folder(tmp_path, {"t": {"name": "t", "turns": [{"user": "hi"}]}})
    code = cli.main(["test", str(folder), "--url", "http://127.0.0.1:9"])   # nothing listens on port 9
    assert code == 1


def test_skills_install(run, tmp_path):
    code, out, _ = run("skills", "install", "--target", "claude")
    assert code == 0
    for name in ("voice-agent-builder", "voice-agent-reviewer", "voice-agent-fixer", "dialogflow-tester"):
        text = (tmp_path / ".claude/skills" / name / "SKILL.md").read_text()
        assert text.startswith("---\nname: " + name)


def test_engine_key_never_written_into_agent_folder(run, tmp_path):
    folder = make_folder(tmp_path, {"t": {"name": "t", "turns": [{"user": "hello"}]}})
    run("test", str(folder))
    for path in Path(folder).rglob("*"):
        if path.is_file():
            assert "test-key" not in path.read_text()


def test_templates_list_and_install(run, tmp_path):
    code, out, _ = run("templates", "list")
    assert code == 0
    for name in ("front-desk", "appointment-scheduling", "bilingual-front-desk"):
        assert name in out
    code, out, _ = run("templates", "install", "front-desk", "--name", "Main Line")
    assert code == 0 and "agents/front-desk" in out
    doc = yaml.safe_load((tmp_path / "agents/front-desk/agent.yaml").read_text())
    assert doc["name"] == "Main Line" and "workflow_id" not in doc
    assert (tmp_path / "agents/front-desk/tests/appointment.yaml").exists()
    assert run("lint", "agents/front-desk", "--strict")[0] == 0
    code, _, err = run("templates", "install", "front-desk")
    assert code == 1 and "already exists" in err
    assert run("templates", "install", "nope")[0] == 1


def test_junit_report_records_pass_and_failure_with_transcripts(run, tmp_path):
    folder = make_folder(tmp_path, {
        "passes": {"name": "greeting <&>", "turns": [{"user": "café & <hello>\x01"}]},
        "fails": {
            "name": "missing answer",
            "turns": [{"user": "hello"}],
            "expect": {"says": ["<never & now>"]},
        },
    })
    code, out, err = run("test", str(folder), "--format", "junit")
    assert code == 1 and not err
    report = ElementTree.fromstring(out)
    assert report.tag == "testsuites"
    assert report.attrib["tests"] == "2"
    assert report.attrib["failures"] == "1"
    suite = report.find("testsuite")
    assert suite.attrib["name"] == "engine"
    assert suite.attrib["tests"] == "2"
    assert suite.attrib["failures"] == "1"
    cases = {case.attrib["name"]: case for case in suite.findall("testcase")}
    passed = cases["greeting <&>"]
    assert passed.attrib["file"] == "passes.yaml"
    assert passed.find("failure") is None
    assert "caller: café & <hello>�" in passed.findtext("system-out")
    assert "<never & now>" in cases["missing answer"].findtext("failure")
    assert "caller: hello" in cases["missing answer"].findtext("system-out")


def test_template_then_test(run, tmp_path):
    run("templates", "install", "appointment-scheduling", "--dir", "sched")
    code, out, _ = run("test", "sched", "-k", "redirected")
    # the keyword fake follows edges whose label appears in the message: "other request"
    assert "Greeting" in out


def test_old_folders_with_a_workflow_id_still_load(tmp_path):
    from vk import agentfs

    folder = make_folder(tmp_path)
    doc = yaml.safe_load((folder / "agent.yaml").read_text())
    doc["workflow_id"] = 7
    (folder / "agent.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    agent = agentfs.load(folder)
    agentfs.write(agent, folder)
    assert "workflow_id" not in (folder / "agent.yaml").read_text()


def test_every_template_is_lint_clean_and_round_trips(tmp_path):
    from importlib import resources

    from vk import agentfs as afs
    from vk.lint import lint as run_lint

    for tpl in (resources.files("vk") / "templates").iterdir():
        agent = afs.load(tpl)
        assert run_lint(agent, tpl) == [], tpl.name
        afs.write(agent, tmp_path / tpl.name)
        assert afs.load(tmp_path / tpl.name).definition == agent.definition


def _flaky_runs(monkeypatch, pattern):
    """scenario.run passes or fails in turn, following `pattern` (True = pass), like a model that varies."""
    from vk import cli, scenario
    real, seen = scenario.run, iter(pattern * 10)

    def run(client, sc, context):
        result = real(client, sc, context)
        return result if next(seen) else scenario.Result(sc, result.transcript, ["expected the agent to say 'noon'"],
                                                         result.seconds)
    monkeypatch.setattr(cli.scenario, "run", run)


def test_repeat_reports_flaky_tests(run, tmp_path, monkeypatch):
    import json
    make_folder(tmp_path, {"hours": {"name": "hours question", "turns": [{"user": "hello"}]}})
    _flaky_runs(monkeypatch, [True, False, True])
    code, out, _ = run("test", "agents/front-desk", "--repeat", "3")
    assert code == 1 and "FLAKY  hours question" in out and "passed 2 of 3 runs" in out
    assert "expected the agent to say 'noon'" in out and "0 passed, 0 failed, 1 flaky" in out
    _flaky_runs(monkeypatch, [True, False, True])
    code, out, _ = run("test", "agents/front-desk", "--repeat", "3", "--allow-flaky", "--format", "json")
    [r] = json.loads(out)
    assert code == 0 and r["runs"] == 3 and r["passes"] == 2 and r["flaky"] is True and r["passed"] is False


def test_junit_report_respects_allow_flaky(run, tmp_path, monkeypatch):
    folder = make_folder(tmp_path, {"hours": {"name": "hours question", "turns": [{"user": "hello"}]}})
    _flaky_runs(monkeypatch, [True, False])
    code, out, _ = run("test", str(folder), "--repeat", "2", "--format", "junit")
    assert code == 1 and ElementTree.fromstring(out).attrib["failures"] == "1"

    _flaky_runs(monkeypatch, [True, False])
    code, out, _ = run("test", str(folder), "--repeat", "2", "--allow-flaky", "--format", "junit")
    report = ElementTree.fromstring(out)
    assert code == 0 and report.attrib["failures"] == "0"
    case = report.find("./testsuite/testcase")
    assert case.find("failure") is None
    assert "Flaky (allowed): 1 of 2 runs passed" in case.findtext("system-out")


def test_repeat_passed_and_failed_every_time(run, tmp_path, monkeypatch):
    import json
    make_folder(tmp_path, {"t": {"name": "t", "turns": [{"user": "hello"}]}})
    code, out, _ = run("test", "agents/front-desk", "--repeat", "2", "--format", "json")
    [r] = json.loads(out)
    assert code == 0 and r == {**r, "runs": 2, "passes": 2, "flaky": False, "passed": True}
    _flaky_runs(monkeypatch, [False])
    code, out, _ = run("test", "agents/front-desk", "--repeat", "2", "--allow-flaky")
    assert code == 1 and "FAIL  t" in out and "passed 0 of 2 runs" in out and "0 passed, 1 failed" in out


def test_repeat_is_bounded_and_one_run_is_unchanged(run, tmp_path):
    import json
    make_folder(tmp_path, {"t": {"name": "t", "turns": [{"user": "hello"}]}})
    code, _, err = run("test", "agents/front-desk", "--repeat", "11")
    assert code == 1 and "--repeat is 1 to 10" in err
    code, out, _ = run("test", "agents/front-desk", "--format", "json")
    [r] = json.loads(out)
    assert code == 0 and r["runs"] == 1 and r["passes"] == 1 and r["flaky"] is False
    code, out, _ = run("test", "agents/front-desk")
    assert "runs)" not in out and "1 passed, 0 failed" in out and "flaky" not in out


def test_version_matches_the_version_file(capsys):
    """vk --version is the release's version (VERSION at the repo root)."""
    import pytest as _pytest
    from vk import __version__, cli
    root = Path(__file__).resolve().parents[2]
    assert __version__ == (root / "VERSION").read_text().strip()
    with _pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.strip() == f"vk {__version__}"


def test_a_closed_pipe_is_not_an_error(monkeypatch):
    """`vk templates list | head -1` ends quietly instead of printing a traceback."""
    from vk import cli

    def gone(msg=""):
        raise BrokenPipeError
    monkeypatch.setattr(cli, "_out", gone)
    monkeypatch.setattr(cli.os, "dup2", lambda *a: None)
    assert cli.main(["templates", "list"]) == 0
