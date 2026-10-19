"""Starting local Whisper/Kokoro from the UI runs fixed docker compose commands."""

import asyncio
import os
import stat
from dataclasses import replace

import httpx
import pytest

from vpengine.server import Engine, create_app


def fake_docker(tmp_path, *, fail_with: str = "") -> str:
    log = tmp_path / "docker-args.txt"
    script = tmp_path / "docker"
    body = f'echo "$@" >> "{log}"\n'
    body += f'echo "{fail_with}" >&2; exit 1\n' if fail_with else 'echo " Container vpe-speech-kokoro-1  Started"\n'
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return str(script)


@pytest.fixture
def make(cfg, agents_dir, fake_url, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "local").mkdir(parents=True)
    (repo / "local/docker-compose.speech.yml").write_text("services: {}\n")
    monkeypatch.setenv("VPE_REPO_ROOT", str(repo))

    def build(docker: str | None, kokoro_url: str = fake_url):
        if docker:
            monkeypatch.setenv("VPE_DOCKER", docker)
        else:
            monkeypatch.setenv("VPE_DOCKER", str(tmp_path / "no-docker-here"))
        local_cfg = replace(cfg, stt="whisper-server", whisper_url=fake_url, tts="kokoro-server", kokoro_url=kokoro_url)
        engine = Engine(agents_dir, local_cfg)
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost")
        return client, engine
    return build


async def wait_state(c, want, timeout=15):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        st = (await c.get("/v1/local-speech")).json()
        if st["state"] in want:
            return st
        await asyncio.sleep(0.1)
    raise AssertionError(f"state never reached {want}: {st}")


async def test_start_runs_compose_up_and_downloads_the_model(make, tmp_path, requests_seen):
    c, _ = make(fake_docker(tmp_path))
    st = (await c.post("/v1/local-speech/start")).json()
    assert st["available"] and st["docker"]
    st = await wait_state(c, {"idle"})
    assert st["message"] == "Running" and st["servers"] == {"whisper": True, "kokoro": True}
    args = (tmp_path / "docker-args.txt").read_text()
    assert "compose -f" in args and args.strip().endswith("up -d") and "docker-compose.speech.yml" in args
    assert any(r.get("path", "").endswith("/models/Systran/faster-whisper-small") for r in requests_seen)
    st = (await c.post("/v1/local-speech/stop")).json()
    await wait_state(c, {"idle"})
    assert (tmp_path / "docker-args.txt").read_text().strip().endswith("down")
    await c.aclose()


async def test_docker_not_running_is_explained(make, tmp_path):
    c, _ = make(fake_docker(tmp_path, fail_with="Cannot connect to the Docker daemon at unix:///var/run/docker.sock"))
    await c.post("/v1/local-speech/start")
    st = await wait_state(c, {"error"})
    assert "open OrbStack" in st["message"]
    await c.aclose()


async def test_no_docker_installed(make):
    c, _ = make(None, kokoro_url="http://127.0.0.1:9/v1")
    st = (await c.get("/v1/local-speech")).json()
    assert not st["docker"] and st["servers"]["kokoro"] is False
    await c.aclose()


async def test_cannot_be_started_through_a_tunnel(make, tmp_path):
    c, _ = make(fake_docker(tmp_path))
    r = await c.post("/v1/local-speech/start", headers={"x-forwarded-for": "203.0.113.9"})
    assert r.status_code == 403 and not (tmp_path / "docker-args.txt").exists()
    assert (await c.post("/v1/local-speech/rm-rf")).status_code == 404
    await c.aclose()
