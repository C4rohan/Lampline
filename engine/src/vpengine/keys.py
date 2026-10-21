"""Users and roles: named API keys, each with a role.

The owner key (VPE_API_KEY) can do everything. When it's set, the owner can
create named keys for other people on the Team page:

    admin    everything: settings, integrations, keys, deleting call history
    builder  agents, data, knowledge, tests, triggers; not settings or keys
    viewer   read everything (agents, calls, data), and run test calls and chats

Only a SHA-256 hash of each key is stored (keys.json next to the settings file,
mode 600); the key itself is shown once, when it's created. Settings changes
still have to come from this computer (not through a tunnel or proxy),
whatever the role.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROLES = ("admin", "builder", "viewer")
PREFIX = "lpk_"
NAME = re.compile(r"^[\w .@'-]{1,60}$")
MAX_KEYS = 100

# Changes only an admin may make (method, path prefix). "*" is any method.
ADMIN_ONLY = (
    ("*", "/v1/keys"),
    ("PUT", "/v1/settings"), ("POST", "/v1/settings/"),
    ("PUT", "/v1/secrets/"), ("DELETE", "/v1/secrets/"),
    ("POST", "/v1/oauth/"), ("POST", "/v1/connectors/"),
    ("POST", "/v1/builtin-speech/"), ("POST", "/v1/moonshine/"), ("POST", "/v1/parakeet/"), ("POST", "/v1/local-speech/"),
    ("POST", "/v1/embeddings/"),
    ("DELETE", "/v1/calls/"),
)
# What a viewer may do besides reading: try agents out, and search without changing anything.
VIEWER_ACTIONS = (
    ("POST", re.compile(r"^/v1/chat/sessions(/[^/]+/(messages|end))?$")),
    ("POST", re.compile(r"^/v1/calls/webrtc/offer$")),
    ("PATCH", re.compile(r"^/v1/calls/webrtc/offer$")),
    ("POST", re.compile(r"^/v1/agents/[^/]+/check$")),
    ("POST", re.compile(r"^/v1/knowledge/[^/]+/search$")),
    ("POST", re.compile(r"^/v1/data/[^/]+/lookup$")),
)


# What stays on this computer even with VPE_REMOTE_EDITING=on: settings and secrets, downloads,
# sign-ins, anything that fetches an address it's given (trying a tool, listing an MCP server), imports
# (they bring tools), and running triggers (they contact people). Saving an agent from elsewhere may not
# change its tools, triggers or webhooks either (server.py _same_reach).
ALWAYS_LOCAL = re.compile(r"^/v1/(settings|secrets|builtin-speech|moonshine|parakeet|local-speech|embeddings/download|oauth|"
                          r"connectors|tools/mcp|tools/try|agents/import|agents/[^/]+/triggers)")


def always_local(method: str, path: str) -> bool:
    return bool(ALWAYS_LOCAL.match(path)) and method not in ("GET", "HEAD")


def allowed(role: str, method: str, path: str) -> bool:
    if role == "admin":
        return True
    if method in ("GET", "HEAD", "OPTIONS"):
        return not path.startswith("/v1/keys")
    if any((m == "*" or m == method) and (path == p or path.startswith(p)) for m, p in ADMIN_ONLY):
        return False
    if role == "builder":
        return True
    return any(m == method and rx.match(path) for m, rx in VIEWER_ACTIONS)


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class KeyStore:
    """Named keys in a small JSON file (or only in memory, for tests)."""

    def __init__(self, path: Path | None):
        self.path = path
        self._lock = threading.Lock()
        self._memory: list[dict] = []
        self._used: dict[str, float] = {}

    def _read(self) -> list[dict]:
        if self.path is None:
            return [dict(k) for k in self._memory]
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (OSError, ValueError):
            return []
        return [k for k in data.get("keys", []) if isinstance(k, dict)] if isinstance(data, dict) else []

    def _write(self, keys: list[dict]) -> None:
        if self.path is None:
            self._memory = [dict(k) for k in keys]
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".keys-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump({"keys": keys}, f, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def list(self) -> list[dict]:
        return [{"id": k["id"], "name": k["name"], "role": k["role"], "hint": k.get("hint", ""),
                 "created": k.get("created"), "last_used": self._used.get(k["id"], k.get("last_used"))}
                for k in self._read()]

    def create(self, name: str, role: str) -> tuple[dict, str]:
        name = (name or "").strip()
        if not NAME.match(name):
            raise ValueError("a name is 1 to 60 letters, digits, spaces and . @ ' -")
        if role not in ROLES:
            raise ValueError(f"the role is one of {', '.join(ROLES)}")
        with self._lock:
            keys = self._read()
            if len(keys) >= MAX_KEYS:
                raise ValueError(f"at most {MAX_KEYS} keys")
            if any(k["name"].lower() == name.lower() for k in keys):
                raise ValueError(f"there's already a key named {name}")
            key = PREFIX + secrets.token_urlsafe(24)
            rec = {"id": secrets.token_hex(6), "name": name, "role": role, "hash": _hash(key),
                   "hint": key[-4:], "created": time.time()}
            self._write([*keys, rec])
        return {k: v for k, v in rec.items() if k != "hash"}, key

    def revoke(self, kid: str) -> bool:
        with self._lock:
            keys = self._read()
            kept = [k for k in keys if k["id"] != kid]
            if len(kept) == len(keys):
                return False
            self._write(kept)
        return True

    def match(self, given: str) -> dict | None:
        """The key record for this key, or None. Compares hashes in constant time."""
        if not given.startswith(PREFIX):
            return None
        h = _hash(given)
        found = None
        for k in self._read():
            if hmac.compare_digest(k.get("hash", ""), h):
                found = k
        if found:
            self._used[found["id"]] = time.time()
        return found

    def any(self) -> bool:
        return bool(self._read())


def describe(rec: dict[str, Any] | None) -> dict:
    return {"name": rec["name"], "role": rec["role"]} if rec else {"name": "owner", "role": "admin"}
