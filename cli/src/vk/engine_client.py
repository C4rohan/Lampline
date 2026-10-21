"""Client for the engine's text chat (vpengine), used by `vk test`.

Tests run on the folder as it is on disk: the agent definition is sent with
each chat, so nothing has to be saved or deployed first.
"""

from __future__ import annotations

from typing import Any

import httpx

from .agentfs import Agent


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status}: {message}" if status else message)
        self.status = status
        self.message = message


class EngineClient:
    def __init__(self, url: str, agent: Agent, api_key: str | None = None, *, timeout: float = 120.0,
                 transport: httpx.BaseTransport | None = None, live_tools: bool = False):
        headers = {"Accept": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key
        self.agent = agent
        self.live_tools = live_tools        # False: tools answer with their mock responses
        self._http = httpx.Client(base_url=f"{url.rstrip('/')}/v1", headers=headers, timeout=timeout,
                                  transport=transport)
        self._turns: dict[str, int] = {}

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "EngineClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            resp = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(0, f"cannot reach the engine ({exc})") from exc
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = resp.text
            raise ApiError(resp.status_code, str(detail))
        return resp.json()

    def health(self) -> dict:
        return self._request("GET", "/health")

    def _shape(self, reply: dict, user_text: str | None) -> dict:
        """Engine reply -> the transcript shape scenario.run reads."""
        sid = reply["session_id"]
        n = self._turns.get(sid, 0) + 1
        self._turns[sid] = n
        events = [{"type": e.get("type"), "payload": e} for e in reply.get("events") or []
                  if e.get("type") != "user_speech"]
        turn = {"id": f"{sid}-{n}", "status": "completed", "events": events}
        if user_text:
            turn["user_message"] = {"text": user_text}
        return {"run_id": sid, "is_completed": bool(reply.get("ended")),
                "session_data": {"turns": [turn], "status": "completed" if reply.get("ended") else "active"},
                "engine": {k: reply.get(k) for k in ("steps", "variables", "current_step")}}

    def start_chat(self, *, initial_context: dict | None = None,
                   name: str | None = None, timeout: float = 120.0) -> dict:
        body = {"name": self.agent.name, "definition": self.agent.definition,
                "context_variables": self.agent.context_variables or {}, "context": initial_context or {},
                "tools": self.agent.tools or [], "mock_tools": not self.live_tools}
        return self._shape(self._request("POST", "/chat/sessions", json=body, timeout=timeout), None)

    def send_chat(self, run_id: str, text: str, *, timeout: float = 120.0) -> dict:
        reply = self._request("POST", f"/chat/sessions/{run_id}/messages", json={"text": text}, timeout=timeout)
        return self._shape(reply, text)

    def caller_line(self, persona: str, goal: str, transcript: list[dict], *, timeout: float = 120.0) -> dict:
        """The next thing a model-played caller says: {line, done}."""
        return self._request("POST", "/tests/caller", json={"persona": persona, "goal": goal, "transcript": transcript},
                             timeout=timeout)

    def end_chat(self, run_id: str) -> dict:
        return self._request("POST", f"/chat/sessions/{run_id}/end", json={})
