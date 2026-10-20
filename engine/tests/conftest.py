import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))
import fakellm  # noqa: E402

TEMPLATES = Path(__file__).resolve().parents[2] / "cli/src/vk/templates"


@pytest.fixture(scope="session")
def fake_url():
    srv, url = fakellm.serve()
    yield url
    srv.shutdown()


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    """Records, knowledge and call history go to the test's own folder, never ~/.local/share."""
    monkeypatch.setenv("VPE_DATA_DIR", str(tmp_path / "data"))


@pytest.fixture
def requests_seen():
    fakellm.REQUESTS.clear()
    return fakellm.REQUESTS


@pytest.fixture
def cfg(fake_url):
    from vpengine.services import Config
    return Config(llm="openai", model="fake-model", base_url=fake_url)


@pytest.fixture
def agents_dir(tmp_path):
    d = tmp_path / "agents"
    d.mkdir()
    for name in ("front-desk", "appointment-scheduling"):
        shutil.copytree(TEMPLATES / name, d / name)
    return d


def mini_definition(*, greeting="Hello from {{business_name | the office}}.", speech="", extraction=False):
    """Start -> Help -> Bye, small enough to reason about in tests."""
    start = {"name": "Start", "prompt": "Greet the caller.", "greeting": greeting}
    if extraction:
        start.update(extraction_enabled=True, extraction_prompt="Note the caller's details.",
                     extraction_variables=[{"name": "caller_name", "type": "string", "prompt": "their name"},
                                           {"name": "is_new", "type": "boolean", "prompt": "new caller"}])
    return {
        "nodes": [
            {"id": "1", "type": "startCall", "data": start},
            {"id": "2", "type": "agentNode", "data": {"name": "Help", "prompt": "Answer questions about parking."}},
            {"id": "3", "type": "endCall", "data": {"name": "Bye", "prompt": "Say goodbye."}},
            {"id": "9", "type": "globalNode", "data": {"name": "Persona", "prompt": "You are Ava at {{business_name}}."}},
        ],
        "edges": [
            {"id": "a", "source": "1", "target": "2",
             "data": {"label": "parking question", "condition": "caller asks about parking",
                      "transition_speech": speech}},
            {"id": "b", "source": "2", "target": "3",
             "data": {"label": "goodbye", "condition": "caller says goodbye or is finished"}},
        ],
    }


def write_agent(folder: Path, definition: dict, context: dict | None = None, name="Mini"):
    from vk import agentfs
    agentfs.write(agentfs.Agent(name=name, definition=definition, context_variables=context or {}), folder)
    return folder


@pytest.fixture
def mini():
    return mini_definition
