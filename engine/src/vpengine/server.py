"""HTTP API and browser call page for the engine.

    vpengine --agents ./agents            # http://127.0.0.1:7860/call

Endpoints (all JSON; /v1/* need X-API-Key when VPE_API_KEY is set):

    GET    /v1/health
    GET    /v1/agents
    POST   /v1/chat/sessions                 {agent | name+definition, context}
    POST   /v1/chat/sessions/{id}/messages   {text}
    POST   /v1/chat/sessions/{id}/end
    POST   /v1/tests/caller                  {persona, goal, transcript}: a role-played caller's next line
    GET    /v1/sessions/{id}                 summary and events
    GET    /v1/sessions/{id}/events          live events (server-sent events)
    POST   /v1/calls/webrtc/offer            {agent, sdp, type, context}
    PATCH  /v1/calls/webrtc/offer            {pc_id, candidates}
    GET    /v1/me                            who this key is, and its role
    GET    /v1/keys, POST /v1/keys {name, role}, DELETE /v1/keys/{id}   named keys (admin)
    GET    /v1/calls                         call history (agent, channel, q, before, limit)
    GET    /v1/calls/{id}                    one call: transcript, events, collected details
    GET    /v1/calls/{id}/recording          its recording (WAV), when recordings are on
    DELETE /v1/calls/{id}

Agents' HTTP tools run for real on calls; chat and browser test calls can ask for
each tool's mock response instead (mock_tools). See README.md for the full list.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets as _secrets
import sys
import time
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from vk import agentfs, importers

from . import __version__
from . import graph as graph_module
from .automation import Automations
from .embed import Download as EmbedDownload, Embedder, MINILM
from . import keys as keys_module
from .history import CallHistory
from .library import KINDS, Library, LibraryError
from .connectors import ConnectorError, Connectors
from .graph import Graph
from .services import Config, ConfigError, make_llm
from . import realtime
from .session import Session, TextSession, VoiceSession
from . import builder, generate
from . import moonshine_stt, parakeet_stt
from .transfer import PendingTransfers
from .builtin_speech import Downloads, warm
from .fish import list_voices
from .local_speech import LocalSpeech
from .settings import SettingsStore, check, local_speech_problem, public_view
from .tools import Tool, ToolError, ToolRunner, env_secrets
from . import jambonz, telnyx
from .twilio import StreamTokens, hangup_twiml, reject_twiml, stream_twiml, valid_signature

log = logging.getLogger("vpengine.server")

IMPORT_MAX_BYTES = 60 * 1024 * 1024      # an uploaded export zip
_LOGO = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}\.(svg|png|webp)$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SESSION_TTL = 15 * 60       # forget finished sessions after this many seconds


class ChatStart(BaseModel):
    agent: str | None = None                 # folder name under the agents directory
    name: str | None = None                  # or an inline agent: name + definition
    definition: dict | None = None
    context_variables: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)   # per-session overrides
    tools: list[dict[str, Any]] | None = None             # inline agents' HTTP tools
    mock_tools: bool = False                               # answer tools with their mock responses


class ChatMessage(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


class WebRTCOffer(BaseModel):
    agent: str
    sdp: str
    type: str
    pc_id: str | None = None
    restart_pc: bool | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    mock_tools: bool = False


class IceCandidateIn(BaseModel):
    candidate: str
    sdp_mid: str
    sdp_mline_index: int


class WebRTCPatch(BaseModel):
    pc_id: str
    candidates: list[IceCandidateIn]


MESSAGE_CHANNELS = ("email", "whatsapp", "telegram")
MESSAGE_STARTS = int(os.environ.get("VPE_MESSAGE_STARTS_PER_10MIN", "30") or 30)
WEBHOOK_MAX_BYTES = 64 * 1024


class Engine:
    """Holds configuration, the agents directory and live sessions."""

    def __init__(self, agents_dir: Path, cfg: Config, *, api_key: str = "", max_sessions: int = 20,
                 llm_factory=None, speech_factory=None, public_url: str = "", twilio_auth_token: str = "",
                 twilio_hang_up: bool = True, store: "SettingsStore | None" = None, listen_host: str = "",
                 data_dir: Path | None = None, container: bool = False, remote_editing: bool = False):
        self.agents_dir = agents_dir
        self.public_url = public_url.rstrip("/")        # https address Twilio reaches us at (a tunnel)
        self._twilio_override = twilio_auth_token     # tests; otherwise from settings
        self.store: SettingsStore | None = store
        self.listen_host = listen_host
        # In a container the published port is the only way in, and every request already needs the API key,
        # so "this computer" means: holds the key and didn't come through a tunnel or proxy.
        self.container = container
        # VPE_REMOTE_EDITING=on: keys may change agents, data and knowledge from elsewhere (HTTPS proxy), as
        # their role allows. Settings, secrets, downloads and anything that contacts people stay on this computer.
        self.remote_editing = bool(remote_editing and api_key)
        self.local_speech = LocalSpeech(agents_dir)
        self.downloads = Downloads()
        self.moonshine = moonshine_stt.Downloads()
        self.parakeet = parakeet_stt.Downloads()
        self.twilio_hang_up = twilio_hang_up            # end the phone call via Twilio's API on an end step
        self.stream_tokens = StreamTokens()
        self.transfers = PendingTransfers()      # calls handed to a person, until the carrier asks what's next
        self.cfg = cfg
        self.api_key = api_key
        self.max_sessions = max_sessions
        self._message_starts: list[float] = []
        self.llm_factory = llm_factory          # tests inject fakes
        self.speech_factory = speech_factory    # () -> (stt, tts)
        self.realtime_factory = None            # tests: () -> a fake speech-to-speech LLM service
        self.tool_transport = None              # tests: an httpx transport for tool calls
        self.sessions: dict[str, Session] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self._webrtc = None
        self.connectors = Connectors(cfg, save=self._save_settings)
        self.library = Library(data_dir, self._embedder(cfg))          # records and knowledge, opened on first use
        self.history = CallHistory(self.library.folder)                 # finished calls, next to records
        self.keys = keys_module.KeyStore(store.path.parent / "keys.json" if store else None)   # named keys and roles
        self.connectors.library = self.library
        self.embed_download = EmbedDownload()
        graph_module.ZONE = cfg.zone if cfg.timezone else None
        state = (store.path.parent / "automation-state.json") if store else None
        self.automations = Automations(self, state)

    def _save_settings(self, changes: dict) -> None:
        """Connections that sign in (Google) save what they receive; the running config follows."""
        if self.store is None:
            raise ConfigError("this engine was started without a settings file")
        self.set_config(self.store.update(changes))

    @staticmethod
    def _embedder(cfg: Config) -> Embedder:
        return Embedder(cfg.embeddings, cfg.embeddings_url, cfg.embeddings_model)

    def set_config(self, cfg: Config) -> None:
        self.cfg = cfg
        self.connectors.cfg = cfg
        self.library.embedder = self._embedder(cfg)
        self.connectors._access = None if not cfg.google_ready else self.connectors._access
        graph_module.ZONE = cfg.zone if cfg.timezone else None

    @property
    def twilio_auth_token(self) -> str:
        return self._twilio_override or self.cfg.twilio_auth_token

    @property
    def voice_ready(self) -> bool:
        return self.cfg.voice_ready or self.speech_factory is not None

    def speech_to_speech(self, graph) -> tuple[bool, str | None]:
        """Whether this agent's calls use the speech-to-speech model, and why not when they can't."""
        if self.cfg.voice_mode != "speech-to-speech" or graph is None:
            return False, None
        if self.realtime_factory is not None:
            return True, None
        why = realtime.problem(graph, self.cfg)
        return why is None, why

    def voice_session(self, graph, transport, *, rate: int | None = None, stt=None, tts=None, llm=None, **kwargs):
        """A voice call: the speech-to-speech model when it's chosen and can run this agent, else the chain."""
        s2s, why = self.speech_to_speech(graph)
        if s2s:
            session = realtime.RealtimeVoiceSession(
                graph, self.cfg, transport, llm=self.realtime_factory() if self.realtime_factory else None,
                extract_llm=llm, **kwargs)
        else:
            rates = {"audio_in_rate": rate, "audio_out_rate": rate} if rate else {}
            session = VoiceSession(graph, self.cfg, transport, stt=stt, tts=tts, llm=llm, **rates, **kwargs)
        if why:
            session.emit({"type": "notice", "message": f"Using the normal voice chain: {why}."})
        return session

    @property
    def llm_ready(self) -> bool:
        return self.cfg.llm_ready or self.llm_factory is not None

    def allowed_hosts(self) -> set[str]:
        hosts = {"localhost", "127.0.0.1", "::1"}
        if self.listen_host:
            hosts.add(self.listen_host)
        if self.public_url:
            hosts.add(urlsplit(self.public_url).hostname or "")
        return hosts

    # -- agents --------------------------------------------------------------

    def list_agents(self) -> list[dict]:
        out = []
        if not self.agents_dir.is_dir():
            return out
        for d in sorted(self.agents_dir.iterdir()):
            if not (d / "agent.yaml").is_file():
                continue
            try:
                g = Graph.from_folder(d)
                steps = [s for s in g.steps.values() if s.kind not in ("global", "other")]
                out.append({
                    "slug": d.name, "name": g.name, "steps": [s.name for s in steps],
                    "graph": [{"id": s.id, "name": s.name, "kind": s.kind,
                               "greeting": bool(s.greeting), "extracts": bool(s.extraction),
                               "routes": [{"label": r.label, "to": g.steps[r.target].name,
                                           "condition": r.condition} for r in s.routes]} for s in steps],
                    "has_persona": bool(g.persona.strip()),
                    "tests": len(list((d / "tests").glob("*.yaml"))) if (d / "tests").is_dir() else 0,
                })
            except Exception as exc:  # noqa: BLE001 - one broken folder shouldn't hide the rest
                out.append({"slug": d.name, "error": str(exc)})
        return out

    def load_agent(self, slug: str) -> Graph:
        if not _SLUG.match(slug or ""):
            raise HTTPException(400, "agent must be a folder name")
        folder = self.agents_dir / slug
        if not (folder / "agent.yaml").is_file():
            raise HTTPException(404, f"no agent folder named {slug!r}")
        try:
            return Graph.from_folder(folder)
        except (agentfs.FolderError, ValueError) as exc:
            raise HTTPException(422, f"{slug}: {exc}") from exc

    # -- sessions ------------------------------------------------------------

    def _make_llm(self):
        return self.llm_factory() if self.llm_factory else None

    def _gc(self) -> None:
        now = time.time()
        for sid, s in list(self.sessions.items()):
            if s.finished.is_set() and now - s.created > SESSION_TTL:
                self.sessions.pop(sid, None)
                self.locks.pop(sid, None)

    def _check_capacity(self, messaging: bool = False) -> None:
        self._gc()
        live = [s for s in self.sessions.values() if not s.finished.is_set()]
        if len(live) >= self.max_sessions:
            raise HTTPException(429, f"{len(live)} sessions are already running (VPE_MAX_SESSIONS)")
        if messaging:
            # Anyone can write in, so message conversations get at most half the room (calls keep the rest)
            # and at most MESSAGE_STARTS new ones every 10 minutes.
            mine = sum(1 for s in live if (getattr(s, "meta", None) or {}).get("channel") in MESSAGE_CHANNELS)
            if mine >= max(1, self.max_sessions // 2):
                raise HTTPException(429, "too many message conversations at once")
            now = time.monotonic()
            self._message_starts = [t for t in self._message_starts if now - t < 600]
            if len(self._message_starts) >= MESSAGE_STARTS:
                raise HTTPException(429, "too many new message conversations; try again in a few minutes")
            self._message_starts.append(now)

    def add(self, session: Session) -> Session:
        if (isinstance(session, (VoiceSession, realtime.RealtimeVoiceSession)) and self.cfg.call_recordings == "on" and self.cfg.call_history != "off"
                and session.meta.get("agent")):
            session.record_to = self.history.recordings / f"{session.id}.wav"
        self.sessions[session.id] = session
        self.locks[session.id] = asyncio.Lock()
        try:
            asyncio.get_running_loop().create_task(self._after(session))
        except RuntimeError:                      # no loop (tests building sessions by hand)
            pass
        return session

    async def _after(self, session: Session) -> None:
        """When a conversation ends, keep it in the call history and run the agent's after-call triggers."""
        await session.finished.wait()
        if session.meta.get("agent") and self.cfg.call_history != "off":
            try:
                await asyncio.to_thread(self.history.save, session)
                await asyncio.to_thread(self.history.prune, int(self.cfg.call_history_days or 30))
            except Exception:  # noqa: BLE001
                log.exception("saving the call history failed")
        if session.meta.get("agent"):
            try:
                await self.automations.after_call(session)
            except Exception:  # noqa: BLE001
                log.exception("after-call triggers failed")

    def get(self, sid: str) -> Session:
        s = self.sessions.get(sid)
        if s is None:
            raise HTTPException(404, "no such session")
        return s

    def secrets(self) -> dict[str, str]:
        return self.store.secrets() if self.store else env_secrets()

    def tool_runner(self, *, mock: bool = False, trusted: bool = True, channel: dict | None = None) -> ToolRunner:
        return ToolRunner(self.secrets() if trusted else {}, mock=mock, transport=self.tool_transport,
                          connectors=self.connectors if trusted else None, live=trusted, channel=channel)

    def new_text(self, graph: Graph, context: dict, *, mock_tools: bool = False, trusted: bool = True,
                 channel: dict | None = None, meta: dict | None = None) -> TextSession:
        self._check_capacity(messaging=(channel or {}).get("kind") in MESSAGE_CHANNELS)
        try:
            return self.add(TextSession(graph, self.cfg, variables=context, llm=self._make_llm(),
                                        tools=self.tool_runner(mock=mock_tools, trusted=trusted, channel=channel),
                                        meta=meta))
        except ConfigError as exc:
            raise HTTPException(503, str(exc)) from exc

    @property
    def webrtc(self):
        if self._webrtc is None:
            from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequestHandler
            from pipecat.transports.smallwebrtc.connection import IceServer
            urls = [u.strip() for u in os.environ.get("VPE_ICE_SERVERS", "").split(",") if u.strip()]
            self._webrtc = SmallWebRTCRequestHandler(ice_servers=[IceServer(urls=u) for u in urls] or None)
        return self._webrtc

    async def shutdown(self) -> None:
        for s in list(self.sessions.values()):
            try:
                await s.stop()
            except Exception:  # noqa: BLE001
                pass
        if self._webrtc is not None:
            await self._webrtc.close()
        self.library.close()
        self.history.close()


TRANSFER_FIELDS = ("transfer_to", "transfer_details", "transfer_uui")
PROXY_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "forwarded", "x-real-ip", "x-forwarded-host", "via")
LOCAL_NAMES = ("127.0.0.1", "::1", "localhost", "testclient", "testserver")
EVENTS_PATH = re.compile(r"/v1/sessions/([A-Za-z0-9_-]{1,64})/events")


def _flag(value: Any, *, default: bool) -> bool:
    """A JSON true/false. Strings like "false" are refused rather than read as true."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    raise HTTPException(422, "dry_run must be true or false")


class IdempotencyKeys:
    """Results of state-changing API requests by their Idempotency-Key, for a day, so a retry gets the first
    answer instead of a second call."""

    TTL = 86400.0
    MAX = 5000

    def __init__(self):
        self._items: dict[str, tuple[float, dict]] = {}

    def get(self, key: str) -> dict | None:
        now = time.time()
        item = self._items.get(key)
        return item[1] if item and item[0] > now else None

    def put(self, key: str, value: dict) -> None:
        now = time.time()
        if len(self._items) >= self.MAX:
            self._items = {k: v for k, v in self._items.items() if v[0] > now}
            while len(self._items) >= self.MAX:
                self._items.pop(next(iter(self._items)))
        self._items[key] = (now + self.TTL, value)

    def drop(self, key: str) -> None:
        self._items.pop(key, None)


def chat_payload(session: Session, events: list[dict]) -> dict:
    return {"session_id": session.id, "ended": session.flow.ended, "events": events, **{
        k: v for k, v in session.summary().items() if k in ("current_step", "steps", "variables")}}


def create_app(engine: Engine) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        if engine.cfg.stt == "whisper" or engine.cfg.tts == "kokoro":
            # Load the built-in models now so the first call doesn't wait for them.
            asyncio.get_running_loop().run_in_executor(None, warm, engine.cfg.local_whisper_model)
        if engine.cfg.stt == "moonshine" and moonshine_stt.ready(engine.cfg.moonshine_model):
            asyncio.get_running_loop().run_in_executor(None, moonshine_stt.load, engine.cfg.moonshine_model)
        if engine.cfg.stt == "parakeet" and parakeet_stt.ready(engine.cfg.parakeet_model):
            asyncio.get_running_loop().run_in_executor(None, parakeet_stt.load, engine.cfg.parakeet_model)
        if engine.store is not None:            # tests drive triggers directly
            engine.automations.start()

        async def prune_history():
            # Old calls go even when no new calls come in: at start, then every few hours.
            while True:
                try:
                    await asyncio.to_thread(engine.history.prune, int(engine.cfg.call_history_days or 30))
                except Exception:  # noqa: BLE001 - try again next time
                    log.exception("pruning the call history failed")
                await asyncio.sleep(6 * 3600)

        pruner = asyncio.create_task(prune_history())
        yield
        pruner.cancel()
        await engine.automations.stop()
        await engine.shutdown()

    app = FastAPI(title="Voice engine", version=__version__, docs_url=None, redoc_url=None, openapi_url=None,
                  lifespan=lifespan)

    def require_key(request: Request) -> None:
        """The owner key (VPE_API_KEY) or a named key; a named key's role limits what it may change."""
        request.state.who = keys_module.describe(None)
        if not engine.api_key:
            return
        given = request.headers.get("x-api-key") or ""
        m = EVENTS_PATH.fullmatch(request.url.path)
        if not given and request.method == "GET" and m and request.query_params.get("token"):
            # EventSource can't send headers: it gets a token for this one session instead of the API key,
            # so no key ever sits in a URL (proxy access logs keep URLs).
            s = engine.sessions.get(m.group(1))
            want = str((getattr(s, "meta", None) or {}).get("events_token") or "") if s is not None else ""
            if want and hmac.compare_digest(request.query_params["token"].encode(), want.encode()):
                request.state.who = (s.meta or {}).get("by") or keys_module.describe(None)
                request.state.events_token = True
                return
            raise HTTPException(401, "missing or wrong events token")
        if hmac.compare_digest(given.encode(), engine.api_key.encode()):
            return
        rec = engine.keys.match(given)
        if rec is None:
            raise HTTPException(401, "missing or wrong X-API-Key")
        request.state.who = keys_module.describe(rec)
        if not keys_module.allowed(rec["role"], request.method, request.url.path):
            raise HTTPException(403, f"your key ({rec['name']}) is a {rec['role']} key, which can't do this; ask an admin")

    auth = [Depends(require_key)]
    idempotent = IdempotencyKeys()

    @app.middleware("http")
    async def same_site_only(request: Request, call_next):
        """Refuse other websites and DNS-rebinding tricks: the Host must be ours, and a
        browser request that changes something must come from our own page."""
        host = urlsplit("//" + (request.headers.get("host") or "")).hostname or ""
        if host not in engine.allowed_hosts():
            return JSONResponse({"detail": "unknown host"}, status_code=421)
        origin = request.headers.get("origin")
        if origin and request.method not in ("GET", "HEAD", "OPTIONS"):
            if urlsplit(origin).netloc.lower() != (request.headers.get("host") or "").lower():
                return JSONResponse({"detail": "cross-site request refused"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        if "content-security-policy" not in response.headers:
            response.headers["Content-Security-Policy"] = "frame-ancestors 'none'"
        return response

    static_dir = resources.files("vpengine").joinpath("static")
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    def _page() -> HTMLResponse:
        page = static_dir.joinpath("index.html").read_text(encoding="utf-8")
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.get("/", include_in_schema=False)
    async def root():
        return _page()

    # -- logos: optional images you add for the Integrations page -----------------
    # Put official logo files (e.g. twilio.svg) in the folder next to your settings file:
    # ~/.config/vpengine/logos/. Nothing is bundled; tiles show monograms without them.

    def _logo_dir() -> Path:
        return (engine.store.path.parent if engine.store else Path.home() / ".config" / "vpengine") / "logos"

    @app.get("/v1/logos", dependencies=auth)
    async def list_logos():
        d = _logo_dir()
        names = sorted(p.name for p in d.iterdir() if p.is_file() and _LOGO.match(p.name)) if d.is_dir() else []
        return {"logos": names, "folder": str(d)}

    @app.get("/v1/logos/{name}", include_in_schema=False)
    async def logo(name: str):
        # Public (an <img> can't send the API key) but tightly scoped: one folder, plain names,
        # image types only, and a CSP so an SVG opened directly can't run anything.
        if not _LOGO.match(name):
            raise HTTPException(404)
        path = _logo_dir() / name
        if not path.is_file() or path.stat().st_size > 512_000:
            raise HTTPException(404)
        kind = {"svg": "image/svg+xml", "png": "image/png", "webp": "image/webp"}[name.rsplit(".", 1)[1]]
        return Response(path.read_bytes(), media_type=kind, headers={
            "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
            "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})

    @app.get("/call", include_in_schema=False)
    async def call_page():
        return RedirectResponse("/#/test")

    def _signed_in(request: Request) -> bool:
        if not engine.api_key:
            return True
        given = request.headers.get("x-api-key") or ""
        return hmac.compare_digest(given.encode(), engine.api_key.encode()) or engine.keys.match(given) is not None

    @app.get("/v1/health")
    async def health(request: Request):
        basic = {"ok": True, "version": __version__, "auth_required": bool(engine.api_key), "container": engine.container,
                 "brand": (engine.cfg.brand_name or "Lampline")[:32]}
        if not _signed_in(request):
            return basic                 # anyone who can reach the address: no setup details
        return {**basic,
                "phone_webhook": f"{engine.public_url}/v1/twilio/voice" if engine.public_url else None,
                "telnyx_webhook": f"{engine.public_url}/v1/telnyx/voice" if engine.public_url else None,
                "jambonz_webhook": f"{engine.public_url}/v1/jambonz/call" if engine.public_url else None,
                "config": {**engine.cfg.describe(), "voice_ready": engine.voice_ready,
                           "llm_ready": engine.llm_ready}}

    # -- integrations --------------------------------------------------------

    def _is_local(request: Request) -> bool:
        """A browser or script on this computer, not through the tunnel or the network.

        Fails closed: any proxy header, or a Host that isn't this computer's own name, means "not local". A proxy
        can still rewrite Host and send no forwarding header (a bare nginx proxy_pass, an SSH tunnel), so once the
        engine has a public address or runs in Docker, a local-only change also needs the owner key."""
        if any(request.headers.get(h) for h in PROXY_HEADERS):
            return False
        named = urlsplit("//" + (request.headers.get("host") or "")).hostname or ""
        if named not in LOCAL_NAMES:
            return False
        client = request.client.host if request.client else ""
        if client not in LOCAL_NAMES and not (engine.container and engine.api_key):
            return False                                 # Docker's port mapping hides the address; the key doesn't
        if (engine.container or engine.public_url) and engine.api_key:
            given = request.headers.get("x-api-key") or ""
            return hmac.compare_digest(given.encode(), engine.api_key.encode())
        return True

    def _local_only(request: Request) -> None:
        if engine.remote_editing and not keys_module.always_local(request.method, request.url.path):
            return                                   # require_key already checked the key and its role
        if not _is_local(request):
            raise HTTPException(403, "this can only be done on this computer, not through the tunnel or the network")

    @app.get("/v1/settings", dependencies=auth)
    async def get_settings():
        if engine.store is None:
            raise HTTPException(404, "settings are read from the environment only")
        return public_view(engine.cfg, engine.store.path)

    @app.put("/v1/settings", dependencies=auth)
    async def put_settings(request: Request, body: dict[str, Any]):
        _local_only(request)
        if engine.store is None:
            raise HTTPException(404, "settings are read from the environment only")
        try:
            engine.set_config(engine.store.update(body))
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        return public_view(engine.cfg, engine.store.path)

    @app.get("/v1/fish/voices", dependencies=auth)
    async def fish_voices():
        if not engine.cfg.fish_key:
            raise HTTPException(409, "save a Fish Audio API key first")
        try:
            return await list_voices(engine.cfg.fish_key)
        except PermissionError as exc:
            raise HTTPException(422, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(502, f"couldn't load Fish Audio voices: {exc.__class__.__name__}") from exc

    @app.get("/v1/builtin-speech", dependencies=auth)
    async def builtin_speech_status():
        return engine.downloads.status(engine.cfg.local_whisper_model)

    @app.post("/v1/builtin-speech/download", dependencies=auth)
    async def builtin_speech_download(request: Request):
        _local_only(request)
        try:
            engine.downloads.start(engine.cfg.local_whisper_model)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return engine.downloads.status(engine.cfg.local_whisper_model)

    @app.get("/v1/moonshine", dependencies=auth)
    async def moonshine_status():
        return engine.moonshine.status(engine.cfg.moonshine_model)

    @app.post("/v1/moonshine/download", dependencies=auth)
    async def moonshine_download(request: Request):
        _local_only(request)
        try:
            engine.moonshine.start(engine.cfg.moonshine_model)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return engine.moonshine.status(engine.cfg.moonshine_model)

    @app.get("/v1/parakeet", dependencies=auth)
    async def parakeet_status():
        return engine.parakeet.status(engine.cfg.parakeet_model)

    @app.post("/v1/parakeet/download", dependencies=auth)
    async def parakeet_download(request: Request):
        _local_only(request)
        try:
            engine.parakeet.start(engine.cfg.parakeet_model)
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return engine.parakeet.status(engine.cfg.parakeet_model)

    @app.get("/v1/local-speech", dependencies=auth)
    async def local_speech_status():
        return await engine.local_speech.status(engine.cfg)

    @app.post("/v1/local-speech/{action}", dependencies=auth)
    async def local_speech_action(action: str, request: Request):
        _local_only(request)
        try:
            if action == "start":
                engine.local_speech.start(engine.cfg)
            elif action == "stop":
                engine.local_speech.stop()
            else:
                raise HTTPException(404, "start or stop")
        except RuntimeError as exc:
            raise HTTPException(409, str(exc)) from exc
        return await engine.local_speech.status(engine.cfg)

    @app.post("/v1/settings/check/{service}", dependencies=auth)
    async def check_settings(service: str, request: Request):
        _local_only(request)
        if service in ("telegram", "whatsapp", "email", "google"):
            return await engine.connectors.test(service)
        if service not in ("llm", "stt", "tts", "deepgram", "fish", "whisper", "kokoro", "whisper-server", "kokoro-server"):
            raise HTTPException(404, "nothing to check by that name")
        return await check(service, engine.cfg)

    @app.get("/v1/agents", dependencies=auth)
    async def agents():
        return {"agents": engine.list_agents(), "dir": str(engine.agents_dir)}

    # -- builder -------------------------------------------------------------

    def _folder(slug: str) -> Path:
        if not _SLUG.match(slug or ""):
            raise HTTPException(400, "agent must be a folder name")
        folder = engine.agents_dir / slug
        if not (folder / "agent.yaml").is_file():
            raise HTTPException(404, f"no agent folder named {slug!r}")
        return folder

    @app.get("/v1/templates", dependencies=auth)
    async def list_templates():
        return {"templates": builder.templates()}

    @app.get("/v1/agents/{slug}", dependencies=auth)
    async def get_agent(slug: str):
        try:
            return builder.read(_folder(slug))
        except agentfs.FolderError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/v1/agents/{slug}", dependencies=auth)
    async def save_agent(slug: str, body: dict[str, Any], request: Request):
        _local_only(request)
        if not _is_local(request):
            _same_reach(_folder(slug), body)
        try:
            return builder.save(_folder(slug), body)
        except (builder.BuilderError, agentfs.FolderError) as exc:
            raise HTTPException(422, str(exc)) from exc

    def _reach(doc: dict) -> str:
        """What in an agent can use your secrets or contact people: tools, triggers, webhook/trigger nodes, where
        end steps transfer calls (and what they send), and the agent's own values those addresses use."""
        all_nodes = [n for n in ((doc.get("definition") or {}).get("nodes") or []) if isinstance(n, dict)]
        nodes = [n for n in all_nodes if n.get("type") in ("trigger", "webhook", "qa")]
        transfers = sorted((str(n.get("id")), {k: (n.get("data") or {}).get(k) for k in TRANSFER_FIELDS})
                           for n in all_nodes if n.get("type") == "endCall" and isinstance(n.get("data"), dict)
                           and any((n.get("data") or {}).get(k) for k in TRANSFER_FIELDS))
        reach = {"tools": doc.get("tools") or [], "triggers": doc.get("triggers") or [], "nodes": nodes,
                 "transfers": transfers}
        used = set(re.findall(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}", json.dumps(reach, default=str)))
        constants = doc.get("context_variables") or {}
        reach["values"] = {k: constants.get(k) for k in sorted(used) if isinstance(constants, dict) and k in constants}
        return json.dumps(reach, sort_keys=True, default=str)

    def _same_reach(folder: Path, body: dict) -> None:
        """Saved from elsewhere (VPE_REMOTE_EDITING): steps, prompts and data may change, but not what can use
        secrets or contact people. A tool or trigger pointed at another address could send them anywhere."""
        try:
            current = builder.read(folder) if folder.exists() else {}
        except (builder.BuilderError, agentfs.FolderError):
            current = {}
        if _reach(current) != _reach(body):
            raise HTTPException(403, "tools, triggers and webhooks can only be changed on this computer: they can "
                                     "use your secrets and contact people")

    @app.post("/v1/agents/{slug}/check", dependencies=auth)
    async def check_agent(slug: str, body: dict[str, Any]):
        try:
            return {"findings": builder.check(_folder(slug), body)}
        except (builder.BuilderError, agentfs.FolderError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/v1/agents/generate", dependencies=auth)
    async def generate_agent(body: dict[str, Any], request: Request):
        """Describe an agent in plain words; the model drafts it into a new folder."""
        _local_only(request)
        description = str(body.get("description") or "").strip()
        name = str(body.get("name") or "").strip()[:80]
        if len(description) < 12:
            raise HTTPException(422, "describe the agent in a sentence or two")
        if len(description) > 6000:
            raise HTTPException(422, "keep the description under 6,000 characters")
        try:
            llm = engine._make_llm() or make_llm(engine.cfg)
        except ConfigError as exc:
            raise HTTPException(503, str(exc)) from exc
        try:
            outline = await generate.draft(llm, description, name)
        except generate.GenerateError as exc:
            raise HTTPException(502, str(exc)) from exc
        agent = generate.build(outline, name)
        slug = builder.create_from(engine.agents_dir, agent)
        return builder.read(engine.agents_dir / slug)

    # ---- Data: records agents look up and save (local only: it may hold callers' details) ----
    def _lib(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except LibraryError as exc:
            raise HTTPException(404 if "there's no" in str(exc) else 422, str(exc)) from exc

    async def _webhook_body(request: Request) -> bytes:
        """A provider's webhook: small by nature, and read before its signature can be checked."""
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > WEBHOOK_MAX_BYTES:
                raise HTTPException(413, "request too large")
        return bytes(data)

    async def _body(request: Request, limit: int) -> bytes:
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > limit:
                raise HTTPException(413, f"the file is larger than {limit // (1024 * 1024)} MB")
        if not data:
            raise HTTPException(422, "send the file as the request body")
        return bytes(data)

    def _phi_warning(text: str) -> list[str]:
        from vk import phi
        hits = phi.scan(text[:2_000_000])
        if not hits:
            return []
        kinds = sorted({h.label for h in hits})
        return [f"This looks like it may contain real personal details ({', '.join(kinds[:4])}). "
                "Use synthetic data unless this engine runs where real patient data is allowed."]

    @app.get("/v1/data", dependencies=auth)
    async def data_collections(request: Request):
        _local_only(request)
        return {"collections": _lib(engine.library.collections), "folder": str(engine.library.folder)}

    @app.post("/v1/data", dependencies=auth)
    async def data_create(request: Request, body: dict[str, Any]):
        _local_only(request)
        return _lib(engine.library.create_collection, str(body.get("name") or ""), str(body.get("description") or ""),
                    str(body.get("key") or ""))

    @app.delete("/v1/data/{name}", dependencies=auth)
    async def data_drop(request: Request, name: str):
        _local_only(request)
        _lib(engine.library.delete_collection, name)
        return {"ok": True}

    @app.get("/v1/data/{name}", dependencies=auth)
    async def data_records(request: Request, name: str, limit: int = 50, offset: int = 0, search: str = ""):
        _local_only(request)
        return {**_lib(engine.library.collection, name), **_lib(engine.library.records, name, limit=limit, offset=offset, search=search)}

    @app.post("/v1/data/{name}/records", dependencies=auth)
    async def data_add(request: Request, name: str, body: dict[str, Any]):
        _local_only(request)
        rid = body.get("id")
        rec = _lib(engine.library.put, name, body.get("data") or {}, str(rid) if rid else None)
        return {"record": rec, "warnings": _phi_warning(json.dumps(rec))}

    @app.put("/v1/data/{name}/records/{rid}", dependencies=auth)
    async def data_put(request: Request, name: str, rid: str, body: dict[str, Any]):
        _local_only(request)
        rec = _lib(engine.library.put, name, body.get("data") or {}, rid)
        return {"record": rec, "warnings": _phi_warning(json.dumps(rec))}

    @app.delete("/v1/data/{name}/records/{rid}", dependencies=auth)
    async def data_delete(request: Request, name: str, rid: str):
        _local_only(request)
        if not _lib(engine.library.delete, name, rid):
            raise HTTPException(404, "no such record")
        return {"ok": True}

    @app.post("/v1/data/{name}/import", dependencies=auth)
    async def data_import(request: Request, name: str, filename: str = "records.csv"):
        _local_only(request)
        raw = await _body(request, 20 * 1024 * 1024)
        out = await asyncio.to_thread(_lib, engine.library.import_rows, name, raw, filename)
        return {**out, "warnings": _phi_warning(raw.decode("utf-8", "replace"))}

    @app.post("/v1/data/{name}/lookup", dependencies=auth)
    async def data_lookup(request: Request, name: str, body: dict[str, Any]):
        _local_only(request)
        where = {str(body.get("field") or "id"): body.get("value")}
        return {"records": _lib(engine.library.find, name, where, limit=5)}

    # ---- Knowledge: documents agents search ----
    def _embeddings_status() -> dict:
        e = engine.library.embedder
        return {"mode": engine.cfg.embeddings, "ready": e.ready, "model": e.name or None,
                "download": engine.embed_download.status() if engine.cfg.embeddings == "local" else None,
                "kinds": list(KINDS)}

    @app.get("/v1/knowledge", dependencies=auth)
    async def knowledge_bases(request: Request):
        _local_only(request)
        return {"bases": _lib(engine.library.bases), "embeddings": _embeddings_status()}

    @app.post("/v1/knowledge", dependencies=auth)
    async def knowledge_create(request: Request, body: dict[str, Any]):
        _local_only(request)
        return _lib(engine.library.create_base, str(body.get("name") or ""), str(body.get("description") or ""))

    @app.get("/v1/knowledge/{name}", dependencies=auth)
    async def knowledge_base(request: Request, name: str):
        _local_only(request)
        return _lib(engine.library.base, name)

    @app.delete("/v1/knowledge/{name}", dependencies=auth)
    async def knowledge_drop(request: Request, name: str):
        _local_only(request)
        _lib(engine.library.delete_base, name)
        return {"ok": True}

    @app.post("/v1/knowledge/{name}/documents", dependencies=auth)
    async def knowledge_add(request: Request, name: str, filename: str = "document.txt"):
        _local_only(request)
        raw = await _body(request, 20 * 1024 * 1024)
        try:
            doc = await engine.library.add_document(name, filename, raw)
        except LibraryError as exc:
            raise HTTPException(404 if "there's no" in str(exc) else 422, str(exc)) from exc
        return {"document": doc, "warnings": _phi_warning(raw.decode("utf-8", "replace")) if filename.lower().endswith(
            (".txt", ".md", ".csv", ".json", ".html", ".htm")) else []}

    @app.delete("/v1/knowledge/{name}/documents/{doc}", dependencies=auth)
    async def knowledge_remove(request: Request, name: str, doc: int):
        _local_only(request)
        if not _lib(engine.library.delete_document, name, doc):
            raise HTTPException(404, "no such document")
        return {"ok": True}

    @app.post("/v1/knowledge/{name}/search", dependencies=auth)
    async def knowledge_search(request: Request, name: str, body: dict[str, Any]):
        _local_only(request)
        try:
            return {"results": await engine.library.search(name, str(body.get("query") or ""), int(body.get("results") or 3))}
        except LibraryError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/v1/knowledge/{name}/reindex", dependencies=auth)
    async def knowledge_reindex(request: Request, name: str):
        _local_only(request)
        _lib(engine.library.base, name)
        return {"embedded": await engine.library.embed_missing(name)}

    @app.get("/v1/embeddings", dependencies=auth)
    async def embeddings_status(request: Request):
        _local_only(request)
        return _embeddings_status()

    @app.post("/v1/embeddings/download", dependencies=auth)
    async def embeddings_download(request: Request):
        _local_only(request)
        engine.embed_download.start()
        return _embeddings_status()

    @app.post("/v1/agents/import", dependencies=auth)
    async def import_agent(request: Request, source: str = "auto", name: str = "", filename: str = ""):
        """Convert a Dialogflow CX or CX Agent Studio export (the zip, sent as the raw body) into a new agent."""
        _local_only(request)
        if source not in ("auto", "dfcx", "ces"):
            raise HTTPException(422, "source must be auto, dfcx or ces")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > IMPORT_MAX_BYTES:
                raise HTTPException(413, f"the export is larger than {IMPORT_MAX_BYTES // (1024 * 1024)} MB")
        if not data:
            raise HTTPException(422, "send the exported .zip file")
        try:
            agent, report = await asyncio.to_thread(importers.convert, bytes(data), source, name.strip()[:80])
        except importers.ImportError_ as exc:
            raise HTTPException(422, f"couldn't import: {exc}") from exc
        if not name.strip() and agent.name == "Imported agent" and filename:
            stem = re.sub(r"\.zip$", "", Path(filename).name, flags=re.I)
            agent.name = re.sub(r"[_\-]+", " ", stem).strip()[:80] or agent.name
        slug = builder.create_from(engine.agents_dir, agent, {"import-report.md": report.markdown(agent.name)})
        out = builder.read(engine.agents_dir / slug)
        out["import"] = {"source": report.source, "notes": report.notes, "counts": report.counts}
        return out

    @app.post("/v1/agents", dependencies=auth)
    async def create_agent(body: dict[str, Any], request: Request):
        _local_only(request)
        engine.agents_dir.mkdir(parents=True, exist_ok=True)
        try:
            slug = builder.create(engine.agents_dir, str(body.get("name") or ""), str(body.get("template") or ""))
        except builder.BuilderError as exc:
            raise HTTPException(422, str(exc)) from exc
        return builder.read(engine.agents_dir / slug)

    # -- tools ---------------------------------------------------------------

    def _store() -> SettingsStore:
        if engine.store is None:
            raise HTTPException(409, "this engine was started without a settings file")
        return engine.store

    @app.get("/v1/secrets", dependencies=auth)
    async def list_secrets():
        """Names and last four characters only; values never leave this computer."""
        return {"secrets": _store().secrets_view() if engine.store else
                [{"name": k, "source": "env", "hint": "set"} for k in sorted(env_secrets())]}

    @app.put("/v1/secrets/{name}", dependencies=auth)
    async def put_secret(name: str, body: dict[str, Any], request: Request):
        _local_only(request)
        try:
            _store().set_secret(name, str(body.get("value") or ""))
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"secrets": _store().secrets_view()}

    @app.delete("/v1/secrets/{name}", dependencies=auth)
    async def delete_secret(name: str, request: Request):
        _local_only(request)
        try:
            _store().set_secret(name, "")
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {"secrets": _store().secrets_view()}

    @app.post("/v1/tools/try", dependencies=auth)
    async def try_tool(body: dict[str, Any], request: Request):
        """Run one tool definition once, from the builder. Real requests use your secrets,
        so this only works from this computer."""
        _local_only(request)
        try:
            functions = Tool.parse_entry(body.get("tool") or {})
        except (ToolError, KeyError, TypeError) as exc:
            raise HTTPException(422, str(exc)) from exc
        if not functions:
            raise HTTPException(422, "this MCP server has no tools listed yet: press Refresh")
        tool = next((t for t in functions if t.name == body.get("function")), functions[0])
        args = body.get("arguments") or {}
        variables = body.get("variables") or {}
        if not isinstance(args, dict) or not isinstance(variables, dict):
            raise HTTPException(422, "arguments and variables must be objects")
        runner = engine.tool_runner(mock=bool(body.get("mock")))
        try:
            result = await runner.run(tool, args, variables)
        finally:
            await runner.aclose()
        return result.for_event()

    # -- text chat -----------------------------------------------------------

    @app.post("/v1/chat/sessions", dependencies=auth)
    async def chat_start(body: ChatStart, request: Request):
        if body.definition is not None:
            try:
                graph = Graph.from_definition(body.name or "agent", body.definition, body.context_variables,
                                              body.tools)
            except (ValueError, KeyError) as exc:
                raise HTTPException(422, f"bad agent definition: {exc}") from exc
        elif body.agent:
            graph = engine.load_agent(body.agent)
        else:
            raise HTTPException(400, "give agent (a folder name) or name + definition")
        # Tools sent inline by someone not on this computer never see your secrets and never call out.
        trusted = not body.tools or _is_local(request)
        session = engine.new_text(graph, body.context, mock_tools=body.mock_tools or not trusted, trusted=trusted,
                                  meta={**({"agent": body.agent, "channel": "web"} if body.agent else {}),
                                        **_owned(request)})
        async with engine.locks[session.id]:
            try:
                events = await session.open()
            except Exception as exc:  # noqa: BLE001
                await session.close()
                raise HTTPException(502, f"could not start: {exc}") from exc
        return {**chat_payload(session, events), "events_token": session.meta["events_token"]}

    def _owned(request: Request) -> dict:
        """Who started a conversation (it's theirs to watch), and a token for its live events."""
        return {"by": getattr(request.state, "who", None) or keys_module.describe(None),
                "events_token": _secrets.token_urlsafe(18)}

    def _session(sid: str, request: Request) -> Session:
        """A live conversation, for the key that started it (or an admin). Phone and message conversations
        carry callers' details, like call history: this computer only (or VPE_REMOTE_EDITING)."""
        s = engine.get(sid)
        if getattr(request.state, "events_token", False):
            return s                                   # the token was checked against this very session
        who = getattr(request.state, "who", None) or keys_module.describe(None)
        by = (s.meta or {}).get("by")
        if by is None:
            _local_only(request)
            return s
        if who.get("role") != "admin" and who.get("name") != by.get("name"):
            raise HTTPException(403, "this conversation was started by someone else")
        return s

    def _text(sid: str, request: Request) -> TextSession:
        s = _session(sid, request)
        if not isinstance(s, TextSession):
            raise HTTPException(400, "not a text chat session")
        return s

    @app.post("/v1/chat/sessions/{sid}/messages", dependencies=auth)
    async def chat_send(sid: str, body: ChatMessage, request: Request):
        s = _text(sid, request)
        async with engine.locks[sid]:
            if s.flow.ended:
                raise HTTPException(409, "the conversation has ended")
            events = await s.send(body.text)
        return chat_payload(s, events)

    @app.post("/v1/chat/sessions/{sid}/end", dependencies=auth)
    async def chat_end(sid: str, request: Request):
        s = _text(sid, request)
        if not s.flow.ended:
            await s.flow.finish("ended by client")
        await s.close()
        return chat_payload(s, [])

    @app.get("/v1/sessions/{sid}", dependencies=auth)
    async def session_info(sid: str, request: Request):
        s = _session(sid, request)
        return {**s.summary(), "events": s.events}

    @app.get("/v1/sessions/{sid}/events", dependencies=auth)
    async def session_events(sid: str, request: Request):
        s = _session(sid, request)
        queue: asyncio.Queue = asyncio.Queue()
        backlog = list(s.events)
        unsubscribe = s.subscribe(queue.put_nowait)

        async def stream():
            try:
                for e in backlog:
                    yield f"data: {json.dumps(e)}\n\n"
                if s.finished.is_set() and queue.empty():
                    return                                 # an ended conversation: the backlog is all there is
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        e = await asyncio.wait_for(queue.get(), 15)
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
                        if s.finished.is_set():
                            break
                        continue
                    yield f"data: {json.dumps(e)}\n\n"
                    if e.get("type") == "call_ended":
                        break
            finally:
                unsubscribe()

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    # -- browser calls (WebRTC) ----------------------------------------------

    @app.post("/v1/calls/webrtc/offer", dependencies=auth)
    async def webrtc_offer(body: WebRTCOffer, request: Request):
        from pipecat.transports.base_transport import TransportParams
        from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest
        from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

        graph = engine.load_agent(body.agent) if not body.pc_id else None
        s2s = engine.speech_to_speech(graph)[0]
        if not engine.voice_ready and not s2s:
            raise HTTPException(503, "voice calls need speech set up in Integrations (text chat works without it)")
        if not engine.llm_ready and not s2s:
            raise HTTPException(503, "no model key set: add one in Integrations")
        if engine.speech_factory is None and not body.pc_id and not s2s:
            problem = await local_speech_problem(engine.cfg)
            if problem:
                raise HTTPException(503, problem)
        if graph is not None:
            engine._check_capacity()
        holder: dict[str, Session] = {}
        owner = _owned(request)

        async def on_connection(connection):
            transport = SmallWebRTCTransport(connection, TransportParams(audio_in_enabled=True,
                                                                         audio_out_enabled=True))
            stt, tts = engine.speech_factory() if engine.speech_factory else (None, None)
            session = engine.add(engine.voice_session(graph, transport, autostart=False, stt=stt, tts=tts,
                                              variables=body.context, llm=engine._make_llm(),
                                              tools=engine.tool_runner(mock=body.mock_tools),
                                              meta={"agent": body.agent, "channel": "web", **owner}))
            holder["s"] = session

            @transport.event_handler("on_client_connected")
            async def _connected(t, client):
                await session.begin()

            @transport.event_handler("on_client_disconnected")
            async def _gone(t, client):
                await session.stop()

            asyncio.create_task(session.run())

        try:
            answer = await engine.webrtc.handle_web_request(
                SmallWebRTCRequest(sdp=body.sdp, type=body.type, pc_id=body.pc_id, restart_pc=body.restart_pc),
                on_connection)
        except ConfigError as exc:
            raise HTTPException(503, str(exc)) from exc
        if graph is not None and "s" not in holder:
            raise HTTPException(500, "the call could not be set up; see the engine log")
        if "s" in holder:
            answer = {**answer, "session_id": holder["s"].id, "events_token": owner["events_token"]}
        return answer

    @app.patch("/v1/calls/webrtc/offer", dependencies=auth)
    async def webrtc_patch(body: WebRTCPatch):
        from pipecat.transports.smallwebrtc.request_handler import IceCandidate, SmallWebRTCPatchRequest

        await engine.webrtc.handle_patch_request(SmallWebRTCPatchRequest(
            pc_id=body.pc_id, candidates=[IceCandidate(**c.model_dump()) for c in body.candidates]))
        return {"ok": True}

    # -- phone calls (Twilio and Telnyx media streams) -----------------------

    def _xml(body: str) -> Response:
        return Response(body, media_type="application/xml")

    async def _refusal(agent: str) -> str | None:
        """Why this line can't take a call right now (logged; the caller hears a short apology)."""
        try:
            graph = engine.load_agent(agent)
            engine._check_capacity()
        except HTTPException as exc:
            return str(exc.detail)
        if engine.speech_to_speech(graph)[0]:
            return None
        if not engine.voice_ready or not engine.llm_ready:
            return "model or speech isn't set up"
        if engine.speech_factory is None:
            return await local_speech_problem(engine.cfg)
        return None

    def _phone_context(form: dict, request: Request, ctx: str = "") -> tuple[dict, str]:
        """The caller's number (or, on our outbound calls, who we called) plus any context we started it with."""
        outbound = str(form.get("Direction", "")).startswith("outbound")
        contact = str(form.get("To" if outbound else "From", "") or "")
        context = engine.automations.take_context(ctx or request.query_params.get("ctx", ""))
        if contact and re.fullmatch(r"\+?\d{7,15}", contact):
            context.setdefault("caller_phone", contact)
        return context, contact

    def _stream_url(path: str) -> str:
        return "wss://" + engine.public_url.split("://", 1)[-1] + path

    async def _read_start(websocket: WebSocket) -> dict | None:
        """Telephony streams send "connected", then "start"; return the whole start message."""
        try:
            for _ in range(5):
                msg = json.loads(await asyncio.wait_for(websocket.receive_text(), 10))
                if msg.get("event") == "start":
                    return msg
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
            pass
        return None

    async def _run_phone_call(websocket: WebSocket, pending, serializer, rate: int = 8000, on_transfer=None) -> None:
        from pipecat.transports.websocket.fastapi import FastAPIWebsocketParams, FastAPIWebsocketTransport

        agent = pending.agent
        try:
            graph = engine.load_agent(agent)
        except HTTPException:
            await websocket.close(code=1011)
            return
        transport = FastAPIWebsocketTransport(websocket, FastAPIWebsocketParams(
            audio_in_enabled=True, audio_out_enabled=True, add_wav_header=False, serializer=serializer))
        stt, tts = engine.speech_factory() if engine.speech_factory else (None, None)
        try:
            session = engine.add(engine.voice_session(graph, transport, rate=rate,
                                              stt=stt, tts=tts, llm=engine._make_llm(), variables=pending.context,
                                              tools=engine.tool_runner(channel={"kind": "phone", "to": pending.contact}),
                                              meta={"agent": agent, "channel": "phone", "contact": pending.contact}))
        except ConfigError as exc:
            log.warning("cannot start phone call: %s", exc)
            await websocket.close(code=1011)
            return

        session.on_transfer = on_transfer

        @transport.event_handler("on_client_disconnected")
        async def _gone(t, client):
            await session.stop()

        try:
            await session.run()
        except asyncio.CancelledError:
            # Stopping the call after the caller hung up cancels the pipeline; that's a normal end.
            if not session.finished.is_set():
                raise
        finally:
            if session.transfer_to is not None:
                # The carrier carries on once the stream closes (Twilio's <Redirect>, jambonz's actionHook).
                try:
                    await websocket.close()
                except Exception:  # noqa: BLE001 - already closed
                    pass

    # Twilio

    @app.post("/v1/twilio/voice")
    async def twilio_voice(request: Request, agent: str = ""):
        """Twilio's "A call comes in" webhook: {VPE_PUBLIC_URL}/v1/twilio/voice?agent=<folder>."""
        if not engine.twilio_auth_token or not engine.public_url:
            raise HTTPException(503, "Twilio calls need the Twilio auth token and a public address (--tunnel)")
        form = dict(parse_qsl((await _webhook_body(request)).decode("utf-8", "replace"), keep_blank_values=True))
        url = f"{engine.public_url}{request.url.path}" + (f"?{request.url.query}" if request.url.query else "")
        if not valid_signature(engine.twilio_auth_token, url, form, request.headers.get("x-twilio-signature", "")):
            raise HTTPException(403, "bad Twilio signature")
        problem = await _refusal(agent)
        if problem:
            log.warning("rejecting Twilio call: %s", problem)
            return _xml(reject_twiml("Sorry, this line can't take calls right now. Please try again later."))
        context, contact = _phone_context(form, request)
        token = engine.stream_tokens.issue(agent, form.get("CallSid", ""), context, contact)
        return _xml(stream_twiml(_stream_url("/v1/twilio/stream"), token, f"{engine.public_url}/v1/twilio/after"))

    @app.post("/v1/twilio/after")
    async def twilio_after(request: Request):
        """Where Twilio goes once our stream closes: connect a transfer, or hang up."""
        if not engine.twilio_auth_token or not engine.public_url:
            raise HTTPException(503, "Twilio isn't set up")
        form = dict(parse_qsl((await _webhook_body(request)).decode("utf-8", "replace"), keep_blank_values=True))
        url = f"{engine.public_url}{request.url.path}" + (f"?{request.url.query}" if request.url.query else "")
        if not valid_signature(engine.twilio_auth_token, url, form, request.headers.get("x-twilio-signature", "")):
            raise HTTPException(403, "bad Twilio signature")
        transfer = engine.transfers.get(form.get("CallSid"))
        return _xml(transfer.twiml() if transfer else hangup_twiml())

    @app.websocket("/v1/twilio/stream")
    async def twilio_stream(websocket: WebSocket):
        from pipecat.serializers.twilio import TwilioFrameSerializer

        await websocket.accept()
        msg = await _read_start(websocket)
        start = (msg or {}).get("start") or {}
        pending = engine.stream_tokens.redeem(start.get("customParameters", {}).get("t"), start.get("callSid")) if msg else None
        if pending is None:
            await websocket.close(code=1008)
            return
        serializer = TwilioFrameSerializer(
            stream_sid=start.get("streamSid", ""), call_sid=start.get("callSid"),
            account_sid=start.get("accountSid"), auth_token=engine.twilio_auth_token,
            params=TwilioFrameSerializer.InputParams(auto_hang_up=engine.twilio_hang_up))

        async def hand_off(transfer):
            engine.transfers.add(start.get("callSid", ""), transfer)
            serializer._params.auto_hang_up = False      # close the stream instead; Twilio then asks /v1/twilio/after

        await _run_phone_call(websocket, pending, serializer, on_transfer=hand_off)

    # Telnyx

    @app.post("/v1/telnyx/voice")
    async def telnyx_voice(request: Request, agent: str = ""):
        """A Telnyx TeXML application's voice URL: {VPE_PUBLIC_URL}/v1/telnyx/voice?agent=<folder>."""
        if not engine.cfg.telnyx_public_key or not engine.public_url:
            raise HTTPException(503, "Telnyx calls need the Telnyx public key and a public address (--tunnel)")
        body = await _webhook_body(request)
        if not telnyx.valid_signature(engine.cfg.telnyx_public_key, request.headers.get("telnyx-timestamp", ""),
                                      request.headers.get("telnyx-signature-ed25519", ""), body):
            raise HTTPException(403, "bad Telnyx signature")
        problem = await _refusal(agent)
        if problem:
            log.warning("rejecting Telnyx call: %s", problem)
            return _xml(telnyx.reject_texml("Sorry, this line can't take calls right now. Please try again later."))
        form = dict(parse_qsl(body.decode("utf-8", "replace"), keep_blank_values=True))
        context, contact = _phone_context(form, request)
        token = engine.stream_tokens.issue(agent, "", context, contact)   # Telnyx's stream "start" carries a different call id
        log.info("Telnyx call %s for %s", form.get("CallSid", "?"), agent)
        return _xml(telnyx.stream_texml(_stream_url("/v1/telnyx/stream"), token))

    @app.websocket("/v1/telnyx/stream")
    async def telnyx_stream(websocket: WebSocket):
        from pipecat.serializers.telnyx import TelnyxFrameSerializer

        await websocket.accept()
        msg = await _read_start(websocket)
        start = (msg or {}).get("start") or {}
        token = websocket.query_params.get("t") or telnyx.token_from_start(start)
        pending = engine.stream_tokens.redeem(token, None) if msg else None
        if pending is None:
            await websocket.close(code=1008)
            return
        api_key = engine.cfg.telnyx_api_key
        call_control_id = start.get("call_control_id")
        serializer = TelnyxFrameSerializer(
            stream_id=msg.get("stream_id", ""),
            outbound_encoding=(start.get("media_format") or {}).get("encoding") or "PCMU",
            inbound_encoding="PCMU", call_control_id=call_control_id, api_key=api_key or None,
            params=TelnyxFrameSerializer.InputParams(
                auto_hang_up=bool(api_key and call_control_id and engine.twilio_hang_up)))

        handed: list = []

        async def hand_off(transfer):
            if not (api_key and call_control_id):
                raise RuntimeError("Telnyx transfers need the Telnyx API key (Integrations → Telnyx)")
            handed.append(transfer)
            serializer._params.auto_hang_up = False      # the transfer takes the call once the agent has finished

        await _run_phone_call(websocket, pending, serializer, on_transfer=hand_off)
        if handed:
            # Only now: the agent's last line has played (the call's pipeline ended after it), so the caller
            # hears it before Telnyx connects them.
            import httpx

            async with httpx.AsyncClient(timeout=10, transport=engine.automations.phone_transport) as http:
                url = f"{telnyx.API}/calls/{call_control_id}/actions"
                r = await http.post(f"{url}/transfer", headers={"Authorization": f"Bearer {api_key}"},
                                    json=handed[0].telnyx_body())
                if r.status_code >= 300:
                    log.error("Telnyx refused the transfer to %s (%s); hanging up", handed[0].to, r.status_code)
                    await http.post(f"{url}/hangup", headers={"Authorization": f"Bearer {api_key}"}, json={})

    # jambonz (SIP trunks, phone systems and SBCs)

    async def _jambonz_body(request: Request) -> dict:
        body = await _webhook_body(request)
        if not jambonz.valid_signature(engine.cfg.jambonz_webhook_secret,
                                       request.headers.get("jambonz-signature", ""), body):
            raise HTTPException(403, "bad jambonz signature")
        try:
            data = json.loads(body or b"{}")
        except ValueError as exc:
            raise HTTPException(400, "jambonz sends JSON") from exc
        return data if isinstance(data, dict) else {}

    @app.post("/v1/jambonz/call")
    async def jambonz_call(request: Request, agent: str = ""):
        """A jambonz application's calling webhook: {VPE_PUBLIC_URL}/v1/jambonz/call?agent=<folder> (POST)."""
        if not engine.cfg.jambonz_webhook_secret or not engine.public_url:
            raise HTTPException(503, "jambonz calls need the application's webhook secret and a public address")
        call = await _jambonz_body(request)
        placed, ctx = jambonz.outbound_tag(call)     # a call we placed through the REST API names its agent
        agent = placed if placed and str(call.get("direction", "")).startswith("outbound") else agent
        problem = await _refusal(agent)
        if problem:
            log.warning("rejecting jambonz call: %s", problem)
            return jambonz.reject_verbs("Sorry, this line can't take calls right now. Please try again later.")
        context, contact = _phone_context({"Direction": call.get("direction", ""), "From": call.get("from", ""),
                                           "To": call.get("to", "")}, request, ctx)
        token = engine.stream_tokens.issue(agent, str(call.get("call_sid") or ""), context, contact)
        log.info("jambonz call %s for %s", call.get("call_sid", "?"), agent)
        return jambonz.listen_verbs(_stream_url("/v1/jambonz/stream"), token, f"{engine.public_url}/v1/jambonz/done",
                                    int(engine.cfg.jambonz_sample_rate))

    @app.post("/v1/jambonz/done")
    async def jambonz_done(request: Request):
        """The listen verb's actionHook (the stream ended) and the application's call status webhook."""
        call = await _jambonz_body(request)
        transfer = engine.transfers.get(str(call.get("call_sid") or ""))
        return transfer.jambonz_verbs() if transfer else [{"verb": "hangup"}]

    @app.websocket("/v1/jambonz/stream")
    async def jambonz_stream(websocket: WebSocket):
        await websocket.accept(subprotocol=jambonz.subprotocol(list(websocket.scope.get("subprotocols") or [])))
        try:          # first a JSON text frame with the call's details, then audio
            first = json.loads(await asyncio.wait_for(websocket.receive_text(), 10))
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError, KeyError):
            first = None
        if not isinstance(first, dict):
            await websocket.close(code=1008)
            return
        token = websocket.query_params.get("t") or str((first.get("metadata") or {}).get("t") or first.get("t") or "")
        sid = first.get("callSid") or first.get("call_sid")
        pending = engine.stream_tokens.redeem(token, str(sid) if sid else None)
        if pending is None:
            await websocket.close(code=1008)
            return
        try:
            rate = int(first.get("sampleRate") or jambonz.SAMPLE_RATE)
        except (TypeError, ValueError):
            rate = jambonz.SAMPLE_RATE
        rate = rate if rate in jambonz.RATES else jambonz.SAMPLE_RATE
        serializer = jambonz.JambonzFrameSerializer(jambonz.JambonzFrameSerializer.InputParams(
            jambonz_sample_rate=rate, hang_up=engine.twilio_hang_up))
        async def hand_off(transfer):
            engine.transfers.add(str(sid or ""), transfer)   # the stream ends; jambonz asks /v1/jambonz/done

        await _run_phone_call(websocket, pending, serializer, rate, on_transfer=hand_off)

    # -- tests: a model plays the caller ---------------------------------------

    @app.post("/v1/tests/caller", dependencies=auth)
    async def test_caller(body: dict[str, Any]):
        """The next thing a role-played caller says, for `vk test` files with a `caller:` block.

        Uses the engine's own model, so `vk` needs no model key of its own."""
        persona = str(body.get("persona") or "").strip()[:2000]
        goal = str(body.get("goal") or "").strip()[:500]
        if len(persona) < 5:
            raise HTTPException(422, "describe the caller (persona)")
        lines = body.get("transcript") or []
        if not isinstance(lines, list):
            raise HTTPException(422, "transcript must be a list of {who, text}")
        said = "\n".join(f"{'Caller' if str(x.get('who')) == 'caller' else 'Agent'}: {str(x.get('text') or '')[:1000]}"
                         for x in lines[-60:] if isinstance(x, dict))
        try:
            llm = engine._make_llm() or make_llm(engine.cfg)
        except ConfigError as exc:
            raise HTTPException(503, str(exc)) from exc
        instruction = "\n\n".join([
            "You are role-playing a CALLER phoning a business, to test its voice agent. You are not the agent.",
            f"Who you are: {persona}",
            f"What you want from this call: {goal}" if goal else "",
            "Speak like a real caller on the phone: one or two short sentences, plain words, no stage directions, "
            "no quotation marks. Answer the agent's questions. When it asks for details, make up plausible ones "
            "(a name, a day, a 555-01xx phone number); never use real people's details.",
            "When what you wanted is done, or the agent has said goodbye, or you would hang up, set done to true "
            "and line to null. Reply with one JSON object and nothing else.",
        ])
        schema = {"type": "object", "additionalProperties": False, "required": ["line", "done"],
                  "properties": {"line": {"type": ["string", "null"]}, "done": {"type": "boolean"}}}
        from pipecat.processors.aggregators.llm_context import LLMContext
        ctx = LLMContext(messages=[{"role": "user", "content": (said or "(the call has just connected; the agent "
                                                                         "hasn't spoken yet)") + "\n\nWhat does the caller say next?"}])
        try:
            text = await asyncio.wait_for(llm.run_inference(ctx, max_tokens=200, system_instruction=instruction,
                                                            response_schema=schema), 45)
        except Exception as exc:  # noqa: BLE001 - shown to the person running the tests
            raise HTTPException(502, f"the model couldn't play the caller: {exc}") from exc
        from .flow import parse_json_object
        out = parse_json_object(text) or {}
        line = str(out.get("line") or "").strip()[:500] or None
        return {"line": line, "done": bool(out.get("done")) or line is None}

    # -- users and roles -------------------------------------------------------

    @app.get("/v1/me", dependencies=auth)
    async def me(request: Request):
        return {**request.state.who, "auth": bool(engine.api_key)}

    @app.get("/v1/keys", dependencies=auth)
    async def list_keys():
        return {"keys": engine.keys.list(), "enabled": bool(engine.api_key), "roles": list(keys_module.ROLES)}

    @app.post("/v1/keys", dependencies=auth)
    async def create_key(body: dict[str, Any]):
        if not engine.api_key:
            raise HTTPException(409, "named keys need the owner key first: start the engine with VPE_API_KEY set "
                                     "(scripts/engine-up.sh --tunnel and Docker set one for you)")
        try:
            rec, key = engine.keys.create(str(body.get("name") or ""), str(body.get("role") or ""))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {**rec, "key": key}

    @app.delete("/v1/keys/{kid}", dependencies=auth)
    async def revoke_key(kid: str):
        if not engine.keys.revoke(kid):
            raise HTTPException(404, "no such key")
        return {"revoked": kid}

    # -- call history --------------------------------------------------------

    @app.get("/v1/calls", dependencies=auth)
    async def list_calls(request: Request, agent: str = "", channel: str = "", q: str = "", before: float | None = None,
                         limit: int = 50):
        _local_only(request)            # callers' details, like records: this computer (or VPE_REMOTE_EDITING)
        calls = await asyncio.to_thread(engine.history.list, agent=agent, channel=channel, query=q[:100],
                                        before=before, limit=limit)
        return {"calls": calls, "keeping": engine.cfg.call_history != "off",
                "days": int(engine.cfg.call_history_days or 30), "recordings": engine.cfg.call_recordings == "on"}

    @app.get("/v1/calls/{call_id}", dependencies=auth)
    async def get_call(call_id: str, request: Request):
        _local_only(request)
        call = await asyncio.to_thread(engine.history.get, call_id)
        if call is None:
            raise HTTPException(404, "no such call (calls older than the history setting are deleted)")
        return call

    @app.get("/v1/calls/{call_id}/recording", dependencies=auth)
    async def call_recording(call_id: str, request: Request):
        _local_only(request)
        path = engine.history.recording_path(call_id)
        if path is None:
            raise HTTPException(404, "this call has no recording")
        return FileResponse(path, media_type="audio/wav", filename=f"call-{call_id}.wav",
                            headers={"Cache-Control": "no-store"})

    @app.delete("/v1/calls/{call_id}", dependencies=auth)
    async def delete_call(call_id: str, request: Request):
        _local_only(request)
        if not await asyncio.to_thread(engine.history.delete, call_id):
            raise HTTPException(404, "no such call")
        return {"deleted": call_id}

    # -- automation: triggers, channels, outbound ------------------------------

    @app.get("/v1/automation", dependencies=auth)
    async def automation_overview():
        return engine.automations.overview()

    @app.post("/v1/agents/{slug}/triggers/{tid}/run", dependencies=auth)
    async def run_trigger(slug: str, tid: str, request: Request, body: dict[str, Any] | None = None):
        """Run a trigger now. dry_run (the default) only reports what would happen."""
        _local_only(request)
        engine.load_agent(slug)
        a = engine.automations.agents().get(slug)
        t = next((x for x in (a.triggers if a else []) if x["id"] == tid), None)
        if t is None:
            raise HTTPException(404, f"no working trigger {tid!r} in {slug} (save it, and check `vk lint`)")
        dry = _flag((body or {}).get("dry_run"), default=True)
        if t["type"] == "schedule":
            return {"results": await engine.automations.run_schedule(slug, t, dry_run=dry)}
        if t["type"] == "after_call":
            sample = {"summary": "This is a test note: a caller asked to book a cleaning next Tuesday morning.",
                      "details": "preferred day: Tuesday\npreferred time: morning", "agent": a.name, "channel": "phone",
                      "contact": "+15550100000", "transcript": "(test)", "session_id": "test", "ended_reason": "completed",
                      "duration_seconds": 0, "steps": "", "transferred_to": "", "caller_name": "Test Caller", "caller_phone": "+15550100000"}
            return {"results": [await engine.automations._send(slug, tid, t["send"], sample, dry_run=dry)]}
        raise HTTPException(400, "message triggers run when someone writes in")

    async def _api_trigger(path: str, request: Request):
        if not engine.api_key and not _is_local(request):
            raise HTTPException(403, "set VPE_API_KEY to use triggers from other computers")
        slug = engine.automations.api_agent(path)
        if slug is None:
            raise HTTPException(404, "no enabled API trigger with that path")
        try:
            body = await request.json()
        except ValueError:
            body = {}
        body = body if isinstance(body, dict) else {}
        channel = str(body.get("channel") or "call")
        to = str(body.get("to") or body.get("phone_number") or "")
        if not to:
            raise HTTPException(422, "give phone_number (for a call) or channel and to (for a message)")
        context = body.get("initial_context") if isinstance(body.get("initial_context"), dict) else {}
        dry_run = _flag(body.get("dry_run"), default=False)
        key = (request.headers.get("idempotency-key") or "").strip()
        if len(key) > 128:
            raise HTTPException(422, "Idempotency-Key is at most 128 characters")
        fingerprint = hashlib.sha256(json.dumps([path, body], sort_keys=True, default=str).encode()).hexdigest()
        if key and not dry_run:
            seen = idempotent.get(key)
            if seen is not None:
                if seen["fingerprint"] != fingerprint:
                    raise HTTPException(422, "this Idempotency-Key was already used for a different request")
                if seen["result"] is None:
                    raise HTTPException(409, "a request with this Idempotency-Key is still in progress")
                return _trigger_reply(seen["result"])
            idempotent.put(key, {"fingerprint": fingerprint, "result": None})
        try:
            result = await engine.automations.outbound(slug, "call" if channel == "call" else "message", to,
                                                       channel=channel, context=context, text=str(body.get("text") or ""),
                                                       source=f"api {path}", dry_run=dry_run)
        except BaseException:
            if key and not dry_run:
                idempotent.drop(key)
            raise
        if key and not dry_run:
            idempotent.put(key, {"fingerprint": fingerprint, "result": result})
        return _trigger_reply(result)

    def _trigger_reply(result: dict):
        if result.get("unknown"):
            raise HTTPException(504, result["detail"])          # outcome unknown: don't retry blindly
        if not result["ok"]:
            raise HTTPException(409, result["detail"])
        return result

    @app.post("/v1/public/agent/{path}", dependencies=auth)
    async def api_trigger(path: str, request: Request):
        """The API Trigger: start a call (phone_number) or a message (channel + to)."""
        return await _api_trigger(path, request)

    @app.post("/v1/public/agent/test/{path}", dependencies=auth)
    async def api_trigger_test(path: str, request: Request):
        return await _api_trigger(path, request)       # one folder: draft and published are the same

    @app.get("/v1/whatsapp/webhook", include_in_schema=False)
    async def whatsapp_verify(request: Request):
        q = request.query_params
        token = engine.cfg.whatsapp_verify_token
        if q.get("hub.mode") == "subscribe" and token and hmac.compare_digest(q.get("hub.verify_token", ""), token):
            return Response(q.get("hub.challenge", ""), media_type="text/plain")
        raise HTTPException(403, "verification failed")

    @app.post("/v1/whatsapp/webhook", include_in_schema=False)
    async def whatsapp_incoming(request: Request):
        body = await _webhook_body(request)
        if not engine.connectors.whatsapp_signature_ok(body, request.headers.get("x-hub-signature-256", "")):
            raise HTTPException(403, "bad signature (check the Meta app secret in Integrations)")
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise HTTPException(400, "not JSON") from exc
        for m in engine.connectors.whatsapp_messages(payload):
            asyncio.create_task(engine.automations.incoming(m))
        return {"ok": True}

    @app.post("/v1/oauth/google/start", dependencies=auth)
    async def google_start(request: Request):
        _local_only(request)
        host = request.url.hostname or ""
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise HTTPException(400, "open the engine at http://localhost to connect Google")
        redirect = f"http://{'localhost' if host == 'localhost' else '127.0.0.1'}:{request.url.port or 80}/v1/oauth/google/callback"
        try:
            return {"url": engine.connectors.google_start(redirect), "redirect_uri": redirect}
        except ConnectorError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/v1/oauth/google/callback", include_in_schema=False)
    async def google_callback(request: Request, state: str = "", code: str = "", error: str = ""):
        # A browser redirect from Google: no API key, but only a state we issued (once, 10 minutes) works.
        if error or not code:
            message, ok = f"Google sign-in didn't finish ({error or 'no code'}).", False
        else:
            try:
                account = await engine.connectors.google_finish(state, code)
                message, ok = f"Connected {account or 'your Google account'}. You can close this tab.", True
            except (ConnectorError, ConfigError) as exc:
                message, ok = str(exc), False
        page = (f"<!doctype html><meta charset=utf-8><title>Google</title><body style='font:16px system-ui;padding:40px'>"
                f"<p>{'✓' if ok else '✗'} {html.escape(message)}</p><p><a href='/#/integrations'>Back to Integrations</a></p>")
        return HTMLResponse(page, headers={"Cache-Control": "no-store"})

    @app.post("/v1/connectors/{service}/test", dependencies=auth)
    async def connector_test(service: str, request: Request):
        _local_only(request)
        return await engine.connectors.test(service)

    @app.post("/v1/tools/mcp/list", dependencies=auth)
    async def mcp_list(body: dict[str, Any], request: Request):
        """Ask an MCP server for its tools (the builder's Refresh)."""
        _local_only(request)
        from vk.tools import _address
        from .mcp import McpClient, McpError, to_definitions
        server = body.get("mcp") if isinstance(body.get("mcp"), dict) else {}
        problems: list[str] = []
        _address(server.get("url") or "", problems.append, lambda m: None)
        if problems:
            raise HTTPException(422, problems[0])
        runner = engine.tool_runner()
        try:
            url = runner._text(str(server.get("url")), {}, {}, secrets_only=True)
            headers = {str(k): runner._text(str(v), {}, {}, secrets_only=True) for k, v in (server.get("headers") or {}).items()}
            client = McpClient(url, headers, transport=engine.tool_transport)
            tools = to_definitions(await client.list_tools())
            await client.close()
        except (McpError, ToolError) as exc:
            raise HTTPException(502, runner.scrub(str(exc))) from exc
        return {"tools": tools}

    @app.get("/v1/agents/{slug}/export", dependencies=auth)
    async def export_agent(slug: str, target: str = "ces"):
        from vk import exporters
        engine.load_agent(slug)
        try:
            data, report = exporters.export(agentfs.load(engine.agents_dir / slug), target)
        except exporters.ExportError as exc:
            raise HTTPException(422, str(exc)) from exc
        name = f"{slug}-{'dialogflow-cx' if target == 'dfcx' else 'cx-agent-studio'}.zip"
        return Response(data, media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{name}"', "X-Export-Notes": str(len(report.notes)),
            "Cache-Control": "no-store"})

    return app


# Pipecat warnings that are normal for our browser calls. Hidden at the default
# "warning" level; they still show with --log-level info or debug.
QUIET_WARNINGS = (
    "No screen video transceiver is available",           # we never send screen share; checked on every connect
    "Media stream error while reading the audio",          # the browser's mic track ended: hang-up, reload, tab closed
)


def _log_filter(level: str):
    verbose = level.upper() in ("TRACE", "DEBUG", "INFO")

    def keep(record) -> bool:
        return verbose or not (record["level"].name == "WARNING" and record["message"].startswith(QUIET_WARNINGS))
    return keep


def _configure_logging(level: str) -> None:
    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level=level.upper(), filter=_log_filter(level))
    logging.basicConfig(level=level.upper(), format="%(levelname)s %(name)s: %(message)s")


def _engine_from_env() -> Engine:
    store = SettingsStore()
    host = os.environ.get("VPE_HOST", "127.0.0.1")
    return Engine(Path(os.environ.get("VPE_AGENTS_DIR", "agents")).resolve(), store.config(),
                  api_key=os.environ.get("VPE_API_KEY", "").strip(),
                  max_sessions=int(os.environ.get("VPE_MAX_SESSIONS", "20")),
                  public_url=os.environ.get("VPE_PUBLIC_URL", "").strip(), store=store, listen_host=host,
                  container=os.environ.get("VPE_CONTAINER", "") == "1",
                  remote_editing=os.environ.get("VPE_REMOTE_EDITING", "").strip().lower() == "on")


def app_from_env() -> FastAPI:
    """Factory for auto-reload: uvicorn rebuilds the app from the environment after each code change."""
    _configure_logging(os.environ.get("VPE_LOG_LEVEL", "warning"))
    return create_app(_engine_from_env())


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vpengine", description="Run voice agents from agent folders.")
    p.add_argument("--agents", default=os.environ.get("VPE_AGENTS_DIR", "agents"),
                   help="directory of agent folders (default: ./agents or VPE_AGENTS_DIR)")
    p.add_argument("--host", default=os.environ.get("VPE_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("VPE_PORT", "7860")))
    p.add_argument("--log-level", default=os.environ.get("VPE_LOG_LEVEL", "warning"))
    p.add_argument("--reload", action="store_true",
                   help="restart automatically when the engine's code changes (calls in progress are dropped)")
    args = p.parse_args(argv)

    api_key = os.environ.get("VPE_API_KEY", "").strip()
    public_url = os.environ.get("VPE_PUBLIC_URL", "").strip()
    if args.host not in ("127.0.0.1", "localhost", "::1") and not api_key:
        print("refusing to listen beyond this computer without VPE_API_KEY set", file=sys.stderr)
        return 2
    if public_url and not api_key:
        # A tunnel makes every endpoint public, not just Twilio's.
        print("VPE_PUBLIC_URL is set (a tunnel), so VPE_API_KEY is required to protect the chat and call APIs",
              file=sys.stderr)
        return 2
    if public_url and not public_url.startswith("https://"):
        print("VPE_PUBLIC_URL must start with https://", file=sys.stderr)
        return 2
    _configure_logging(args.log_level)
    store = SettingsStore()
    try:
        cfg = store.config()
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    container = os.environ.get("VPE_CONTAINER", "") == "1"
    engine = Engine(Path(args.agents).resolve(), cfg, api_key=api_key,
                    max_sessions=int(os.environ.get("VPE_MAX_SESSIONS", "20")), public_url=public_url,
                    store=store, listen_host=args.host, container=container,
                    remote_editing=os.environ.get("VPE_REMOTE_EDITING", "").strip().lower() == "on")
    found = engine.list_agents()
    print(f"voice engine {__version__}: {len(found)} agent(s) in {engine.agents_dir}")
    print(f"  model: {cfg.llm} {cfg.model}" + ("" if cfg.llm_ready else "  (no key set: chats will fail)"))
    d = cfg.describe()
    print(f"  voice: {d['stt']} + {d['tts']} ({d['tts_voice']})" if cfg.voice_ready
          else "  voice: off (set up speech in Integrations)")
    print(f"  settings: {store.path}")
    if public_url:
        print(f"  phone: Twilio webhook {public_url}/v1/twilio/voice?agent=<folder>")
        print(f"         Telnyx voice URL {public_url}/v1/telnyx/voice?agent=<folder>")
        print(f"         jambonz calling webhook {public_url}/v1/jambonz/call?agent=<folder>")
    if container:
        print(f"  open http://localhost:{os.environ.get('VPE_PUBLISHED_PORT', args.port)} "
              "(the page asks once for the API key: VPE_DOCKER_API_KEY in .env)")
    else:
        print(f"  open http://{'localhost' if args.host == '127.0.0.1' else args.host}:{args.port}")

    import uvicorn
    if args.reload:
        # Hand the settings to the reloading worker through the environment.
        os.environ.update(VPE_AGENTS_DIR=str(engine.agents_dir), VPE_HOST=args.host, VPE_LOG_LEVEL=args.log_level)
        print("  auto-reload: on (code changes apply by themselves; settings and agents never need a restart)")
        uvicorn.run("vpengine.server:app_from_env", factory=True, reload=True, host=args.host, port=args.port,
                    reload_dirs=[str(Path(__file__).resolve().parent)], reload_includes=["*.py"],
                    log_level=args.log_level.lower())
    else:
        uvicorn.run(create_app(engine), host=args.host, port=args.port, log_level=args.log_level.lower())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
