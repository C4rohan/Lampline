import pytest

from conftest import TEMPLATES, mini_definition
from vpengine.graph import Graph, render


@pytest.mark.parametrize("text,expected", [
    ("Hi {{name}}", "Hi Sam"),
    ("Hi {{ name }}!", "Hi Sam!"),
    ("Hi {{missing}}.", "Hi ."),
    ("Hi {{missing | there}}.", "Hi there."),
    ("Hi {{missing | \"friend\"}}.", "Hi friend."),
    ("Hi {{name | there}}.", "Hi Sam."),
    ("{{empty | fallback}}", "fallback"),
])
def test_render(text, expected):
    assert render(text, {"name": "Sam", "empty": ""}) == expected


def test_front_desk_template_loads():
    g = Graph.from_folder(TEMPLATES / "front-desk")
    assert g.start.name == "Greeting"
    assert "Riverside Clinic" in g.persona or "{{business_name}}" in g.persona
    router = next(s for s in g.steps.values() if s.name == "Router")
    assert {r.tool_name for r in router.routes} >= {"appointments"}
    assert any(s.kind == "end" for s in g.steps.values())


def test_system_prompt_composition():
    g = Graph.from_definition("Mini", mini_definition(), {"business_name": "Riverside Clinic"})
    start = g.start
    prompt = g.system_prompt(start, g.context)
    assert prompt.startswith("You are Ava at Riverside Clinic.")
    assert "Greet the caller." in prompt and "call that tool" in prompt
    bye = g.steps["3"]
    assert "last thing you say" in g.system_prompt(bye, g.context)


def test_persona_can_be_turned_off():
    d = mini_definition()
    d["nodes"][1]["data"]["add_global_prompt"] = False
    g = Graph.from_definition("Mini", d)
    assert "Ava" not in g.system_prompt(g.steps["2"], {})


def test_needs_exactly_one_start():
    d = mini_definition()
    d["nodes"] = [n for n in d["nodes"] if n["type"] != "startCall"]
    with pytest.raises(ValueError, match="exactly one start"):
        Graph.from_definition("x", d)


def test_edges_to_missing_steps_are_ignored():
    d = mini_definition()
    d["edges"].append({"id": "z", "source": "2", "target": "404", "data": {"label": "x", "condition": "y"}})
    g = Graph.from_definition("x", d)
    assert [r.target for r in g.steps["2"].routes] == ["3"]
