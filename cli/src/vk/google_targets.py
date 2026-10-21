"""Run `vk test` files against a live Google agent: Dialogflow CX or CX Agent Studio.

    vk test agents/clinic --target dfcx --agent projects/P/locations/global/agents/AGENT_ID
    vk test agents/clinic --target ces  --agent projects/P/locations/us/apps/APP_ID
    vk test agents/clinic --target engine,dfcx --agent ...      # both, with a side-by-side summary

The same tests/*.yaml files run as on the engine: scripted turns, or a model
playing the caller (the model comes from the Lampline engine, so that needs
--url too). What the Google agent says, which flows, pages, playbooks or agents
it goes through, and which tools it calls are checked with the same `expect`
keys, so a folder imported from Dialogflow CX or CX Agent Studio can be checked
against the original side by side.

Text only for now. Authentication is your own: an access token from
`gcloud auth print-access-token` (or VK_GOOGLE_TOKEN). It is never written
anywhere. Point this at your own agents, with synthetic test data.

Requests go only to Google's API hosts:
- Dialogflow CX: POST {region}-dialogflow.googleapis.com/v3/{agent}/sessions/{id}:detectIntent
- CX Agent Studio: POST ces.googleapis.com/v1/{app}/sessions/{id}:runSession
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from typing import Any

import httpx

from .engine_client import ApiError

DFCX_AGENT = re.compile(r"^projects/([^/]+)/locations/([a-z0-9-]+)/agents/([^/]+)$")
CES_APP = re.compile(r"^projects/([^/]+)/locations/([a-z0-9-]+)/apps/([^/]+)$")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


def access_token() -> str:
    """The caller's own Google access token: VK_GOOGLE_TOKEN, else `gcloud auth print-access-token`."""
    token = (os.environ.get("VK_GOOGLE_TOKEN") or "").strip()
    if token:
        return token
    gcloud = shutil.which("gcloud")
    if not gcloud:
        raise ApiError(0, "testing a Google agent needs an access token: install gcloud and run `gcloud auth login`, "
                          "or set VK_GOOGLE_TOKEN")
    try:
        out = subprocess.run([gcloud, "auth", "print-access-token"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ApiError(0, f"gcloud couldn't give an access token ({exc})") from exc
    if out.returncode != 0 or not out.stdout.strip():
        raise ApiError(0, "gcloud couldn't give an access token: run `gcloud auth login` first")
    return out.stdout.strip()


def _turn(run_id: str, n: int, events: list[dict], user_text: str | None) -> dict:
    turn = {"id": f"{run_id}-{n}", "status": "completed", "events": [{"type": e["type"], "payload": e} for e in events]}
    if user_text:
        turn["user_message"] = {"text": user_text}
    return turn


def _last(path: str) -> str:
    return str(path or "").rsplit("/", 1)[-1]


class _GoogleClient:
    """What scenario.run needs (start_chat, send_chat, end_chat, caller_line), over a Google sessions API."""

    target = ""

    def __init__(self, resource: str, *, token: str, engine=None, transport: httpx.BaseTransport | None = None,
                 timeout: float = 60.0):
        self.resource = resource
        self.engine = engine                   # an EngineClient, for model-played callers only
        self._http = httpx.Client(timeout=timeout, transport=transport,
                                  headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        self._sessions: dict[str, dict] = {}

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _post(self, url: str, body: dict, project: str) -> dict:
        try:
            r = self._http.post(url, json=body, headers={"x-goog-user-project": project})
        except httpx.HTTPError as exc:
            raise ApiError(0, f"cannot reach {self.target} ({exc})") from exc
        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message") or r.text
            except ValueError:
                msg = r.text
            raise ApiError(r.status_code, f"{self.target}: {str(msg)[:300]}")
        return r.json()

    def caller_line(self, persona: str, goal: str, transcript: list[dict], **kw) -> dict:
        if self.engine is None:
            raise ApiError(0, "a model-played caller needs the Lampline engine for its model: pass --url too")
        return self.engine.caller_line(persona, goal, transcript, **kw)

    def end_chat(self, run_id: str) -> dict:
        self._sessions.pop(run_id, None)       # Google sessions simply expire
        return {"ok": True}

    def _shape(self, run_id: str, events: list[dict], user_text: str | None, ended: bool) -> dict:
        s = self._sessions[run_id]
        s["n"] += 1
        return {"run_id": run_id, "is_completed": ended,
                "session_data": {"turns": [_turn(run_id, s["n"], events, user_text)],
                                 "status": "completed" if ended else "active"}}

    def start_chat(self, *, initial_context: dict | None = None, name: str | None = None, timeout: float = 60.0) -> dict:
        run_id = "vk-" + uuid.uuid4().hex[:20]
        self._sessions[run_id] = {"n": 0, "context": dict(initial_context or {}), "first": True, "where": None}
        return self._shape(run_id, [], None, False)     # the caller speaks first


class DialogflowCX(_GoogleClient):
    target = "Dialogflow CX"

    def __init__(self, agent: str, *, environment: str = "", language: str = "en", **kw):
        m = DFCX_AGENT.match(agent or "")
        if not m:
            raise ApiError(0, "--agent must look like projects/PROJECT/locations/LOCATION/agents/AGENT_ID")
        if environment and not NAME.match(environment):
            raise ApiError(0, "--environment is an environment ID")
        super().__init__(agent, **kw)
        self.project, self.location = m.group(1), m.group(2)
        host = "dialogflow.googleapis.com" if self.location == "global" else f"{self.location}-dialogflow.googleapis.com"
        self.base = f"https://{host}/v3/{agent}" + (f"/environments/{environment}" if environment else "")
        self.language = language

    def send_chat(self, run_id: str, text: str, *, timeout: float = 60.0) -> dict:
        s = self._sessions[run_id]
        body: dict[str, Any] = {"queryInput": {"text": {"text": text}, "languageCode": self.language}}
        if s["first"] and s["context"]:
            body["queryParams"] = {"parameters": s["context"]}
        s["first"] = False
        out = self._post(f"{self.base}/sessions/{run_id}:detectIntent", body, self.project)
        events, ended = self.events(out.get("queryResult") or {}, s)
        return self._shape(run_id, events, text, ended)

    @staticmethod
    def events(qr: dict, s: dict) -> tuple[list[dict], bool]:
        events: list[dict] = []

        def step(name: str, sid: str = "") -> None:
            if name and name != s.get("where"):
                s["where"] = name
                events.append({"type": "node_transition", "node_id": sid or name, "node_name": name})

        for block in qr.get("traceBlocks") or []:          # playbooks and flows the turn went through, in order
            meta = block.get("playbookTraceMetadata") or block.get("flowTraceMetadata") or {}
            step(meta.get("displayName") or "", _last(meta.get("playbook") or meta.get("flow") or ""))
            for action in block.get("actions") or []:
                use = action.get("toolUse")
                if use:
                    events.append({"type": "tool_call_started",
                                   "function_name": use.get("displayName") or _last(use.get("tool", ""))})
                for key in ("playbookInvocation", "flowInvocation"):
                    inv = action.get(key)
                    if inv and inv.get("displayName"):
                        step(inv["displayName"], _last(inv.get("playbook") or inv.get("flow") or ""))
        page = (qr.get("currentPage") or {}).get("displayName") or ""
        flow = (qr.get("currentFlow") or {}).get("displayName") or ""
        if page and page not in ("Start Page", "End Session", "End Flow"):
            step(page, _last((qr.get("currentPage") or {}).get("name", "")))
        elif flow and page == "Start Page":
            step(flow, _last((qr.get("currentFlow") or {}).get("name", "")))
        ended = page == "End Session"
        for msg in qr.get("responseMessages") or []:
            said = [t for t in (msg.get("text") or {}).get("text") or [] if t]
            if said:
                events.append({"type": "bot_speech", "text": " ".join(said)})
            elif (msg.get("outputAudioText") or {}).get("text"):
                events.append({"type": "bot_speech", "text": msg["outputAudioText"]["text"]})
            if msg.get("toolCall"):
                events.append({"type": "tool_call_started", "function_name": _last(msg["toolCall"].get("tool", ""))})
            if "endInteraction" in msg or "liveAgentHandoff" in msg or "telephonyTransferCall" in msg:
                ended = True
        for hook in qr.get("webhookStatuses") or []:
            if hook.get("code"):
                events.append({"type": "execution_error", "message": f"webhook error {hook.get('code')}: "
                                                                     f"{str(hook.get('message') or '')[:200]}"})
        return events, ended


class CXAgentStudio(_GoogleClient):
    target = "CX Agent Studio"

    def __init__(self, app: str, *, deployment: str = "", **kw):
        m = CES_APP.match(app or "")
        if not m:
            raise ApiError(0, "--agent must look like projects/PROJECT/locations/LOCATION/apps/APP_ID")
        if deployment and not NAME.match(deployment):
            raise ApiError(0, "--deployment is a deployment ID")
        super().__init__(app, **kw)
        self.project = m.group(1)
        self.deployment = f"{app}/deployments/{deployment}" if deployment else ""

    def send_chat(self, run_id: str, text: str, *, timeout: float = 60.0) -> dict:
        s = self._sessions[run_id]
        session = f"{self.resource}/sessions/{run_id}"
        inputs: list[dict] = []
        if s["first"] and s["context"]:
            inputs.append({"variables": s["context"]})
        s["first"] = False
        inputs.append({"text": text})
        config: dict[str, Any] = {"session": session}
        if self.deployment:
            config["deployment"] = self.deployment
        out = self._post(f"https://ces.googleapis.com/v1/{session}:runSession", {"config": config, "inputs": inputs},
                         self.project)
        events, ended = self.events(out.get("outputs") or [], s)
        return self._shape(run_id, events, text, ended)

    @staticmethod
    def events(outputs: list[dict], s: dict) -> tuple[list[dict], bool]:
        events: list[dict] = []
        ended, said = False, []
        for out in outputs:
            for msg in (out.get("diagnosticInfo") or {}).get("messages") or []:
                for chunk in msg.get("chunks") or []:
                    hop = chunk.get("agentTransfer")
                    if hop:
                        name = hop.get("displayName") or _last(hop.get("targetAgent", ""))
                        if name and name != s.get("where"):
                            s["where"] = name
                            events.append({"type": "node_transition", "node_id": _last(hop.get("targetAgent", "")) or name,
                                           "node_name": name})
            if out.get("text"):
                said.append(out["text"])
            for call in (out.get("toolCalls") or {}).get("toolCalls") or []:
                name = call.get("displayName") or (call.get("toolsetTool") or {}).get("toolId") or _last(call.get("tool", ""))
                events.append({"type": "tool_call_started", "function_name": name})
            if "endSession" in out:
                ended = True
        if said:
            events.append({"type": "bot_speech", "text": " ".join(said)})
        return events, ended
