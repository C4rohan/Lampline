from conftest import load_fixture
from importlib import resources

from vk import agentfs
from vk.lint import has_errors, lint, template_variables


def agent(definition=None, context=None, lint_cfg=None):
    return agentfs.Agent(
        name="Front Desk",
        definition=definition if definition is not None else load_fixture(),
        context_variables={"first_name": "Sam"} if context is None else context,
        lint=lint_cfg or {},
    )


def codes(findings):
    return sorted({f.code for f in findings})


def node(defn, node_id):
    return next(n for n in defn["nodes"] if n["id"] == node_id)


def test_clean_fixture_has_no_findings():
    assert lint(agent()) == []


def test_requires_exactly_one_start():
    d = load_fixture()
    node(d, "1")["type"] = "agentNode"
    assert "VK001" in codes(lint(agent(d)))


def test_only_one_global_node():
    d = load_fixture()
    d["nodes"].append({"id": "g2", "type": "globalNode", "position": {}, "data": {"name": "Persona 2", "prompt": "x"}})
    assert "VK003" in codes(lint(agent(d)))


def test_edge_to_missing_node():
    d = load_fixture()
    d["edges"].append({"id": "x", "source": "2", "target": "nope", "data": {"label": "x", "condition": "y"}})
    assert "VK004" in codes(lint(agent(d)))


def test_edge_needs_label_and_condition():
    d = load_fixture()
    d["edges"][1]["data"]["condition"] = "  "
    assert "VK005" in codes(lint(agent(d)))


def test_duplicate_transition_tool_names():
    d = load_fixture()
    d["edges"][2]["data"]["label"] = "Reschedule!"  # becomes tool "reschedule_" vs "reschedule"
    d["edges"][2]["data"]["label"] = "reschedule"
    assert "VK006" in codes(lint(agent(d)))


def test_connection_rules():
    d = load_fixture()
    d["edges"].append({"id": "5-2", "source": "5", "target": "2", "data": {"label": "end out", "condition": "c"}})
    d["edges"].append({"id": "g-2", "source": "g", "target": "2", "data": {"label": "global out", "condition": "c2"}})
    assert "VK007" in codes(lint(agent(d)))


def test_unreachable_and_orphan_nodes():
    d = load_fixture()
    d["nodes"].append({"id": "7", "type": "agentNode", "position": {}, "data": {"name": "Orphan", "prompt": "p"}})
    found = codes(lint(agent(d)))
    assert "VK008" in found and "VK101" in found


def test_dead_end_warning():
    d = load_fixture()
    d["edges"] = [e for e in d["edges"] if e["source"] != "4"]
    findings = lint(agent(d))
    assert "VK103" in codes(findings)
    assert not has_errors(findings)


def test_prompt_budget():
    d = load_fixture()
    node(d, "2")["data"]["prompt"] = "x" * 9000
    assert "VK105" in codes(lint(agent(d)))
    assert "VK105" not in codes(lint(agent(d, lint_cfg={"prompt_budget_chars": 10000})))


def test_ambiguous_sibling_conditions():
    d = load_fixture()
    d["edges"][2]["data"]["condition"] = d["edges"][1]["data"]["condition"]
    assert "VK106" in codes(lint(agent(d)))


def test_unprovided_template_variable():
    findings = lint(agent(context={}))
    assert [f.message for f in findings if f.code == "VK107"] == [
        "uses {{first_name}} but nothing provides it (add it to context_variables or extract it)"
    ]


def test_template_variable_rules():
    text = "{{a}} {{b | default}} {{gathered_context.city}} {{current_time_America/New_York}} {{campaign_id}}"
    assert template_variables(text) == {"a"}


def test_pre_call_fetch_suppresses_variable_check():
    d = load_fixture()
    node(d, "1")["data"].update({"pre_call_fetch_mode": "blocking", "pre_call_fetch_url": "https://x.test/lookup"})
    assert "VK107" not in codes(lint(agent(d, context={})))


def test_duplicate_node_names():
    d = load_fixture()
    node(d, "4")["data"]["name"] = "Reschedule"
    assert "VK109" in codes(lint(agent(d)))


def test_phi_in_prompt_is_an_error():
    d = load_fixture()
    node(d, "4")["data"]["prompt"] += " Test with member id 99887766."
    findings = lint(agent(d))
    assert "VK206" in codes(findings) and has_errors(findings)


def test_phi_in_test_files(tmp_path):
    agentfs.write(agent(), tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/billing.yaml").write_text("turns:\n  - user: my SSN is 219-09-9999\n")
    assert "VK201" in codes(lint(agentfs.load(tmp_path), tmp_path))


def test_ignore_by_code_and_by_node():
    d = load_fixture()
    node(d, "2")["data"]["prompt"] = "x" * 9000
    assert "VK105" not in codes(lint(agent(d, lint_cfg={"ignore": ["VK105"]})))
    assert "VK105" not in codes(lint(agent(d, lint_cfg={"ignore": ["VK105:2"]})))
    assert "VK105" in codes(lint(agent(d, lint_cfg={"ignore": ["VK105:3"]})))


def test_transfer_to_what_the_caller_said_is_flagged():
    from vk.lint import lint
    a = agentfs.load(resources.files("vk") / "templates" / "front-desk")
    staff = next(n for n in a.definition["nodes"] if n["data"]["name"] == "Transfer to staff")
    appt = next(n for n in a.definition["nodes"] if (n["data"].get("extraction_variables") or []))
    var = appt["data"]["extraction_variables"][0]["name"]
    staff["data"]["transfer_to"] = "{{" + var + "}}"
    assert any(f.code == "VK134" and "caller says" in f.message for f in lint(a))
