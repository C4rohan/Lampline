"""Running HTTP tools during a call.

The definitions and their rules live in vk.tools (shared with `vk lint`). Here a
definition becomes a function the model can call, and ToolRunner makes the
request: arguments go into the path and query (URL-encoded) or the JSON body,
secrets fill {{secrets.NAME}}, and the answer goes back to the model.

Safety:
- the scheme and host come only from the definition and secrets, never from
  arguments or session values, so a caller can't steer a request elsewhere;
- redirects are not followed; responses are capped at 1 MB and trimmed before
  the model sees them; timeouts are 1-30 seconds;
- secrets never appear in events (only method, host and path are reported).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import zlib
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import quote

import httpx

from pipecat.adapters.schemas.function_schema import FunctionSchema
from vk import tools as spec

MAX_RESPONSE_BYTES = 1_000_000
MODEL_CHARS = 4000
_WHOLE = re.compile(r"^\{\{\s*([A-Za-z_][\w.]*)\s*\}\}$")


class ToolError(ValueError):
    pass


@dataclass
class Param:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False


@dataclass
class Tool:
    name: str
    description: str
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: Any = None
    params: list[Param] = field(default_factory=list)
    timeout: float = spec.DEFAULT_TIMEOUT
    speech: str = ""
    save: dict[str, str] = field(default_factory=dict)
    mock: Any = None
    has_mock: bool = False
    kind: str = "http"                  # http | connector | mcp
    group: str = ""                     # the agent.yaml tool a step lists to get this function
    connector: str = ""                 # connector action id (vk.connectors)
    pinned: dict = field(default_factory=dict)
    server: dict | None = None          # MCP: {"url", "headers"}
    remote: str = ""                    # MCP: the server's name for the tool
    input_schema: dict | None = None    # MCP: JSON schema of the arguments

    @classmethod
    def parse_entry(cls, raw: dict) -> list["Tool"]:
        """One agent.yaml tool -> the functions the model sees (an MCP server gives several)."""
        kind = spec.kind(raw)
        if kind == "http":
            tool = cls.parse(raw)
            tool.group = tool.name
            return [tool]
        errors = [m for sev, m in spec.problems(raw) if sev == "error"]
        if errors:
            raise ToolError(f"tool {raw.get('name') or '?'}: {errors[0]}")
        common = dict(timeout=float(raw.get("timeout_seconds", spec.DEFAULT_TIMEOUT)),
                      speech=str(raw.get("say_while_running") or ""), save=dict(raw.get("save") or {}), kind=kind,
                      group=raw["name"])
        if kind == "connector":
            from vk.connectors import ACTIONS, model_params
            action = ACTIONS[raw["connector"]]
            params = [Param(p.name, p.type, p.description, p.required) for p in model_params(raw)]
            return [cls(name=raw["name"], description=str(raw.get("description") or action.description).strip(),
                        method="POST", url="", params=params, mock=raw.get("mock"), has_mock="mock" in raw,
                        connector=action.id, pinned=dict(raw.get("with") or {}), **common)]
        server = raw["mcp"]
        mocks = raw.get("mock") if isinstance(raw.get("mock"), dict) else {}
        out = []
        for t in server.get("tools") or []:
            schema = t.get("input_schema") if isinstance(t.get("input_schema"), dict) else {"type": "object", "properties": {}}
            out.append(cls(name=t["name"], description=str(t.get("description") or t["name"])[:1000], method="POST", url="",
                           mock=mocks.get(t["name"]), has_mock=t["name"] in mocks,
                           server={"url": str(server.get("url") or ""), "headers": dict(server.get("headers") or {})},
                           remote=str(t.get("remote") or t["name"]), input_schema=schema, **common))
        return out

    @classmethod
    def parse(cls, raw: dict) -> "Tool":
        errors = [m for sev, m in spec.problems(raw) if sev == "error"]
        if errors:
            name = raw.get("name") if isinstance(raw, dict) else None
            raise ToolError(f"tool {name or '?'}: {errors[0]}")
        params = [Param(p["name"], str(p.get("type") or "string"), str(p.get("description") or ""),
                        bool(p.get("required"))) for p in raw.get("parameters") or []]
        return cls(name=raw["name"], description=str(raw.get("description") or "").strip(),
                   method=str(raw.get("method") or "GET").upper(), url=str(raw.get("url") or "").strip(),
                   headers={str(k): str(v) for k, v in (raw.get("headers") or {}).items()},
                   body=raw.get("body"), params=params,
                   timeout=float(raw.get("timeout_seconds", spec.DEFAULT_TIMEOUT)),
                   speech=str(raw.get("say_while_running") or ""), save=dict(raw.get("save") or {}),
                   mock=raw.get("mock"), has_mock="mock" in raw)

    def schema(self) -> FunctionSchema:
        if self.input_schema is not None:
            return FunctionSchema(name=self.name, description=self.description or self.name,
                                  properties=dict(self.input_schema.get("properties") or {}),
                                  required=[str(r) for r in self.input_schema.get("required") or []])
        props = {p.name: {"type": p.type, "description": p.description or p.name} for p in self.params}
        return FunctionSchema(name=self.name, description=self.description or self.name, properties=props,
                              required=[p.name for p in self.params if p.required])


def parse_all(raw: list | None) -> dict[str, Tool]:
    """Every function the agent's tools give the model, by name (each knows its `group`)."""
    tools: dict[str, Tool] = {}
    groups: set[str] = set()
    for item in raw or []:
        if isinstance(item, dict) and item.get("name") in groups:
            raise ToolError(f"two tools are named {item.get('name')}")
        for tool in Tool.parse_entry(item):
            if tool.name in tools:
                raise ToolError(f"two tools give the model a tool named {tool.name}")
            tools[tool.name] = tool
        if isinstance(item, dict):
            groups.add(str(item.get("name")))
    return tools


# --------------------------------------------------------------------- results

@dataclass
class Result:
    ok: bool
    data: Any = None
    error: str = ""
    status: int | None = None
    saved: dict[str, Any] = field(default_factory=dict)
    mock: bool = False
    seconds: float = 0.0
    request: str = ""          # "GET host/path", no query string, no headers

    def for_model(self) -> dict:
        if not self.ok:
            return {"ok": False, "error": self.error}
        text = json.dumps(self.data, ensure_ascii=False, default=str)
        if len(text) > MODEL_CHARS:
            return {"ok": True, "data_truncated": text[:MODEL_CHARS]}
        return {"ok": True, "data": self.data}

    def for_event(self) -> dict:
        out = {**self.for_model(), "status": self.status, "seconds": self.seconds, "mock": self.mock}
        if self.request:
            out["request"] = self.request
        if self.saved:
            out["saved"] = self.saved
        return out


def dig(data: Any, path: str) -> Any:
    """Follow "slots.0.time" through dicts and lists; None when it isn't there."""
    cur = data
    for part in str(path).strip().split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit() and -len(cur) <= int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return None
    return cur


# --------------------------------------------------------------------- secrets

def env_secrets(env: Mapping[str, str] | None = None) -> dict[str, str]:
    env = os.environ if env is None else env
    return {k[len("VPE_SECRET_"):]: v for k, v in env.items()
            if k.startswith("VPE_SECRET_") and spec.SECRET_NAME.match(k[len("VPE_SECRET_"):]) and v}


# --------------------------------------------------------------------- runner

class ToolRunner:
    """Runs tools for one session. mock=True answers with each tool's mock response
    (tools without a url always do), so tests never touch real services."""

    def __init__(self, secrets: Mapping[str, str] | None = None, *, mock: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None, connectors=None, live: bool = True,
                 channel: dict | None = None):
        self.secrets = dict(secrets or {})
        self.mock = mock
        self.transport = transport
        self.connectors = connectors        # vpengine.connectors.Connectors, for connector tools
        self.live = live                    # False: connector and MCP tools only answer with mocks
        self.channel = dict(channel or {})  # {"kind": "telegram", "to": "123"}: who a reply goes to
        self.sends = 0
        self._mcp: dict[str, Any] = {}

    # -- templates ------------------------------------------------------------

    def _lookup(self, name: str, args: dict, variables: dict, *, secrets_only: bool = False,
                no_secrets: bool = False) -> Any:
        if name.startswith("secrets."):
            if no_secrets:
                raise ToolError("a mock answer can't use secrets")
            key = name[len("secrets."):]
            if key not in self.secrets:
                raise ToolError(f"secret {key} is not set (add it under Integrations → Tool secrets)")
            return self.secrets[key]
        if secrets_only:
            raise ToolError("the address can only use secrets")
        if name in args:
            return args[name]
        return variables.get(name)

    def _text(self, template: str, args: dict, variables: dict, *, encode: bool = False, path: bool = False,
              secrets_only: bool = False, no_secrets: bool = False, mask: bool = False) -> str:
        def repl(m: re.Match) -> str:
            name = m.group(1)
            if mask and name.startswith("secrets."):
                return "***"
            value = self._lookup(name, args, variables, secrets_only=secrets_only, no_secrets=no_secrets)
            value = "" if value is None else (json.dumps(value) if isinstance(value, bool) else str(value))
            if path and value.strip() in (".", ".."):        # would climb to another endpoint on the host
                raise ToolError(f"{{{{{name}}}}} can't be . or .. in the path")
            return quote(value, safe="") if encode else value
        return spec.TEMPLATE.sub(repl, template)

    def _value(self, template: Any, args: dict, variables: dict, *, no_secrets: bool = False) -> Any:
        """Fill a body or mock: "{{x}}" alone keeps x's type; inside text it becomes text.
        Mocks go straight back to the model, so they never get secrets (no_secrets)."""
        if isinstance(template, str):
            m = _WHOLE.match(template.strip())
            if m:
                return self._lookup(m.group(1), args, variables, no_secrets=no_secrets)
            return self._text(template, args, variables, no_secrets=no_secrets)
        if isinstance(template, dict):
            return {self._text(str(k), args, variables, no_secrets=no_secrets): self._value(v, args, variables, no_secrets=no_secrets)
                    for k, v in template.items()}
        if isinstance(template, list):
            return [self._value(v, args, variables, no_secrets=no_secrets) for v in template]
        return template

    def build_url(self, tool: Tool, args: dict, variables: dict) -> str:
        origin, rest = spec.split_url(tool.url)
        base = self._text(origin, args, variables, secrets_only=True).rstrip("/")
        if not base.lower().startswith(("http://", "https://")) or any(c in base for c in " \r\n\t"):
            raise ToolError("the tool's address is not a valid http(s) URL")
        path, sep, query = rest.partition("#")[0].partition("?")
        return (base + self._text(path, args, variables, encode=True, path=True)
                + sep + self._text(query, args, variables, encode=True))

    def describe(self, tool: Tool, args: dict, variables: dict) -> str:
        """"GET host/path" for events: no query string, no headers, secrets as ***."""
        origin, rest = spec.split_url(tool.url)
        try:
            host = "(address from a secret)" if "{{" in origin else (httpx.URL(origin).host or origin)
            path = self._text(rest.split("?")[0].split("#")[0], args, variables, encode=True, mask=True)
        except Exception:  # noqa: BLE001 - only a label
            host, path = "(address)", ""
        return f"{tool.method} {host}{path}"

    def scrub(self, text: str) -> str:
        """Never hand a secret back: a service might echo a key in its answer."""
        for value in self.secrets.values():
            if len(value) >= 6:
                text = text.replace(value, "***")
        return text

    # -- running --------------------------------------------------------------

    def check_args(self, tool: Tool, arguments: dict | None) -> dict:
        if tool.input_schema is not None:
            props = tool.input_schema.get("properties") or {}
            args = {k: v for k, v in (arguments or {}).items() if v is not None and (not props or k in props)}
            missing = [r for r in tool.input_schema.get("required") or [] if r not in args]
            if missing:
                raise ToolError(f"missing required {'argument' if len(missing) == 1 else 'arguments'}: "
                                f"{', '.join(missing)}; ask the caller")
            return args
        args = {p.name: arguments[p.name] for p in tool.params if arguments and p.name in arguments
                and arguments[p.name] not in (None, "")}
        missing = [p.name for p in tool.params if p.required and p.name not in args]
        if missing:
            raise ToolError(f"missing required {'argument' if len(missing) == 1 else 'arguments'}: "
                            f"{', '.join(missing)}; ask the caller")
        return args

    async def run(self, tool: Tool, arguments: dict | None, variables: dict) -> Result:
        started = time.monotonic()
        try:
            args = self.check_args(tool, arguments)
            if tool.kind != "http":
                result = await self._run_other(tool, args, variables)
            elif not tool.url or (self.mock and tool.has_mock):
                result = Result(ok=True, data=self._value(tool.mock, args, variables, no_secrets=True), status=200, mock=True)
            elif self.mock:
                result = Result(ok=False, mock=True, error="this tool has no mock answer; add one, or turn off mock tool answers")
            else:
                result = await self._request(tool, args, variables)
        except ToolError as exc:
            result = Result(ok=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - a broken request is reported to the model, never raised
            result = Result(ok=False, error=f"the request could not be made ({exc.__class__.__name__})")
        if result.ok and tool.save:
            result.saved = {var: v for var, path in tool.save.items() if (v := dig(result.data, path)) is not None}
        result.seconds = round(time.monotonic() - started, 3)
        return result

    # -- connector and MCP tools ----------------------------------------------

    async def _run_other(self, tool: Tool, args: dict, variables: dict) -> Result:
        offline = self.mock or not self.live or (tool.kind == "connector" and self.connectors is None)
        if tool.connector in ("data.lookup", "knowledge.search") and self.live and self.connectors is not None:
            offline = False                       # read-only and on this computer: real answers even with mocks on
        if offline:
            if tool.has_mock:
                return Result(ok=True, data=self._value(tool.mock, args, variables, no_secrets=True), status=200, mock=True)
            why = ("mock tool answers are on" if self.mock else
                   "this conversation can't use live connections" if not self.live else "connections aren't available here")
            return Result(ok=False, mock=True, error=f"{why} and this tool has no mock answer")
        if tool.kind == "connector":
            return await self._run_connector(tool, args, variables)
        return await self._run_mcp(tool, args)

    async def _run_connector(self, tool: Tool, args: dict, variables: dict) -> Result:
        from vk.connectors import ACTIONS, MAX_SENDS
        from .connectors import ConnectorError
        action = ACTIONS[tool.connector]
        merged = {**args, **{k: v for k, v in self._value(tool.pinned, args, variables, no_secrets=True).items()
                            if v not in (None, "")}}
        for p in action.params:
            if p.recipient and p.required and not merged.get(p.name):
                if self.channel.get("kind") == action.service and self.channel.get("to"):
                    merged[p.name] = self.channel["to"]          # reply to whoever wrote in
                else:
                    return Result(ok=False, error=f"no {p.name} to send to: set it in the tool's `with`", request=action.id)
        if action.sends:
            if self.sends >= MAX_SENDS:
                return Result(ok=False, error=f"already sent {MAX_SENDS} messages in this conversation", request=action.id)
            self.sends += 1
        try:
            data = await asyncio.wait_for(self.connectors.run(action.id, merged), max(tool.timeout, 20) + 5)
        except ConnectorError as exc:
            return Result(ok=False, error=str(exc), request=action.id)
        except asyncio.TimeoutError:
            return Result(ok=False, error=f"{action.label.lower()} took too long", request=action.id)
        return Result(ok=True, data=data, status=200, request=action.id)

    async def _run_mcp(self, tool: Tool, args: dict) -> Result:
        from .mcp import McpClient, McpError
        server = tool.server or {}
        url = self._text(server.get("url", ""), {}, {}, secrets_only=True)
        if not url.lower().startswith(("http://", "https://")):
            return Result(ok=False, error="the MCP server's address is not a valid http(s) URL")
        headers = {k: self._text(str(v), {}, {}, secrets_only=True) for k, v in (server.get("headers") or {}).items()}
        key = json.dumps([url, sorted(headers.items())])
        client = self._mcp.get(key)
        if client is None:
            client = self._mcp[key] = McpClient(url, headers, timeout=max(tool.timeout, 10), transport=self.transport)
        host = httpx.URL(url).host
        try:
            ok, data = await client.call_tool(tool.remote or tool.name, args)
        except McpError as exc:
            return Result(ok=False, error=self.scrub(str(exc)), request=f"MCP {host} {tool.remote}")
        data = json.loads(self.scrub(json.dumps(data, ensure_ascii=False, default=str)))
        if not ok:
            return Result(ok=False, error="the tool reported an error", data=data, request=f"MCP {host} {tool.remote}")
        return Result(ok=True, data=data, status=200, request=f"MCP {host} {tool.remote}")

    async def aclose(self) -> None:
        for client in self._mcp.values():
            await client.close()
        self._mcp.clear()

    def _headers(self, tool: Tool, args: dict, variables: dict) -> dict[str, str]:
        headers: dict[str, str] = {}
        for k, v in tool.headers.items():
            if k.lower() == "accept-encoding":          # we pick the compression we can safely cap
                continue
            value = self._text(v, args, variables)
            if "\r" in value or "\n" in value:
                raise ToolError("a header value contains a line break")
            try:
                value.encode("ascii")
            except UnicodeEncodeError:
                raise ToolError(f"header {k} has characters a header can't carry (like é); send that value in "
                                "the query or body instead") from None
            headers[k] = value
        headers["Accept-Encoding"] = "gzip, deflate"
        return headers

    async def _request(self, tool: Tool, args: dict, variables: dict) -> Result:
        url = self.build_url(tool, args, variables)
        request = self.describe(tool, args, variables)
        headers = self._headers(tool, args, variables)
        used = spec.template_names(tool.url)
        kwargs: dict[str, Any] = {"headers": headers}
        if tool.method in ("GET", "DELETE"):
            extra = {k: v for k, v in args.items() if k not in used}
            if extra:
                kwargs["params"] = {k: (json.dumps(v) if isinstance(v, bool) else v) for k, v in extra.items()}
        else:
            kwargs["json"] = self._value(tool.body, args, variables) if tool.body is not None else args

        async def fetch() -> tuple[int, bytes | None, str]:
            async with httpx.AsyncClient(timeout=httpx.Timeout(tool.timeout, connect=min(5.0, tool.timeout)),
                                         follow_redirects=False, transport=self.transport) as http:
                async with http.stream(tool.method, url, **kwargs) as r:
                    enc = r.headers.get("content-encoding", "").strip().lower()
                    if enc not in ("", "identity", "gzip", "x-gzip", "deflate"):
                        return r.status_code, None, f"the service answered with {enc} compression, which isn't supported"
                    if r.is_stream_consumed:     # in-memory responses (test transports) are already read and decoded
                        too_big = len(r.content) > MAX_RESPONSE_BYTES
                        return r.status_code, None if too_big else r.content, "the service's answer was too large" if too_big else ""
                    dec = zlib.decompressobj(47) if enc in ("gzip", "x-gzip", "deflate") else None
                    body = bytearray()
                    async for chunk in r.aiter_raw():
                        if dec is not None:      # inflate no more than the cap allows
                            chunk = dec.decompress(chunk, MAX_RESPONSE_BYTES + 1 - len(body))
                            if dec.unconsumed_tail:
                                return r.status_code, None, "the service's answer was too large"
                        body += chunk
                        if len(body) > MAX_RESPONSE_BYTES:
                            return r.status_code, None, "the service's answer was too large"
                    return r.status_code, bytes(body), ""

        try:   # the whole request, not each read, gets the tool's time limit
            status, raw, problem = await asyncio.wait_for(fetch(), tool.timeout + 0.5)
        except (asyncio.TimeoutError, httpx.TimeoutException):
            return Result(ok=False, error=f"the service did not answer within {tool.timeout:g} seconds", request=request)
        except httpx.HTTPError as exc:
            return Result(ok=False, error=f"cannot reach the service ({exc.__class__.__name__})", request=request)
        except zlib.error:
            return Result(ok=False, error="the service's compressed answer could not be read", request=request)
        if problem:
            return Result(ok=False, error=problem, status=status, request=request)
        text = self.scrub((raw or b"").decode("utf-8", "replace"))
        try:
            data: Any = json.loads(text) if text.strip() else None
        except ValueError:
            data = text[:MODEL_CHARS]
        if 300 <= status < 400:
            return Result(ok=False, error=f"the service redirected ({status}); use its final address",
                          status=status, request=request)
        if status >= 400:
            detail = data if isinstance(data, (dict, list)) else str(data or "")[:300]
            return Result(ok=False, error=f"the service answered {status}", data=detail, status=status, request=request)
        return Result(ok=True, data=data, status=status, request=request)
