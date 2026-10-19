"""A small client for MCP servers over Streamable HTTP (tools only).

    initialize -> notifications/initialized -> tools/list | tools/call

Replies come back as JSON or as a server-sent event stream; both are read with
the same limits as HTTP tools (no redirects, 1 MB, a time limit per request).
The server's address and headers come only from the agent's definition and
saved secrets. stdio servers (programs on this computer) aren't supported:
an agent folder never starts programs.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import re
from typing import Any

import httpx

from . import __version__

PROTOCOL_VERSION = "2025-06-18"     # initialize handshake and sessions (what CX Agent Studio uses)
STATELESS_VERSION = "2026-07-28"    # no handshake: every request carries its version
MAX_BYTES = 1_000_000


class McpError(RuntimeError):
    pass


class McpClient:
    def __init__(self, url: str, headers: dict[str, str] | None = None, *, timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.url = url
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.transport = transport
        self.session_id: str | None = None
        self.protocol = PROTOCOL_VERSION
        self._ids = itertools.count(1)
        self._ready = False
        self._stateless = False
        self._lock = asyncio.Lock()

    # -- transport ------------------------------------------------------------

    def _request_headers(self, message: dict | None = None) -> dict[str, str]:
        h = {**self.headers, "Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        if self._ready:
            h["MCP-Protocol-Version"] = self.protocol
        if self._stateless and message:
            h["Mcp-Method"] = str(message.get("method", ""))
            name = (message.get("params") or {}).get("name")
            if message.get("method") == "tools/call" and name:
                h["Mcp-Name"] = str(name)
        return h

    async def _post(self, message: dict) -> dict | None:
        async def go() -> dict | None:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=min(5.0, self.timeout)),
                                         follow_redirects=False, transport=self.transport) as http:
                async with http.stream("POST", self.url, headers=self._request_headers(message), json=message) as r:
                    if r.status_code == 404 and self.session_id:
                        raise _Expired()
                    if 300 <= r.status_code < 400:
                        raise McpError(f"the server redirected ({r.status_code}); use its final address")
                    if r.status_code in (401, 403):
                        raise McpError("the server refused the credentials (check the secret in its headers)")
                    if r.status_code >= 400:
                        raise McpError(f"the server answered {r.status_code}")
                    sid = r.headers.get("mcp-session-id")
                    if sid:
                        self.session_id = sid
                    if "id" not in message or r.status_code == 202:
                        return None
                    kind = r.headers.get("content-type", "").split(";")[0].strip().lower()
                    body = bytearray()
                    if kind == "text/event-stream":
                        return await _from_events(r, message["id"])
                    async for chunk in r.aiter_bytes():
                        body += chunk
                        if len(body) > MAX_BYTES:
                            raise McpError("the server's answer was too large")
                    data = json.loads(bytes(body) or b"null")
                    if isinstance(data, list):        # a batch: take ours
                        data = next((m for m in data if isinstance(m, dict) and m.get("id") == message["id"]), None)
                    return data

        try:
            return await asyncio.wait_for(go(), self.timeout + 1)
        except asyncio.TimeoutError as exc:
            raise McpError(f"the server did not answer within {self.timeout:g} seconds") from exc
        except httpx.HTTPError as exc:
            raise McpError(f"cannot reach the server ({exc.__class__.__name__})") from exc
        except ValueError as exc:
            raise McpError("the server's answer wasn't valid JSON") from exc

    async def _call(self, method: str, params: dict | None = None) -> Any:
        if self._stateless:                     # 2026-07-28: version and client info ride on every request
            params = {**(params or {}), "_meta": {"io.modelcontextprotocol/protocolVersion": self.protocol,
                                                   "io.modelcontextprotocol/clientInfo": {"name": "vpengine", "version": __version__}}}
        msg = {"jsonrpc": "2.0", "id": next(self._ids), "method": method, **({"params": params} if params is not None else {})}
        reply = await self._post(msg)
        if not isinstance(reply, dict):
            raise McpError("the server sent no answer")
        if reply.get("error"):
            err = reply["error"]
            raise McpError(str(err.get("message") if isinstance(err, dict) else err)[:300])
        return reply.get("result")

    # -- protocol ---------------------------------------------------------------

    async def connect(self) -> dict:
        async with self._lock:
            if self._ready:
                return {}
            try:
                result = await self._call("initialize", {
                    "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": {"name": "vpengine", "version": __version__}}) or {}
            except McpError as exc:
                # Servers on the 2026-07-28 spec have no handshake: discover, then send the version each time.
                if "method" not in str(exc).lower() and "initialize" not in str(exc).lower():
                    raise
                self._stateless, self.protocol, self._ready = True, STATELESS_VERSION, True
                try:
                    return await self._call("server/discover", {}) or {}
                except McpError:
                    return {}
            self.protocol = str(result.get("protocolVersion") or PROTOCOL_VERSION)
            self._ready = True
            await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
            return result

    async def _with_session(self, method: str, params: dict | None = None) -> Any:
        await self.connect()
        try:
            return await self._call(method, params)
        except _Expired:                          # the server forgot our session: start a new one
            self.session_id, self._ready = None, False
            await self.connect()
            return await self._call(method, params)

    async def list_tools(self) -> list[dict]:
        tools, cursor = [], None
        for _ in range(20):
            result = await self._with_session("tools/list", {"cursor": cursor} if cursor else {}) or {}
            tools += [t for t in result.get("tools") or [] if isinstance(t, dict)]
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    async def call_tool(self, name: str, arguments: dict) -> tuple[bool, Any]:
        """(ok, answer for the model). Text parts are joined; JSON text is parsed."""
        result = await self._with_session("tools/call", {"name": name, "arguments": arguments}) or {}
        if result.get("structuredContent") is not None:
            data: Any = result["structuredContent"]
        else:
            texts = [str(c.get("text", "")) for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
            other = [c.get("type") for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") != "text"]
            joined = "\n".join(texts)
            try:
                data = json.loads(joined) if joined.strip() else None
            except ValueError:
                data = joined
            if other and data is None:
                data = f"(the server returned {', '.join(map(str, other))} content, which isn't read out)"
        return not result.get("isError"), data

    async def close(self) -> None:
        if not self.session_id:
            return
        try:
            async with httpx.AsyncClient(timeout=3, transport=self.transport, follow_redirects=False) as http:
                await http.delete(self.url, headers=self._request_headers())
        except httpx.HTTPError:
            pass
        self.session_id, self._ready = None, False


class _Expired(Exception):
    pass


async def _from_events(r: httpx.Response, want: int) -> dict | None:
    """Read server-sent events until the reply with our id arrives."""
    size, data_lines = 0, []
    async for line in r.aiter_lines():
        size += len(line)
        if size > MAX_BYTES:
            raise McpError("the server's answer was too large")
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
        elif line == "" and data_lines:
            try:
                msg = json.loads("\n".join(data_lines))
            except ValueError:
                msg = None
            data_lines = []
            for m in msg if isinstance(msg, list) else [msg]:
                if isinstance(m, dict) and m.get("id") == want and ("result" in m or "error" in m):
                    return m
    if data_lines:
        msg = json.loads("\n".join(data_lines))
        if isinstance(msg, dict) and msg.get("id") == want:
            return msg
    return None


def to_definitions(tools: list[dict]) -> list[dict]:
    """tools/list -> the `mcp.tools` list saved in agent.yaml."""
    out, seen = [], set()
    for t in tools:
        remote = str(t.get("name") or "")
        name = re.sub(r"[^A-Za-z0-9_]", "_", remote)[:64] or "tool"
        if not name[0].isalpha():
            name = ("t_" + name)[:64]
        while name in seen:
            name = (name[:60] + "_x")
        seen.add(name)
        schema = t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {"type": "object", "properties": {}}
        item = {"name": name, "description": str(t.get("description") or "")[:1000], "input_schema": schema}
        if remote != name:
            item["remote"] = remote          # the server's own name, used when calling it
        out.append(item)
    return out
