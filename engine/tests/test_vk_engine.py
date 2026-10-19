"""`vk test`: the folder's tests run on the engine as they are on disk."""

import socket
import threading
import time

import pytest
import uvicorn
import yaml

from conftest import mini_definition, write_agent
from vk.cli import main as vk_main
from vpengine.server import Engine, create_app


@pytest.fixture
def engine_url(cfg, agents_dir):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(Engine(agents_dir, cfg)), host="127.0.0.1", port=port,
                                           log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    t.join(10)


def mini_folder(tmp_path, tests: dict):
    folder = write_agent(tmp_path / "mini", mini_definition(), {"business_name": "Riverside"})
    (folder / "tests").mkdir()
    for name, doc in tests.items():
        (folder / "tests" / f"{name}.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return folder


PASSING = {
    "name": "parking then goodbye",
    "turns": [{"user": "I have a parking question", "expect": {"reaches": "Help"}},
              {"user": "thanks, goodbye"}],
    "expect": {"reaches": ["Start", "Help", "Bye"], "calls_tool": ["parking_question", "goodbye"],
               "says": ["hello from riverside", "parking"], "not_says": "card number", "ends": True},
}


def test_vk_test_on_engine_passes(engine_url, tmp_path, capsys):
    folder = mini_folder(tmp_path, {"ok": PASSING})
    assert vk_main(["test", str(folder), "--engine", engine_url]) == 0
    out = capsys.readouterr().out
    assert "PASS  parking then goodbye" in out and "Start → Help → Bye" in out and "1 passed, 0 failed" in out


def test_vk_test_on_engine_reports_failures(engine_url, tmp_path, capsys):
    bad = {"name": "wrong", "turns": [{"user": "a parking question"}], "expect": {"reaches": "Bye", "ends": True}}
    folder = mini_folder(tmp_path, {"bad": bad})
    assert vk_main(["test", str(folder), "--engine", engine_url, "--format", "json"]) == 1
    import json
    result = json.loads(capsys.readouterr().out)[0]
    assert not result["passed"] and any("expected to reach 'Bye'" in f for f in result["failures"])
    assert any("expected the conversation to end" in f for f in result["failures"])


def test_vk_test_engine_uses_env_url(engine_url, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VK_ENGINE_URL", engine_url)
    folder = mini_folder(tmp_path, {"ok": PASSING})
    assert vk_main(["test", str(folder)]) == 0


def test_vk_test_engine_unreachable(tmp_path, capsys):
    folder = mini_folder(tmp_path, {"ok": PASSING})
    assert vk_main(["test", str(folder), "--engine", "http://127.0.0.1:9"]) == 1
    assert "cannot reach the engine" in capsys.readouterr().err
