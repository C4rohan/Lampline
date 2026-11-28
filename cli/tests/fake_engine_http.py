"""The test suite's FakeEngine on a real port, for running the vk GitHub Action end to end in CI.

    python cli/tests/fake_engine_http.py --port 7899 --agents DIR

serves the fake engine (API key: test-key) and, with --agents, first writes two agent folders
with passing tests into DIR (one with a space in its name).
"""
from __future__ import annotations

import argparse
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).parent))
from conftest import FakeEngine, make_folder  # noqa: E402

PASSING = {"billing": {"name": "billing question", "turns": [{"user": "hi"}, {"user": "billing"}],
                       "expect": {"reaches": ["Billing"]}}}


def server(port: int = 0) -> ThreadingHTTPServer:
    fake = FakeEngine()

    class Handler(BaseHTTPRequestHandler):
        def _go(self):
            n = int(self.headers.get("Content-Length") or 0)
            request = httpx.Request(self.command, "http://engine" + self.path, headers=dict(self.headers),
                                    content=self.rfile.read(n) if n else b"")
            reply = fake.handler(request)
            self.send_response(reply.status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(reply.content)

        do_GET = do_POST = _go

        def log_message(self, *args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve_in_background(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    httpd = server(port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=7899)
    p.add_argument("--agents", help="write two agent folders with passing tests here first")
    args = p.parse_args()
    if args.agents:
        root = Path(args.agents)
        for name in ("desk", "front desk"):
            folder = make_folder(root, PASSING, name=name)
            target = root / name
            if folder != target:                        # make_folder writes under <root>/agents/
                target.parent.mkdir(parents=True, exist_ok=True)
                folder.rename(target)
        (root / "agents").rmdir() if (root / "agents").exists() else None
        print(f"agents: {sorted(p.name for p in root.iterdir())}", flush=True)
    server(args.port).serve_forever()
