import json
from pathlib import Path

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


def test_errors_are_friendly(run, tmp_path):
    code, _, err = run("lint", "does-not-exist")
    assert code == 1 and "agent.yaml not found" in err


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
