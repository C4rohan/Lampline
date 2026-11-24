"""The tough-caller pack: personas shipped in vk/personas/*.yaml, used by name in caller: blocks."""

import pytest

from conftest import make_folder
from vk import personas, scenario


def test_the_pack_is_well_formed():
    shipped = personas.all()
    names = {p["name"] for p in shipped}
    assert {"angry-wants-human", "elderly-repeats", "rambling-story",
            "english-second-language", "changes-mind", "scattered-details"} <= names
    assert len(shipped) >= 6 and personas.names() == sorted(names)
    for p in shipped:
        assert personas.NAME.fullmatch(p["name"])                       # how caller: blocks refer to it
        assert p["description"] and len(p["persona"]) > 40              # something to show, and text to play


def test_a_named_persona_resolves(tmp_path):
    doc = {"name": "tough caller", "caller": {"persona": "angry-wants-human", "goal": "talk to a person"}}
    sc = scenario.load(make_folder(tmp_path, {"t": doc}))[0]
    assert sc.caller["persona"] == personas.lookup("angry-wants-human")["persona"]
    assert sc.caller["persona"] != "angry-wants-human"                  # the text, not the name


def test_a_named_persona_reaches_the_engine(run, engine, tmp_path):
    doc = {"name": "tough caller", "caller": {"persona": "angry-wants-human", "goal": "a person",
                                              "opening": "I want to speak to a person."}}
    code, out, err = run("test", str(make_folder(tmp_path, {"t": doc})))
    assert code == 0, out + err
    assert engine.caller_asks and engine.caller_asks[0]["persona"] == personas.lookup("angry-wants-human")["persona"]


def test_an_inline_persona_still_works(run, engine, tmp_path):
    doc = {"name": "inline", "caller": {"persona": "Sam, busy and a little rude", "goal": "reschedule"}}
    sc = scenario.load(make_folder(tmp_path, {"t": doc}))[0]
    assert sc.caller["persona"] == "Sam, busy and a little rude"
    folder = make_folder(tmp_path / "b", {"t": {**doc, "caller": {**doc["caller"], "opening": "hi"}}})
    code, _, _ = run("test", str(folder))
    assert code == 0 and engine.caller_asks[0]["persona"] == "Sam, busy and a little rude"


def test_a_one_word_persona_is_still_inline_text(tmp_path):
    doc = {"name": "one word", "caller": {"persona": "grumpy"}}
    assert scenario.load(make_folder(tmp_path, {"t": doc}))[0].caller["persona"] == "grumpy"


def test_an_unknown_name_is_explained(tmp_path):
    folder = make_folder(tmp_path, {"x": {"name": "x", "caller": {"persona": "furious-wants-human", "goal": "y"}}})
    with pytest.raises(scenario.TestFileError, match="no persona 'furious-wants-human'"):
        scenario.load(folder)


def test_lookup_and_name_detection():
    assert personas.lookup("angry-wants-human")["description"]
    assert personas.lookup("Sam, busy") is None and personas.lookup("") is None
    assert personas.looks_like_name("angry-wants-human") and personas.looks_like_name("rambling_story")
    assert not personas.looks_like_name("grumpy") and not personas.looks_like_name("Sam, busy")
