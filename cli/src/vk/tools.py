"""HTTP tools an agent's steps can call (look up a slot, book it, check a balance).

Tools live in agent.yaml under `tools:`. A step lists the tools it may use in `data.tools`. Each tool becomes a
function the model can call; the engine makes the HTTP request and hands the
answer back to the model.

    tools:
    - name: find_open_slots
      description: Look up open appointment times for a day.
      method: GET
      url: https://scheduling.example.com/v1/slots
      headers: {Authorization: "Bearer {{secrets.SCHEDULING_KEY}}"}
      parameters:
      - {name: day, type: string, description: the day the caller wants, required: true}
      say_while_running: One moment while I check.
      save: {first_open_time: slots.0.time}
      mock: {slots: [{time: "9:30 AM"}, {time: "2:15 PM"}]}

Templates: {{name}} is an argument the model filled in, else a session variable;
{{secrets.NAME}} is a secret saved in Integrations (or VPE_SECRET_NAME), so keys
never sit in the folder. The address (scheme and host) may only use secrets,
never values the caller could influence.
"""

from __future__ import annotations

import re
from typing import Any

NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
VAR_NAME = re.compile(r"^[A-Za-z_]\w{0,63}$")
SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
PARAM_TYPES = ("string", "number", "integer", "boolean")
MAX_TIMEOUT = 30
DEFAULT_TIMEOUT = 8
TEMPLATE = re.compile(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}")
KEYS = ("name", "description", "method", "url", "headers", "body", "parameters", "timeout_seconds",
        "say_while_running", "save", "mock", "connector", "with", "mcp")
MCP_KEYS = ("url", "headers", "tools")
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "host.docker.internal")
_SECRET_HEADER = re.compile(r"auth|key|token|secret|cookie|password|signature", re.I)


def split_url(url: str) -> tuple[str, str]:
    """("https://host:port", "/path?query") without touching templates. An address from a
    secret ("{{secrets.BASE}}/slots") ends at the first / ? or # after its first }}."""
    if url.startswith("{{") and "}}" in url:
        end = url.index("}}") + 2
        cut = min([i for i in (url.find("/", end), url.find("?", end), url.find("#", end)) if i >= 0] or [len(url)])
        return url[:cut], url[cut:]
    if "://" not in url:
        return "", url
    scheme, rest = url.split("://", 1)
    cut = min([i for i in (rest.find("/"), rest.find("?"), rest.find("#")) if i >= 0] or [len(rest)])
    return f"{scheme}://{rest[:cut]}", rest[cut:]


def template_names(value: Any) -> set[str]:
    """Every {{name}} used anywhere in a string, list or mapping."""
    if isinstance(value, str):
        return {m.group(1) for m in TEMPLATE.finditer(value)}
    if isinstance(value, dict):
        out: set[str] = set()
        for k, v in value.items():
            out |= template_names(k) | template_names(v)
        return out
    if isinstance(value, list):
        out = set()
        for v in value:
            out |= template_names(v)
        return out
    return set()


def kind(tool: dict) -> str:
    """http (a request you describe), connector (an Integrations action) or mcp (a server's tools)."""
    if isinstance(tool, dict) and "connector" in tool:
        return "connector"
    if isinstance(tool, dict) and "mcp" in tool:
        return "mcp"
    return "http"


def function_names(tool: dict) -> list[str]:
    """The function names the model sees: one per tool, or one per listed tool of an MCP server."""
    if kind(tool) == "mcp":
        listed = ((tool.get("mcp") or {}).get("tools") or []) if isinstance(tool.get("mcp"), dict) else []
        return [str(t.get("name")) for t in listed if isinstance(t, dict) and t.get("name")]
    return [str(tool.get("name"))]


def _address(url: Any, err, warn) -> None:
    if not isinstance(url, str):
        err("url must be text")
        return
    origin, _ = split_url(url)
    bad = {n for n in template_names(origin) if not n.startswith("secrets.")}
    from_secret = origin.startswith("{{")
    if not origin:
        err("url must start with https:// (or http:// for this computer)")
    scheme = origin.split("://", 1)[0].lower() if origin and not from_secret else ""
    if origin and not from_secret and scheme not in ("http", "https"):
        err("url must use https:// or http://")
    if bad:
        err("the address (scheme and host) can only use {{secrets.NAME}}, never caller or session "
            f"values ({', '.join(sorted(bad))})")
    host = origin.split("://", 1)[-1].split(":")[0].lower() if origin else ""
    if scheme == "http" and host and "{{" not in host and host not in _LOCAL_HOSTS:
        warn("uses plain http://; use https:// for anything outside this computer")


def _headers(headers: Any, err) -> None:
    if not isinstance(headers, dict):
        err("headers must be a mapping of header name to value")
        return
    for k, v in headers.items():
        if not isinstance(v, (str, int, float)):
            err(f"header {k} must be text")
        elif _SECRET_HEADER.search(str(k)) and "{{" not in str(v) and len(str(v).strip()) >= 8:
            err(f"header {k} looks like a pasted key; save it as a tool secret in Integrations and "
                "write {{secrets.NAME}} instead")


def _common(tool: dict, err, warn) -> None:
    unknown = sorted(set(tool) - set(KEYS))
    if unknown:
        err(f"unknown key(s) {', '.join(unknown)} (known: {', '.join(KEYS)})")
    name = tool.get("name")
    if not isinstance(name, str) or not NAME.match(name):
        err("name must start with a letter and use only letters, digits and underscores (up to 64)")
    if not str(tool.get("description") or "").strip() and kind(tool) != "connector":
        warn("no description: the model uses it to decide when to call the tool")
    t = tool.get("timeout_seconds", DEFAULT_TIMEOUT)
    if not isinstance(t, (int, float)) or isinstance(t, bool) or not 1 <= t <= MAX_TIMEOUT:
        err(f"timeout_seconds must be between 1 and {MAX_TIMEOUT}")
    save = tool.get("save") or {}
    if not isinstance(save, dict):
        err("save must map a variable name to a path in the response, like first_time: slots.0.time")
    else:
        for var, path in save.items():
            if not isinstance(var, str) or not VAR_NAME.match(var):
                err(f"save: {var!r} is not a valid variable name")
            if not isinstance(path, str) or not path.strip():
                err(f"save: {var} needs a path in the response, like slots.0.time")
    if any(n.startswith("secrets.") for n in template_names(tool.get("mock"))):
        err("the mock answer can't use {{secrets.NAME}}: it goes straight back to the model")


def _mcp(tool: dict, err, warn) -> None:
    server = tool.get("mcp")
    if not isinstance(server, dict):
        err("mcp must be a mapping with the server's url (and optional headers and tools)")
        return
    unknown = sorted(set(server) - set(MCP_KEYS))
    if unknown:
        err(f"mcp: unknown key(s) {', '.join(unknown)} (known: {', '.join(MCP_KEYS)})")
    for k in ("method", "url", "parameters", "body", "headers", "connector", "with"):
        if k in tool:
            err(f"an MCP tool takes its address and headers under mcp:, not {k}")
    _address(server.get("url") or "", err, warn)
    _headers(server.get("headers") or {}, err)
    for n in template_names(server.get("headers")) | template_names(server.get("url")):
        if not n.startswith("secrets."):
            err(f"mcp: {{{{{n}}}}} can't be used here; only {{{{secrets.NAME}}}}")
        elif not SECRET_NAME.match(n[len("secrets."):]):
            err(f"{{{{{n}}}}}: secret names use capital letters, digits and underscores")
    listed = server.get("tools") or []
    if not isinstance(listed, list):
        err("mcp.tools must be a list (press Refresh in the builder to fill it from the server)")
        return
    if not listed:
        warn("no tools listed yet: press Refresh on this tool in the builder to load them from the server")
    seen: set[str] = set()
    for t in listed:
        if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not NAME.match(t["name"]):
            err("each MCP tool needs a name made of letters, digits and underscores")
            continue
        if t["name"] in seen:
            err(f"mcp tool {t['name']} is listed twice")
        seen.add(t["name"])
        schema = t.get("input_schema", {})
        if not isinstance(schema, dict) or schema.get("type", "object") != "object":
            err(f"mcp tool {t['name']}: input_schema must be a JSON schema object")
    mock = tool.get("mock")
    if mock is not None and not isinstance(mock, dict):
        err("an MCP tool's mock maps each tool name to its example answer")
    elif isinstance(mock, dict):
        extra = sorted(set(mock) - seen)
        if extra and listed:
            warn(f"mock has answers for tools the server list doesn't have: {', '.join(extra)}")


def problems(tool: Any) -> list[tuple[str, str]]:
    """(severity, message) for one tool definition: "error" stops the agent loading."""
    if not isinstance(tool, dict):
        return [("error", "each tool must be a mapping with a name, description and url")]
    out: list[tuple[str, str]] = []
    err = lambda m: out.append(("error", m))     # noqa: E731
    warn = lambda m: out.append(("warning", m))  # noqa: E731
    _common(tool, err, warn)
    if kind(tool) == "connector":
        from .connectors import problems as connector_problems
        if "mcp" in tool:
            err("a tool is either a connector or an MCP server, not both")
        out.extend(connector_problems(tool))
        return out
    if kind(tool) == "mcp":
        _mcp(tool, err, warn)
        return out
    if "with" in tool:
        err("with only applies to connector tools")
    method = str(tool.get("method") or "GET").upper()
    if method not in METHODS:
        err(f"method must be one of {', '.join(METHODS)}")
    url = tool.get("url") or ""
    if isinstance(url, str) and not url.strip():
        if "mock" not in tool:
            err("needs a url, or a mock response to use while you build it")
    else:
        _address(url, err, warn)
    _headers(tool.get("headers") or {}, err)

    params = tool.get("parameters") or []
    seen: set[str] = set()
    if not isinstance(params, list):
        err("parameters must be a list")
        params = []
    for p in params:
        if not isinstance(p, dict) or not isinstance(p.get("name"), str) or not VAR_NAME.match(p["name"]):
            err("each parameter needs a name made of letters, digits and underscores")
            continue
        if p["name"] in seen:
            err(f"parameter {p['name']} is listed twice")
        seen.add(p["name"])
        if str(p.get("type") or "string") not in PARAM_TYPES:
            err(f"parameter {p['name']} type must be one of {', '.join(PARAM_TYPES)}")

    for n in template_names(tool.get("headers")) | template_names(url) | template_names(tool.get("body")):
        if n.startswith("secrets.") and not SECRET_NAME.match(n[len("secrets."):]):
            err(f"{{{{{n}}}}}: secret names use capital letters, digits and underscores")
    return out
