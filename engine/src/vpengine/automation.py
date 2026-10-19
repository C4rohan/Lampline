"""Triggers at run time: message channels, after-call actions, schedules, the
API trigger, and outbound calls and messages.

Definitions live in each agent folder (vk.triggers, plus the API Trigger and
Webhook nodes). This module watches them while the engine runs:

- message channels: Telegram (long polling), email (checked every minute) and
  WhatsApp (Meta's webhook) start or continue a text conversation per sender;
- after a conversation ends, after_call triggers and Webhook nodes send notes;
- schedules fire on their cron minute in the business's time zone;
- POST /v1/public/agent/<trigger_path> starts a call or a message.

Outbound contact is refused unless it is turned on (Settings: outbound), and
then respects quiet hours, a daily limit per agent and a 10-minute gap per
person. Every attempt is logged (recipients masked) for the Triggers panel.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import secrets as _secrets
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from vk import agentfs
from vk import triggers as spec
from vk.tools import TEMPLATE

from .connectors import ConnectorError, Incoming, normalize_email, normalize_phone
from .graph import render
from .services import make_llm

if TYPE_CHECKING:
    from .server import Engine
    from .session import Session

log = logging.getLogger("vpengine.automation")

IDLE_MINUTES = 30            # a message conversation ends after this long without a reply
RECIPIENT_GAP = 600          # seconds between outbound contacts to the same person from one agent
LOG_SIZE = 300


class OutboundRefused(RuntimeError):
    pass


def mask(contact: str) -> str:
    c = str(contact or "")
    if "@" in c:
        name, _, domain = c.partition("@")
        return f"{name[:1]}…@{domain}"
    return f"…{c[-4:]}" if len(c) > 4 else "…"


@dataclass
class AgentTriggers:
    slug: str
    name: str
    triggers: list[dict]
    api_paths: list[str]                  # API Trigger nodes (enabled)
    webhooks: list[dict]                  # Webhook nodes (enabled, with an endpoint)
    mtime: float = 0.0
    error: str = ""


@dataclass
class Conversation:
    channel: str
    sender: str
    slug: str
    session: Any
    last: float = field(default_factory=time.time)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    thread: dict = field(default_factory=dict)


class Automations:
    def __init__(self, engine: "Engine", state_path: Path | None = None):
        self.engine = engine
        self.state_path = state_path
        self.state = self._load_state()
        self.log: deque[dict] = deque(self.state.pop("log", [])[-LOG_SIZE:], maxlen=LOG_SIZE)
        self._agents: dict[str, AgentTriggers] = {}
        self.conversations: dict[tuple[str, str], Conversation] = {}
        self.contexts: dict[str, tuple[dict, float]] = {}       # outbound call -> context for the session
        self._tasks: list[asyncio.Task] = []
        self.transport: httpx.AsyncBaseTransport | None = None   # tests: webhook and recipient-list requests
        self.phone_transport: httpx.AsyncBaseTransport | None = None  # tests: Twilio/Telnyx REST

    # ------------------------------------------------------------------ state

    def _load_state(self) -> dict:
        if self.state_path and self.state_path.is_file():
            try:
                data = json.loads(self.state_path.read_text())
                return data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                return {}
        return {}

    def _save_state(self) -> None:
        if not self.state_path:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({**self.state, "log": list(self.log)}, indent=1))
            tmp.chmod(0o600)
            tmp.replace(self.state_path)
        except OSError as exc:
            log.warning("can't save automation state: %s", exc)

    def record(self, agent: str, trigger: str, what: str, ok: bool, detail: str = "", contact: str = "",
               dry_run: bool = False) -> dict:
        entry = {"at": dt.datetime.now(self.engine.cfg.zone).isoformat(timespec="seconds"), "agent": agent,
                 "trigger": trigger, "what": what, "ok": ok, "detail": detail[:300], "to": mask(contact) if contact else "",
                 "dry_run": dry_run}
        if not dry_run:
            self.log.append(entry)
            self._save_state()
        (log.info if ok else log.warning)("%s %s %s: %s %s", agent, trigger, what, "ok" if ok else "failed", detail)
        return entry

    # ------------------------------------------------------------------ definitions

    def agents(self) -> dict[str, AgentTriggers]:
        """Triggers of every agent folder, re-read when an agent.yaml changes."""
        root = self.engine.agents_dir
        seen = set()
        if root.is_dir():
            for d in sorted(root.iterdir()):
                f = d / agentfs.AGENT_FILE
                if not f.is_file():
                    continue
                seen.add(d.name)
                mtime = f.stat().st_mtime
                cached = self._agents.get(d.name)
                if cached and cached.mtime == mtime:
                    continue
                try:
                    a = agentfs.load(d)
                    nodes = (a.definition or {}).get("nodes") or []
                    api = [str((n.get("data") or {}).get("trigger_path") or "") for n in nodes
                           if n.get("type") == "trigger" and (n.get("data") or {}).get("enabled", True)]
                    hooks = [n.get("data") or {} for n in nodes if n.get("type") == "webhook"
                             and (n.get("data") or {}).get("enabled", True) and (n.get("data") or {}).get("endpoint_url")]
                    good = [t for t in a.triggers if isinstance(t, dict) and not [m for s, m in spec.problems(t) if s == "error"]]
                    bad = len(a.triggers) - len(good)
                    self._agents[d.name] = AgentTriggers(d.name, a.name, good, [p for p in api if p], hooks, mtime,
                                                         f"{bad} trigger(s) have errors; run vk lint" if bad else "")
                except Exception as exc:  # noqa: BLE001 - one broken folder shouldn't stop the others
                    self._agents[d.name] = AgentTriggers(d.name, d.name, [], [], [], mtime, str(exc))
        for gone in set(self._agents) - seen:
            del self._agents[gone]
        return self._agents

    def channel_agent(self, channel: str) -> str | None:
        for slug, a in self.agents().items():
            if any(t["type"] == "message" and t.get("channel") == channel and spec.enabled(t) for t in a.triggers):
                return slug
        return None

    def overview(self) -> dict:
        c = self.engine.cfg
        channels = {ch: self.channel_agent(ch) for ch in spec.CHANNELS}
        return {"outbound": c.outbound == "on", "quiet_hours": c.quiet_hours, "daily_cap": int(c.outbound_daily_cap or 50),
                "timezone": str(c.zone), "channels": channels, "connected": self.engine.connectors.status(),
                "whatsapp_webhook": f"{self.engine.public_url}/v1/whatsapp/webhook" if self.engine.public_url else None,
                "log": list(self.log)[-100:][::-1],
                "agents": {slug: {"name": a.name, "triggers": a.triggers, "api_paths": a.api_paths,
                                  "webhooks": len(a.webhooks), "error": a.error} for slug, a in self.agents().items()}}

    # ------------------------------------------------------------------ running loops

    def start(self) -> None:
        if self._tasks:
            return
        self._tasks = [asyncio.create_task(self._loop(self._scheduler, 20), name="schedules"),
                       asyncio.create_task(self._telegram_loop(), name="telegram"),
                       asyncio.create_task(self._loop(self._email_check, 60), name="email"),
                       asyncio.create_task(self._loop(self._expire, 60), name="idle")]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []

    async def _loop(self, fn, every: float) -> None:
        while True:
            try:
                await fn()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the loop alive
                log.exception("%s failed", fn.__name__)
            await asyncio.sleep(every)

    async def _telegram_loop(self) -> None:
        offset = self.state.get("telegram_offset")
        while True:
            try:
                if not self.engine.cfg.telegram_bot_token or not self.channel_agent("telegram"):
                    await asyncio.sleep(15)
                    continue
                messages, offset = await self.engine.connectors.telegram_updates(offset)
                if offset != self.state.get("telegram_offset"):
                    self.state["telegram_offset"] = offset
                    self._save_state()
                for m in messages:
                    asyncio.create_task(self.incoming(m))
            except asyncio.CancelledError:
                raise
            except ConnectorError as exc:
                log.warning("telegram: %s", exc)
                await asyncio.sleep(30)
            except Exception:  # noqa: BLE001
                log.exception("telegram polling failed")
                await asyncio.sleep(30)

    async def _email_check(self) -> None:
        if not self.engine.cfg.email_ready or not self.channel_agent("email"):
            return
        for m in await self.engine.connectors.fetch_email():
            asyncio.create_task(self.incoming(m))

    async def _expire(self) -> None:
        now = time.time()
        for key, conv in list(self.conversations.items()):
            if conv.session.flow.ended or conv.session.finished.is_set():
                self.conversations.pop(key, None)
            elif now - conv.last > IDLE_MINUTES * 60 and not conv.lock.locked():
                self.conversations.pop(key, None)
                await conv.session.close()       # ends it: after-call triggers run

    # ------------------------------------------------------------------ message channels

    async def incoming(self, m: Incoming) -> list[str]:
        """A message arrived: continue (or start) that person's conversation and reply."""
        slug = self.channel_agent(m.channel)
        if not slug:
            return []
        key = (m.channel, m.sender)
        conv = self.conversations.get(key)
        replies: list[str] = []
        try:
            if conv is None or conv.session.flow.ended or conv.session.finished.is_set():
                conv, opening = await self._open(slug, m.channel, m.sender, self._contact_context(m), "message")
                replies += opening
            conv.thread = m.thread or conv.thread
            async with conv.lock:
                conv.last = time.time()
                events = await conv.session.send(m.text)
            replies += [e["text"] for e in events if e.get("type") == "bot_speech" and not e.get("filler") and e.get("text")]
            text = "\n\n".join(r.strip() for r in replies if r.strip())
            if text:
                await self.engine.connectors.reply(m.channel, m.sender, text, conv.thread)
            if conv.session.flow.ended:
                self.conversations.pop(key, None)
            return replies
        except Exception as exc:  # noqa: BLE001 - logged for the Triggers panel
            self.record(slug, f"{m.channel} message", "reply", False, str(exc), m.sender)
            return replies

    def _contact_context(self, m: Incoming) -> dict:
        ctx: dict[str, Any] = {"channel": m.channel, "contact": m.sender}
        if m.name:
            ctx["caller_name"] = m.name
        if m.channel == "whatsapp":
            ctx["caller_phone"] = m.sender
        elif m.channel == "email":
            ctx["caller_email"] = m.sender
        elif m.channel == "telegram":
            ctx["telegram_chat_id"] = m.sender
        return ctx

    async def _open(self, slug: str, channel: str, sender: str, context: dict, source: str) -> tuple[Conversation, list[str]]:
        graph = self.engine.load_agent(slug)
        session = self.engine.new_text(graph, context, channel={"kind": channel, "to": sender},
                                       meta={"agent": slug, "channel": channel, "contact": sender, "source": source})
        events = await session.open()
        conv = Conversation(channel, sender, slug, session)
        self.conversations[(channel, sender)] = conv
        return conv, [e["text"] for e in events if e.get("type") == "bot_speech" and not e.get("filler") and e.get("text")]

    # ------------------------------------------------------------------ after a conversation

    async def after_call(self, session: "Session") -> list[dict]:
        meta = getattr(session, "meta", {}) or {}
        slug = meta.get("agent")
        a = self.agents().get(slug or "")
        if a is None:
            return []
        channel = meta.get("channel", "web")
        ended = next((e for e in reversed(session.events) if e.get("type") == "call_ended"), {})
        variables = dict(session.flow.variables)
        reached_end = ended.get("reason") not in (None, "disconnected") or any(
            session.graph.steps[i].kind == "end" for i in session.flow.visited if i in session.graph.steps)
        jobs: list[tuple[str, dict]] = []
        for t in a.triggers:
            if t["type"] != "after_call" or not spec.enabled(t):
                continue
            if channel not in (t.get("channels") or spec.DEFAULT_CHANNELS):
                continue
            when = t.get("when") or "always"
            if when == "reached_end" and not reached_end:
                continue
            if when.startswith("collected:") and variables.get(when.split(":", 1)[1]) in (None, ""):
                continue
            jobs.append((t["id"], t["send"]))
        if channel in spec.DEFAULT_CHANNELS:      # Webhook nodes run after real conversations
            for hook in a.webhooks:
                jobs.append((str(hook.get("name") or "webhook"), {"via": "webhook-node", **hook}))
        if not jobs:
            return []
        values = await self._call_values(session, meta, ended, variables,
                                         need_summary=any("summary" in json.dumps(j[1]) for j in jobs))
        results = []
        for tid, send in jobs:
            results.append(await self._send(slug, tid, send, values))
        return results

    async def _call_values(self, session, meta: dict, ended: dict, variables: dict, need_summary: bool) -> dict:
        transcript = "\n".join(f"{'Caller' if e['type'] == 'user_speech' else 'Agent'}: {e.get('text', '')}"
                               for e in session.events if e.get("type") in ("user_speech", "bot_speech") and not e.get("filler"))
        shown = {k: v for k, v in variables.items() if k not in ("channel", "contact") and v not in (None, "")
                 and k not in (session.graph.context or {})}
        details = "\n".join(f"{k.replace('_', ' ')}: {v}" for k, v in shown.items()) or "(nothing collected)"
        values = {**variables, "transcript": transcript[:20000], "details": details, "agent": session.graph.name,
                  "channel": meta.get("channel", "web"), "contact": meta.get("contact", ""),
                  "ended_reason": ended.get("reason", ""), "session_id": session.id,
                  "duration_seconds": int(time.time() - session.created),
                  "steps": ", ".join(session.graph.steps[i].name for i in session.flow.visited if i in session.graph.steps)}
        values["summary"] = await self._summary(session, transcript) if need_summary else ""
        if not values["summary"]:
            values["summary"] = f"Conversation with {session.graph.name} ({values['channel']}). Collected: {details}"
        return values

    async def _summary(self, session, transcript: str) -> str:
        if not transcript.strip():
            return ""
        try:
            from pipecat.processors.aggregators.llm_context import LLMContext
            llm = self.engine._make_llm() or make_llm(self.engine.cfg)
            ctx = LLMContext(messages=[{"role": "user", "content": transcript[-12000:]}])
            text = await asyncio.wait_for(llm.run_inference(
                ctx, max_tokens=300, system_instruction="Summarize this phone or chat conversation for the business in "
                "two or three plain sentences: what the person wanted, what was done, and anything to follow up."), 30)
            return str(text or "").strip()[:1500]
        except Exception as exc:  # noqa: BLE001 - the details are sent instead
            log.info("summary failed: %s", exc)
            return ""

    async def _send(self, slug: str, tid: str, send: dict, values: dict, dry_run: bool = False) -> dict:
        via = send.get("via")
        try:
            if via in ("webhook", "webhook-node"):
                return await self._webhook(slug, tid, send, values, dry_run)
            to = render(str(send.get("to") or ""), values).strip()
            text = render(str(send.get("text") or ""), values).strip()
            subject = render(str(send.get("subject") or f"From {values.get('agent', '')}"), values).strip()
            if not to:
                return self.record(slug, tid, f"send {via}", False, "no recipient (send.to was empty)", dry_run=dry_run)
            if dry_run:
                return self.record(slug, tid, f"send {via}", True, f"would send: {text[:200]}", to, dry_run=True)
            c = self.engine.connectors
            if via == "email":
                await c.send_email(to, subject, text)
            elif via == "whatsapp":
                await c.send_whatsapp(to, text)
            else:
                await c.send_telegram(to, text)
            return self.record(slug, tid, f"send {via}", True, "", to)
        except (ConnectorError, httpx.HTTPError, ValueError) as exc:
            return self.record(slug, tid, f"send {via}", False, str(exc), dry_run=dry_run)

    async def _webhook(self, slug: str, tid: str, send: dict, values: dict, dry_run: bool) -> dict:
        secrets = self.engine.secrets()

        def fill(text: str, *, only_secrets: bool = False) -> str:
            def repl(m: re.Match) -> str:
                name = m.group(1)
                if name.startswith("secrets."):
                    if name[8:] not in secrets:
                        raise ValueError(f"secret {name[8:]} is not set")
                    return secrets[name[8:]]
                if only_secrets:
                    raise ValueError("the webhook address can only use secrets")
                return str(_lookup(values, name))
            return TEMPLATE.sub(repl, text)

        def fill_value(v: Any) -> Any:
            if isinstance(v, str):
                whole = re.fullmatch(r"\{\{\s*([A-Za-z_][\w.]*)\s*\}\}", v.strip())
                if whole and not whole.group(1).startswith("secrets."):
                    return _lookup(values, whole.group(1))
                return TEMPLATE.sub(lambda m: "" if m.group(1).startswith("secrets.") else str(_lookup(values, m.group(1))), v)
            if isinstance(v, dict):
                return {k: fill_value(x) for k, x in v.items()}
            if isinstance(v, list):
                return [fill_value(x) for x in v]
            return v

        if send.get("via") == "webhook-node":       # a Webhook node
            url, method = str(send.get("endpoint_url") or ""), str(send.get("http_method") or "POST").upper()
            headers = {str(h.get("key")): str(h.get("value")) for h in send.get("custom_headers") or [] if isinstance(h, dict) and h.get("key")}
            payload = send.get("payload_template") or {"call_id": "{{workflow_run_id}}", "gathered_context": "{{gathered_context}}"}
        else:
            url, method = str(send.get("url") or ""), str(send.get("method") or "POST").upper()
            headers = {str(k): str(v) for k, v in (send.get("headers") or {}).items()}
            payload = send.get("payload") or {"agent": "{{agent}}", "session_id": "{{session_id}}", "channel": "{{channel}}",
                                              "summary": "{{summary}}", "variables": "{{gathered_context}}"}
        url = fill(url, only_secrets=True)
        if not url.lower().startswith(("https://", "http://")):
            return self.record(slug, tid, "webhook", False, "the webhook address isn't an http(s) URL", dry_run=dry_run)
        headers = {k: fill(v) for k, v in headers.items()}
        body = fill_value(payload)
        host = httpx.URL(url).host
        if dry_run:
            return self.record(slug, tid, "webhook", True, f"would {method} {host}: {json.dumps(body)[:200]}", dry_run=True)
        async with httpx.AsyncClient(timeout=10, follow_redirects=False, transport=self.transport) as http:
            r = await http.request(method, url, headers=headers, json=body)
        ok = 200 <= r.status_code < 300
        return self.record(slug, tid, "webhook", ok, f"{method} {host} → {r.status_code}")

    # ------------------------------------------------------------------ outbound

    def _check_outbound(self, slug: str, contact: str, *, dry_run: bool = False) -> None:
        c = self.engine.cfg
        if c.outbound != "on":
            raise OutboundRefused("outbound calls and messages are off (Integrations → Automation)")
        now = dt.datetime.now(c.zone)
        if c.quiet_hours:
            a, b = (dt.time.fromisoformat(x) for x in c.quiet_hours.split("-"))
            t = now.time()
            if (a <= t or t < b) if a > b else (a <= t < b):
                raise OutboundRefused(f"quiet hours ({c.quiet_hours})")
        day = now.date().isoformat()
        counts = self.state.setdefault("counts", {})
        if counts.get("day") != day:
            counts.clear()
            counts["day"] = day
        if counts.get(slug, 0) >= int(c.outbound_daily_cap or 50):
            raise OutboundRefused(f"the daily limit of {c.outbound_daily_cap} outbound contacts for this agent was reached")
        last = self.state.setdefault("last_contact", {}).get(f"{slug}|{contact}", 0)
        if time.time() - last < RECIPIENT_GAP:
            raise OutboundRefused("this person was contacted by this agent in the last 10 minutes")

    def _count(self, slug: str, contact: str) -> None:
        """Only contacts that went out count toward the limits."""
        counts = self.state.setdefault("counts", {})
        counts[slug] = counts.get(slug, 0) + 1
        last = self.state.setdefault("last_contact", {})
        last[f"{slug}|{contact}"] = time.time()
        self.state["last_contact"] = {k: v for k, v in last.items() if time.time() - v < 86400}

    async def outbound(self, slug: str, action: str, to: str, *, channel: str = "", context: dict | None = None,
                       text: str = "", source: str = "api", dry_run: bool = False) -> dict:
        """Start a call or a message conversation with someone."""
        context = {k: v for k, v in (context or {}).items() if isinstance(k, str) and re.fullmatch(r"[A-Za-z_]\w{0,63}", k)}
        what = "call" if action == "call" else f"{channel} message"
        try:
            self.engine.load_agent(slug)
            if action == "call":
                contact = "+" + normalize_phone(to)
            elif channel == "email":
                contact = normalize_email(to)
            elif channel == "whatsapp":
                contact = "+" + normalize_phone(to)
            elif channel == "telegram":
                contact = str(to).strip()
            else:
                raise ValueError("channel must be telegram, whatsapp or email")
            self._check_outbound(slug, contact, dry_run=dry_run)
            if dry_run:
                return self.record(slug, source, what, True, "would start" + (f" with: {render(text, context)[:150]}" if text else ""),
                                   contact, dry_run=True)
            if action == "call":
                detail = await self._call(slug, contact, context)
                self._count(slug, contact)
                return self.record(slug, source, what, True, detail, contact)
            conv, opening = await self._open(slug, channel, contact, {**context, "channel": channel, "contact": contact,
                                                                      **({"caller_phone": contact} if channel == "whatsapp" else {}),
                                                                      **({"caller_email": contact} if channel == "email" else {})},
                                             source)
            first = render(text, conv.session.flow.variables).strip() if text else "\n\n".join(opening)
            if text:
                conv.session.context.add_message({"role": "assistant", "content": first})
            await self.engine.connectors.reply(channel, contact, first or "Hello!")
            self._count(slug, contact)
            return self.record(slug, source, what, True, "", contact)
        except (OutboundRefused, ConnectorError, ValueError) as exc:
            return self.record(slug, source, what, False, str(exc), to, dry_run=dry_run)
        except Exception as exc:  # noqa: BLE001
            log.exception("outbound failed")
            return self.record(slug, source, what, False, f"{exc.__class__.__name__}: {exc}", to, dry_run=dry_run)

    async def _call(self, slug: str, number: str, context: dict) -> str:
        e, c = self.engine, self.engine.cfg
        if not e.public_url:
            raise ValueError("outbound calls need a public address (start the engine with a tunnel)")
        token = _secrets.token_urlsafe(18)
        now = time.time()
        self.contexts = {k: v for k, v in self.contexts.items() if v[1] > now}
        self.contexts[token] = ({**context, "caller_phone": number}, now + 600)
        async with httpx.AsyncClient(timeout=15, transport=self.phone_transport, follow_redirects=False) as http:
            if c.twilio_account_sid and c.twilio_auth_token and c.twilio_from_number:
                url = f"{e.public_url}/v1/twilio/voice?agent={slug}&ctx={token}"
                r = await http.post(f"https://api.twilio.com/2010-04-01/Accounts/{c.twilio_account_sid}/Calls.json",
                                    auth=(c.twilio_account_sid, c.twilio_auth_token),
                                    data={"To": number, "From": c.twilio_from_number, "Url": url, "Method": "POST"})
                if r.status_code >= 400:
                    raise ValueError(f"Twilio refused the call ({r.status_code}): {str(r.json().get('message', ''))[:150]}")
                return f"Twilio call {r.json().get('sid', '')}"
            if c.telnyx_api_key and c.telnyx_texml_app_id and c.telnyx_from_number:
                url = f"{e.public_url}/v1/telnyx/voice?agent={slug}&ctx={token}"
                r = await http.post(f"https://api.telnyx.com/v2/texml/calls/{c.telnyx_texml_app_id}",
                                    headers={"Authorization": f"Bearer {c.telnyx_api_key}"},
                                    json={"To": number, "From": c.telnyx_from_number, "Url": url})
                if r.status_code >= 400:
                    raise ValueError(f"Telnyx refused the call ({r.status_code})")
                return "Telnyx call started"
        raise ValueError("outbound calls need Twilio (account SID, auth token, number) or Telnyx (API key, TeXML app, number)")

    def take_context(self, token: str) -> dict:
        ctx, expiry = self.contexts.pop(token or "", ({}, 0))
        return ctx if expiry > time.time() else {}

    # ------------------------------------------------------------------ schedules and the API trigger

    async def _scheduler(self) -> None:
        now = dt.datetime.now(self.engine.cfg.zone).replace(second=0, microsecond=0)
        fired = self.state.setdefault("fired", {})
        stamp = now.isoformat()
        for slug, a in self.agents().items():
            for t in a.triggers:
                if t["type"] != "schedule" or not spec.enabled(t):
                    continue
                key = f"{slug}/{t['id']}"
                if fired.get(key) == stamp or not spec.cron_matches(t["cron"], now):
                    continue
                fired[key] = stamp
                self._save_state()
                asyncio.create_task(self.run_schedule(slug, t))

    async def recipients(self, t: dict) -> list[dict]:
        out = [r for r in t.get("recipients") or []]
        src = t.get("recipients_from")
        if src:
            secrets = self.engine.secrets()
            fill = lambda s: TEMPLATE.sub(lambda m: secrets.get(m.group(1)[8:], "") if m.group(1).startswith("secrets.") else "", s)  # noqa: E731
            url = fill(str(src.get("url") or ""))
            async with httpx.AsyncClient(timeout=15, follow_redirects=False, transport=self.transport) as http:
                r = await http.get(url, headers={k: fill(str(v)) for k, v in (src.get("headers") or {}).items()})
            if r.status_code >= 400:
                raise ValueError(f"the recipient list answered {r.status_code}")
            data = r.json()
            data = data.get("recipients", data) if isinstance(data, dict) else data
            if not isinstance(data, list):
                raise ValueError("the recipient list must be a JSON array of {to, context}")
            out += [x for x in data[:500] if isinstance(x, dict) and x.get("to")]
        return out

    async def run_schedule(self, slug: str, t: dict, dry_run: bool = False) -> list[dict]:
        try:
            people = await self.recipients(t)
        except (httpx.HTTPError, ValueError) as exc:
            return [self.record(slug, t["id"], "recipient list", False, str(exc), dry_run=dry_run)]
        results = []
        for r in people:
            results.append(await self.outbound(slug, t["action"], str(r["to"]), channel=t.get("channel", ""),
                                               context=r.get("context") or {}, text=t.get("text") or "",
                                               source=t["id"], dry_run=dry_run))
            if not dry_run:
                await asyncio.sleep(1)           # pace calls and messages
        if not people:
            results.append(self.record(slug, t["id"], "schedule", True, "no recipients", dry_run=dry_run))
        return results

    def api_agent(self, path: str) -> str | None:
        for slug, a in self.agents().items():
            if path in a.api_paths:
                return slug
        return None


def _lookup(values: dict, name: str) -> Any:
    """{{gathered_context.x}}, {{initial_context.x}} and {{workflow_run_id}} (the Webhook node's names) or our own."""
    if name == "workflow_run_id":
        return values.get("session_id", "")
    if name in ("gathered_context", "initial_context"):
        return {k: v for k, v in values.items() if k not in ("transcript",)}
    if name.startswith(("gathered_context.", "initial_context.")):
        return values.get(name.split(".", 1)[1], "")
    if name == "cost_info.call_duration_seconds":
        return values.get("duration_seconds", 0)
    v = values.get(name, "")
    return "" if v is None else v
