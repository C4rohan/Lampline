"""Start, stop and watch the free local speech servers (Whisper and Kokoro) from the UI.

They run in Docker from local/docker-compose.speech.yml, the same file
scripts/speech-up.sh uses. The engine only ever runs these fixed commands;
nothing from the browser reaches the command line.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path

import httpx

from .services import Config

COMPOSE = Path("local") / "docker-compose.speech.yml"
DOCKER_PATHS = ("/usr/local/bin/docker", "/opt/homebrew/bin/docker",
                str(Path.home() / ".orbstack" / "bin" / "docker"), "/usr/bin/docker")


def find_compose(start: Path) -> Path | None:
    """The compose file in the repo: VPE_REPO_ROOT, or walk up from the agents folder / package."""
    roots = [os.environ.get("VPE_REPO_ROOT", ""), str(start), str(Path(__file__).resolve())]
    for root in filter(None, roots):
        p = Path(root).resolve()
        for d in (p, *p.parents):
            if (d / COMPOSE).is_file():
                return d / COMPOSE
    return None


def find_docker() -> str | None:
    custom = os.environ.get("VPE_DOCKER", "").strip()
    if custom:
        return custom if os.access(custom, os.X_OK) else None
    found = shutil.which("docker")
    if found:
        return found
    return next((p for p in DOCKER_PATHS if os.access(p, os.X_OK)), None)


class LocalSpeech:
    def __init__(self, agents_dir: Path):
        self.compose = find_compose(agents_dir)
        self.state = "idle"            # idle | starting | downloading | stopping | error
        self.message = ""
        self.log: list[str] = []
        self._task: asyncio.Task | None = None
        self.started_at: float | None = None

    def _note(self, line: str) -> None:
        line = line.rstrip()
        if line:
            self.log.append(line[:300])
            del self.log[:-60]

    async def _run(self, *args: str, timeout: float = 900) -> tuple[int, str]:
        docker = find_docker()
        if docker is None:
            return 127, "Docker isn't installed (on a Mac, install OrbStack and open it once)"
        proc = await asyncio.create_subprocess_exec(
            docker, "compose", "-f", str(self.compose), *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, cwd=str(self.compose.parent.parent))
        out: list[str] = []
        try:
            async def read():
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    line = raw.decode("utf-8", "replace")
                    out.append(line)
                    self._note(line)
            await asyncio.wait_for(read(), timeout)
            code = await proc.wait()
        except asyncio.TimeoutError:
            proc.kill()
            return 124, "timed out"
        return code, "".join(out)

    async def probe(self, cfg: Config) -> dict:
        async def up(url: str) -> bool:
            try:
                async with httpx.AsyncClient(timeout=1.5) as http:
                    return (await http.get(url)).status_code < 500
            except httpx.HTTPError:
                return False
        whisper, kokoro = await asyncio.gather(up(cfg.whisper_url.rstrip("/") + "/models"),
                                               up(cfg.kokoro_url.rstrip("/") + "/audio/voices"))
        return {"whisper": whisper, "kokoro": kokoro}

    async def status(self, cfg: Config) -> dict:
        servers = await self.probe(cfg)
        return {"state": self.state, "message": self.message, "servers": servers,
                "available": self.compose is not None, "docker": find_docker() is not None,
                "log": self.log[-15:],
                "seconds": round(time.time() - self.started_at) if self.started_at and self.state != "idle" else None}

    def start(self, cfg: Config) -> None:
        if self.compose is None:
            raise RuntimeError("can't find local/docker-compose.speech.yml; start it with scripts/speech-up.sh")
        if self._task and not self._task.done():
            return
        self.log.clear()
        self._task = asyncio.create_task(self._start(cfg))

    async def _start(self, cfg: Config) -> None:
        self.state, self.message, self.started_at = "starting", "Starting the speech servers…", time.time()
        code, out = await self._run("up", "-d")
        if code != 0:
            self.state = "error"
            low = out.lower()
            self.message = ("Docker isn't running: open OrbStack (or Docker Desktop) and try again."
                            if "cannot connect" in low or "daemon" in low or "is the docker" in low
                            else out.strip().splitlines()[-1] if out.strip() else f"docker compose failed ({code})")
            return
        self.message = "Waiting for the servers to answer (the first start downloads them, a few minutes)…"
        deadline = time.time() + 900
        while time.time() < deadline:
            s = await self.probe(cfg)
            if s["whisper"] and s["kokoro"]:
                break
            await asyncio.sleep(3)
        else:
            self.state, self.message = "error", "The servers didn't answer within 15 minutes; see the log."
            return
        self.state, self.message = "downloading", f"Downloading the Whisper model {cfg.whisper_model} (once)…"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(900.0, connect=5.0)) as http:
                r = await http.post(cfg.whisper_url.rstrip("/") + f"/models/{cfg.whisper_model}")
            if r.status_code >= 400:
                self._note(f"model download answered {r.status_code}; it will download on first use")
        except httpx.HTTPError as exc:
            self._note(f"model download didn't finish ({exc.__class__.__name__}); it will download on first use")
        self.state, self.message = "idle", "Running"

    def stop(self) -> None:
        if self.compose is None:
            raise RuntimeError("can't find local/docker-compose.speech.yml")
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._stop())

    async def _stop(self) -> None:
        self.state, self.message, self.started_at = "stopping", "Stopping…", time.time()
        code, out = await self._run("down")
        self.state = "idle" if code == 0 else "error"
        self.message = "Stopped" if code == 0 else (out.strip().splitlines() or ["docker compose down failed"])[-1]
