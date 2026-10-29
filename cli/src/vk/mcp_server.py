"""`vk mcp`: Lampline's agent folders and tests as an MCP server (stdio), for Claude Desktop, Cursor and other
MCP clients that don't run a terminal.

Tools:
    list_agents        the agent folders, with their step and test counts
    lint_agent         vk lint, as findings
    run_tests          vk test, the same JSON as `vk test --format json`
    read_prompt        one step's prompt
    write_prompt       replace one step's prompt (refused if it drops a {{variable}} or looks like real personal data)
    list_failed_calls  real calls whose call checks failed: when, and which checks failed and why

What it can't do, on purpose: change steps, routes, tools, triggers or webhooks (they can use secrets or contact
people), return call transcripts or callers' numbers (real calls can hold real people's details, and MCP results go
to the model), or read anything outside the agents folder.

The protocol is small enough to speak directly (JSON-RPC 2.0, one message per line on stdin and stdout), so vk
keeps its two dependencies. Nothing but protocol messages is written to stdout.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

import httpx

from . import __version__, agentfs, phi
from .lint import lint

SUPPORTED = ("2025-06-18", "2025-03-26", "2024-11-05")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
VARIABLE = re.compile(r"\{\{\s*([A-Za-z_]\w*)")
MAX_PROMPT = 20_000


class ToolError(Exception):
    """Shown to the model as the tool's result, with isError."""


def _schema(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


AGENT = {"type": "string", "description": "the agent's folder name, as list_agents shows it"}
STEP = {"type": "string", "description": "the step's name or id"}

TOOLS = [
    {"name": "list_agents", "description": "List the agent folders: name, number of steps, number of tests.",
     "inputSchema": _schema({}, [])},
    {"name": "lint_agent", "description": "Check an agent folder offline (vk lint). Errors block testing.",
     "inputSchema": _schema({"agent": AGENT}, ["agent"])},
    {"name": "run_tests",
     "description": "Run the agent's tests/*.yaml on the Lampline engine (vk test). Returns each test's result: "
                    "passed, failures, the steps the call went through, the transcript, and with repeat > 1 how many "
                    "runs passed (flaky: passed only some of the time). Needs the engine running.",
     "inputSchema": _schema({"agent": AGENT,
                             "match": {"type": "string", "description": "only tests whose name or file contains this"},
                             "repeat": {"type": "integer", "minimum": 1, "maximum": 10,
                                        "description": "run every test this many times (default 1)"}}, ["agent"])},
    {"name": "read_prompt", "description": "Read one step's prompt, and list every step's name and id.",
     "inputSchema": _schema({"agent": AGENT, "step": STEP}, ["agent", "step"])},
    {"name": "write_prompt",
     "description": "Replace one step's prompt in the agent folder. Keep every {{variable}} the prompt uses; use "
                    "made-up details only. Run run_tests afterwards to check the change.",
     "inputSchema": _schema({"agent": AGENT, "step": STEP,
                             "prompt": {"type": "string", "description": "the new prompt, in full"}},
                            ["agent", "step", "prompt"])},
    {"name": "list_failed_calls",
     "description": "Real calls to this agent whose call checks failed, newest first: when, the grader's summary, "
                    "and each failed check with its reason. No transcripts or callers' numbers. Needs the engine, "
                    "on this computer.",
     "inputSchema": _schema({"agent": AGENT,
                             "limit": {"type": "integer", "minimum": 1, "maximum": 50,
                                       "description": "how many calls (default 10)"}}, ["agent"])},
]


class Server:
    def __init__(self, root: str | Path = "agents", *, url: str | None = None, key: str | None = None,
                 transport: httpx.BaseTransport | None = None):
        self.root = Path(root)
        self.url = (url or os.environ.get("VK_ENGINE_URL") or "http://localhost:7860").rstrip("/")
        self.key = key if key is not None else os.environ.get("VK_ENGINE_KEY") or None
        self.transport = transport
        self.handlers: dict[str, Callable[[dict], Any]] = {
            "list_agents": self.list_agents, "lint_agent": self.lint_agent, "run_tests": self.run_tests,
            "read_prompt": self.read_prompt, "write_prompt": self.write_prompt,
            "list_failed_calls": self.list_failed_calls,
        }

    # ------------------------------------------------------------------ protocol
    def handle(self, msg: Any) -> dict | None:
        """One JSON-RPC message in; the response out (None for a notification)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
            return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "invalid request")
        mid, method, params = msg.get("id"), msg["method"], msg.get("params") or {}
        if "id" not in msg:
            return None                                     # notifications/initialized, cancelled, ...
        if method == "initialize":
            asked = params.get("protocolVersion")
            return _result(mid, {"protocolVersion": asked if asked in SUPPORTED else SUPPORTED[0],
                                 "capabilities": {"tools": {}},
                                 "serverInfo": {"name": "lampline-vk", "version": __version__},
                                 "instructions": "Lampline phone agents as folders: list them, lint them, run their "
                                                 "tests, and change one step's prompt at a time. Synthetic data only."})
        if method == "ping":
            return _result(mid, {})
        if method == "tools/list":
            return _result(mid, {"tools": TOOLS})
        if method == "tools/call":
            name, args = params.get("name"), params.get("arguments") or {}
            if name not in self.handlers:
                return _error(mid, -32602, f"unknown tool: {name}")
            try:
                out = self.handlers[name](args if isinstance(args, dict) else {})
                return _result(mid, {"content": [{"type": "text", "text": json.dumps(out, indent=2, ensure_ascii=False)}],
                                     "isError": False})
            except ToolError as exc:
                return _result(mid, {"content": [{"type": "text", "text": str(exc)}], "isError": True})
        return _error(mid, -32601, f"method not found: {method}")

    def serve(self, stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                reply: dict | None = _error(None, -32700, "parse error")
            else:
                # anything the tools print (vk's own output) goes to stderr, never into the protocol stream
                with contextlib.redirect_stdout(sys.stderr):
                    reply = self.handle(msg)
            if reply is not None:
                stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
                stdout.flush()

    # ------------------------------------------------------------------ helpers
    def _folder(self, args: dict) -> Path:
        name = str(args.get("agent") or "")
        if not NAME.match(name):
            raise ToolError("agent is a folder name, like front-desk (see list_agents)")
        folder = (self.root / name)
        if not folder.resolve().is_relative_to(self.root.resolve()) or not (folder / agentfs.AGENT_FILE).is_file():
            raise ToolError(f"no agent folder named {name!r} in {self.root}/ (see list_agents)")
        return folder

    def _load(self, folder: Path) -> agentfs.Agent:
        try:
            return agentfs.load(folder)
        except agentfs.FolderError as exc:
            raise ToolError(str(exc)) from exc

    @staticmethod
    def _node(agent: agentfs.Agent, step: str) -> dict:
        nodes = [n for n in agent.definition.get("nodes") or [] if isinstance(n, dict)]
        found = [n for n in nodes if str(n.get("id")) == step] or \
                [n for n in nodes if str((n.get("data") or {}).get("name", "")).lower() == step.strip().lower()]
        if len(found) != 1:
            names = ", ".join(f"{(n.get('data') or {}).get('name')} ({n.get('id')})" for n in nodes)
            raise ToolError(f"no single step named {step!r}; steps: {names}")
        if not isinstance(found[0].get("data"), dict):
            raise ToolError(f"step {step!r} has no data")
        return found[0]

    # ------------------------------------------------------------------ tools
    def list_agents(self, args: dict) -> dict:
        out = []
        if self.root.is_dir():
            for folder in sorted(p for p in self.root.iterdir() if (p / agentfs.AGENT_FILE).is_file()):
                try:
                    agent = agentfs.load(folder)
                except agentfs.FolderError as exc:
                    out.append({"agent": folder.name, "error": str(exc)})
                    continue
                out.append({"agent": folder.name, "name": agent.name,
                            "steps": len(agent.definition.get("nodes") or []),
                            "tests": len(list((folder / "tests").glob("*.yaml")))})
        return {"folder": str(self.root), "agents": out}

    def lint_agent(self, args: dict) -> dict:
        folder = self._folder(args)
        findings = lint(self._load(folder), folder)
        return {"errors": sum(f.severity == "error" for f in findings), "findings": [f.as_dict() for f in findings]}

    def run_tests(self, args: dict) -> Any:
        from . import cli
        folder = self._folder(args)
        repeat = args.get("repeat") or 1
        if not isinstance(repeat, int) or not 1 <= repeat <= 10:
            raise ToolError("repeat is 1 to 10")
        argv = ["test", str(folder), "--format", "json", "--url", self.url, "--repeat", str(repeat)]
        if args.get("match"):
            argv += ["-k", str(args["match"])[:200]]
        out, err = io.StringIO(), io.StringIO()
        env_key = os.environ.get("VK_ENGINE_KEY")
        if self.key is not None:
            os.environ["VK_ENGINE_KEY"] = self.key
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(argv, transport=self.transport)
        except SystemExit as exc:                       # argparse refuses an argument: report it, don't quit
            raise ToolError((err.getvalue() or "vk test refused its arguments").strip()[-2000:]) from exc
        finally:
            if env_key is None:
                os.environ.pop("VK_ENGINE_KEY", None)
            else:
                os.environ["VK_ENGINE_KEY"] = env_key
        text = out.getvalue().strip()
        try:
            results = json.loads(text) if text.startswith(("[", "{")) else None
        except ValueError:
            results = None
        if results is None:
            raise ToolError((err.getvalue() or text or "vk test failed").strip()[-2000:])
        return {"all_passed": code == 0, "results": results}

    def read_prompt(self, args: dict) -> dict:
        agent = self._load(self._folder(args))
        node = self._node(agent, str(args.get("step") or ""))
        steps = [{"id": n.get("id"), "name": (n.get("data") or {}).get("name"), "type": n.get("type")}
                 for n in agent.definition.get("nodes") or [] if isinstance(n, dict)]
        return {"step": node["data"].get("name"), "id": node.get("id"), "prompt": node["data"].get("prompt") or "",
                "steps": steps}

    def write_prompt(self, args: dict) -> dict:
        folder = self._folder(args)
        agent = self._load(folder)
        node = self._node(agent, str(args.get("step") or ""))
        new = str(args.get("prompt") or "").strip()
        if not new:
            raise ToolError("the prompt is empty")
        if len(new) > MAX_PROMPT:
            raise ToolError(f"the prompt is longer than {MAX_PROMPT} characters")
        old = str(node["data"].get("prompt") or "")
        dropped = sorted(set(VARIABLE.findall(old)) - set(VARIABLE.findall(new)))
        if dropped:
            raise ToolError("the new prompt drops values the step uses: " + ", ".join("{{" + d + "}}" for d in dropped))
        hits = phi.scan(new)
        if hits:
            raise ToolError("not saved: this looks like it contains real personal details ("
                            + ", ".join(sorted({h.label for h in hits})) + "). Use made-up values.")
        node["data"]["prompt"] = new
        try:
            agentfs.write(agent, folder)
        except agentfs.FolderError as exc:
            raise ToolError(str(exc)) from exc
        return {"saved": True, "step": node["data"].get("name"), "id": node.get("id"),
                "next": "run run_tests to check the change"}

    def list_failed_calls(self, args: dict) -> dict:
        name = self._folder(args).name
        limit = args.get("limit") or 10
        if not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ToolError("limit is 1 to 50")
        headers = {"X-API-Key": self.key} if self.key else {}
        try:
            with httpx.Client(base_url=f"{self.url}/v1", headers=headers, timeout=30, transport=self.transport) as c:
                r = c.get("/calls", params={"agent": name, "grade": "failed", "limit": limit})
        except httpx.HTTPError as exc:
            raise ToolError(f"cannot reach the engine at {self.url} ({exc.__class__.__name__}); is it running?") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail")
            except ValueError:
                detail = r.text[:200]
            raise ToolError(f"the engine said {r.status_code}: {detail}")
        calls = []
        for call in (r.json().get("calls") or [])[:limit]:
            res = call.get("grade_results") or {}
            calls.append({"id": call.get("id"), "started": call.get("started"), "channel": call.get("channel"),
                          "summary": str(res.get("summary") or "")[:500],
                          "failed_checks": [{"check": c.get("criterion"), "why": c.get("reason")}
                                            for c in res.get("criteria") or [] if not c.get("passed")]})
        return {"agent": name, "calls": calls}


def _result(mid: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}
