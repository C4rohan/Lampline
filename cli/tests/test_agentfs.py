import copy

import pytest

from conftest import load_fixture
from vk import agentfs


def make_agent(**overrides):
    defaults = dict(name="Front Desk", definition=load_fixture(), context_variables={"first_name": "Sam"},
                    setup={"llm": "cloud"}, lint={"ignore": []})
    defaults.update(overrides)
    return agentfs.Agent(**defaults)


def test_json_folder_json_is_lossless(tmp_path):
    agent = make_agent()
    agentfs.write(agent, tmp_path / "a")
    back = agentfs.load(tmp_path / "a")
    assert back.definition == agent.definition
    assert back.name == agent.name
    assert back.context_variables == agent.context_variables
    assert back.setup == {"llm": "cloud"}


def test_folder_json_folder_reproduces_files(tmp_path):
    agentfs.write(make_agent(), tmp_path / "a")
    first = {p.relative_to(tmp_path / "a"): p.read_text() for p in (tmp_path / "a").rglob("*") if p.is_file()}
    agentfs.write(agentfs.load(tmp_path / "a"), tmp_path / "b")
    second = {p.relative_to(tmp_path / "b"): p.read_text() for p in (tmp_path / "b").rglob("*") if p.is_file()}
    assert first == second


def test_prompts_become_markdown_files(tmp_path):
    agentfs.write(make_agent(), tmp_path)
    assert (tmp_path / "prompts/greeting.md").read_text() == (
        "Greet the caller and ask how you can help today.\nKeep it under two sentences.\n"
    )
    assert (tmp_path / "prompts/reschedule.extraction.md").exists()
    yaml_text = (tmp_path / "agent.yaml").read_text()
    assert "file: prompts/router.md" in yaml_text
    assert "Work out why the caller" not in yaml_text  # prompt text lives only in the .md file


@pytest.mark.parametrize("prompt", ["ends with newline\n", "two newlines\n\n", "", "  spaces  "])
def test_whitespace_round_trips_exactly(tmp_path, prompt):
    definition = load_fixture()
    definition["nodes"][1]["data"]["prompt"] = prompt
    agentfs.write(make_agent(definition=definition), tmp_path)
    assert agentfs.load(tmp_path).definition["nodes"][1]["data"]["prompt"] == prompt


def test_duplicate_names_get_unique_files(tmp_path):
    definition = load_fixture()
    definition["nodes"][3]["data"]["name"] = "Reschedule"  # same as node 3
    files = agentfs.render_files(make_agent(definition=definition))
    assert "prompts/reschedule.md" in files and "prompts/reschedule-2.md" in files
    agentfs.write(make_agent(definition=definition), tmp_path)
    assert agentfs.load(tmp_path).definition == definition


def test_renamed_node_removes_stale_prompt_file(tmp_path):
    agentfs.write(make_agent(), tmp_path)
    definition = load_fixture()
    definition["nodes"][3]["data"]["name"] = "Payments"
    agentfs.write(make_agent(definition=definition), tmp_path)
    assert not (tmp_path / "prompts/billing.md").exists()
    assert (tmp_path / "prompts/payments.md").exists()


def test_missing_prompt_file_is_a_clear_error(tmp_path):
    agentfs.write(make_agent(), tmp_path)
    (tmp_path / "prompts/router.md").unlink()
    with pytest.raises(agentfs.FolderError, match="prompts/router.md is missing"):
        agentfs.load(tmp_path)


def test_render_without_local_keys_ignores_setup():
    a = agentfs.render_files(make_agent(setup={"x": 1}), include_local=False)
    b = agentfs.render_files(make_agent(setup={"y": 2}), include_local=False)
    assert a == b


def test_extra_top_level_keys_survive(tmp_path):
    definition = copy.deepcopy(load_fixture())
    assert "viewport" in definition
    agentfs.write(make_agent(definition=definition), tmp_path)
    assert agentfs.load(tmp_path).definition["viewport"] == definition["viewport"]


def test_prompt_files_stay_inside_the_folder(tmp_path):
    import pytest
    secret = tmp_path / "settings.json"
    secret.write_text('{"anthropic_key": "not-for-prompts"}')
    folder = tmp_path / "agent"
    (folder / "prompts").mkdir(parents=True)
    for ref in (str(secret), "../settings.json", "prompts/../../settings.json", "agent.yaml"):
        (folder / "agent.yaml").write_text(
            "vk_format: 1\nname: x\nnodes:\n- id: s\n  type: startCall\n  data: {name: Start, prompt: {file: '%s'}}\nedges: []\n" % ref)
        with pytest.raises(agentfs.FolderError, match="inside the agent's folder"):
            agentfs.load(folder)
    a = agentfs.Agent(name="x", definition={"nodes": [{"id": "s", "type": "startCall", "data": {"prompt": {"file": str(secret)}}}], "edges": []})
    with pytest.raises(agentfs.FolderError, match="must be text"):
        agentfs.render_files(a)
