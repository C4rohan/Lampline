"""Connections to other services: Google Calendar and Gmail, email (SMTP/IMAP),
Telegram and WhatsApp. Agents use them as connector tools (vk.connectors) and
as message channels; triggers use them to send after-call notes and reminders.

Keys come from Settings (Integrations page or environment), never from agent
folders, and never appear in events or errors.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import email.utils
import hashlib
import hmac
import json
import re
import secrets as _secrets
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Callable
from urllib.parse import quote, urlencode

import httpx

from .services import Config

GOOGLE_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_SCOPES = ("openid", "email",
                 "https://www.googleapis.com/auth/calendar.events",
                 "https://www.googleapis.com/auth/calendar.freebusy",
                 "https://www.googleapis.com/auth/gmail.send",
                 "https://www.googleapis.com/auth/gmail.modify")
CALENDAR = "https://www.googleapis.com/calendar/v3"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
TELEGRAM = "https://api.telegram.org"
GRAPH = "https://graph.facebook.com/v21.0"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_WHATSAPP_SEEN: dict[str, float] = {}     # WhatsApp message ids already handled (Meta redelivers)


class ConnectorError(RuntimeError):
    pass


@dataclass
class Incoming:
    """A message that arrived on a channel."""
    channel: str          # telegram | whatsapp | email
    sender: str           # chat id, phone number, or email address
    text: str
    name: str = ""
    subject: str = ""
    thread: dict = field(default_factory=dict)    # how to reply in the same thread (email)


def _scrub(text: str, *values: str) -> str:
    for v in values:
        if v and len(v) >= 6:
            text = text.replace(v, "***")
    return text


def open_days(spec: str) -> set[int]:
    out: set[int] = set()
    for part in (spec or "mon-fri").lower().split(","):
        a, _, b = part.strip().partition("-")
        if a in DAYS:
            i, j = DAYS.index(a), DAYS.index(b) if b in DAYS else DAYS.index(a)
            out |= set(range(i, j + 1)) if i <= j else set(range(i, 7)) | set(range(0, j + 1))
    return out


def _hours(spec: str) -> tuple[dt.time, dt.time]:
    a, b = (spec or "09:00-17:00").split("-")
    return dt.time.fromisoformat(a), dt.time.fromisoformat(b)


def spoken_time(t: dt.datetime) -> str:
    return t.strftime("%I:%M %p").lstrip("0")


def normalize_phone(value: str) -> str:
    digits = re.sub(r"[^\d+]", "", str(value or ""))
    if not re.fullmatch(r"\+?[1-9]\d{6,14}", digits):
        raise ConnectorError("that isn't a phone number with a country code, like +15551234567")
    return digits.lstrip("+")


def normalize_email(value: str) -> str:
    name, addr = email.utils.parseaddr(str(value or ""))
    if not re.fullmatch(r"[^@\s<>]+@[^@\s<>]+\.[A-Za-z]{2,}", addr or ""):
        raise ConnectorError("that isn't an email address")
    return addr


class Connectors:
    def __init__(self, cfg: Config, *, transport: httpx.AsyncBaseTransport | None = None,
                 save: Callable[[dict], None] | None = None, smtp_factory=None, imap_factory=None):
        self.cfg = cfg
        self.transport = transport
        self.save = save                      # persists settings changes (Google sign-in)
        self.smtp_factory = smtp_factory      # tests
        self.imap_factory = imap_factory
        self._access: tuple[str, float] | None = None
        self._pending: dict[str, tuple[str, str, float]] = {}    # OAuth state -> (verifier, redirect, expiry)
        self.library = None                   # the engine's records and knowledge (library.Library)

    def _http(self, timeout: float = 15.0) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=timeout, follow_redirects=False, transport=self.transport)

    def _secrets(self) -> tuple[str, ...]:
        c = self.cfg
        return (c.telegram_bot_token, c.whatsapp_token, c.whatsapp_app_secret, c.email_password,
                c.google_client_secret, c.google_refresh_token, (self._access or ("", 0))[0])

    def _fail(self, what: str, exc: Exception | None = None, status: int | None = None, detail: str = "") -> ConnectorError:
        msg = f"{what} failed" + (f" ({status})" if status else f" ({exc.__class__.__name__})" if exc else "")
        if detail:
            msg += f": {detail[:200]}"
        return ConnectorError(_scrub(msg, *self._secrets()))

    def status(self) -> dict[str, bool]:
        c = self.cfg
        return {"email": c.email_ready, "telegram": bool(c.telegram_bot_token),
                "whatsapp": bool(c.whatsapp_token and c.whatsapp_phone_number_id), "google": c.google_ready}

    # ------------------------------------------------------------------ actions

    async def run(self, action: str, args: dict[str, Any]) -> Any:
        if action.startswith(("data.", "knowledge.")):
            return await self._local(action, args)
        if action == "email.send":
            return await self.send_email(args["to"], args.get("subject") or "", args.get("body") or "")
        if action == "whatsapp.send":
            return await self.send_whatsapp(args["to"], args["text"])
        if action == "telegram.send":
            return await self.send_telegram(args["chat_id"], args["text"])
        if action == "calendar.free_times":
            return await self.free_times(args["day"], int(args.get("duration_minutes") or 30),
                                         args.get("calendar_id"), args.get("hours"))
        if action == "calendar.book":
            return await self.book(args["start"], args["summary"], int(args.get("duration_minutes") or 30),
                                   args.get("attendee_email"), args.get("calendar_id"), args.get("description"))
        raise ConnectorError(f"unknown action {action}")

    # ------------------------------------------------------------------ the engine's own data
    async def _local(self, action: str, args: dict[str, Any]) -> Any:
        from .library import LibraryError
        lib = self.library
        if lib is None:
            raise ConnectorError("this engine has no data folder")
        try:
            if action == "data.lookup":
                where = {str(args["field"]): args.get("value")}
                where.update({str(k): v for k, v in (args.get("where") or {}).items()})
                if any(v in (None, "") for v in where.values()):
                    return {"found": 0, "records": [], "note": "nothing to look up yet"}
                found = lib.find(str(args["collection"]), where, limit=int(args.get("limit") or 3))
                keep = args.get("fields")
                if isinstance(keep, list) and keep:
                    found = [{k: r[k] for k in ["id", *keep] if k in r} for r in found]
                return {"found": len(found), "records": found}
            if action == "data.save":
                values = args.get("values")
                if not isinstance(values, dict):
                    raise ConnectorError("with.values must map field names to values")
                rec = lib.put(str(args["collection"]), {k: v for k, v in values.items() if v not in (None, "")}, merge=True)
                return {"saved": True, "id": rec["id"]}
            if action == "knowledge.search":
                hits = await lib.search(str(args["base"]), str(args.get("query") or ""), int(args.get("results") or 3))
                return {"results": [{"text": h["text"], "source": h["source"]} for h in hits]} if hits else \
                    {"results": [], "note": "nothing in the knowledge base matches; say you don't know rather than guess"}
        except LibraryError as exc:
            raise ConnectorError(str(exc)) from exc
        raise ConnectorError(f"unknown action {action}")

    # ------------------------------------------------------------------ Google sign-in

    def google_start(self, redirect_uri: str) -> str:
        if not (self.cfg.google_client_id and self.cfg.google_client_secret):
            raise ConnectorError("save your Google OAuth client ID and secret first")
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v[2] > now}
        state, verifier = _secrets.token_urlsafe(24), _secrets.token_urlsafe(48)
        self._pending[state] = (verifier, redirect_uri, now + 600)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return GOOGLE_AUTH + "?" + urlencode({
            "client_id": self.cfg.google_client_id, "redirect_uri": redirect_uri, "response_type": "code",
            "scope": " ".join(GOOGLE_SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
            "code_challenge": challenge, "code_challenge_method": "S256"})

    async def google_finish(self, state: str, code: str) -> str:
        pending = self._pending.pop(state or "", None)
        if pending is None or pending[2] < time.time():
            raise ConnectorError("this sign-in link expired; start again from Integrations")
        verifier, redirect, _ = pending
        try:
            async with self._http() as http:
                r = await http.post(GOOGLE_TOKEN, data={
                    "code": code, "client_id": self.cfg.google_client_id, "client_secret": self.cfg.google_client_secret,
                    "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier})
        except httpx.HTTPError as exc:
            raise self._fail("Google sign-in", exc) from exc
        if r.status_code != 200:
            raise self._fail("Google sign-in", status=r.status_code, detail=str(r.json().get("error_description", "")) if r.content else "")
        body = r.json()
        refresh = body.get("refresh_token")
        if not refresh:
            raise ConnectorError("Google didn't return a refresh token; remove the app's access in your Google account and try again")
        account = ""
        try:
            payload = body.get("id_token", "").split(".")[1]
            account = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("email", "")
        except (IndexError, ValueError):
            pass
        self._access = (body.get("access_token", ""), time.time() + int(body.get("expires_in", 3000)) - 60)
        if self.save:
            self.save({"google_refresh_token": refresh, "google_account": account})
        return account

    async def google_token(self) -> str:
        if self._access and self._access[1] > time.time():
            return self._access[0]
        c = self.cfg
        if not c.google_ready:
            raise ConnectorError("Google isn't connected (Integrations → Google)")
        try:
            async with self._http() as http:
                r = await http.post(GOOGLE_TOKEN, data={"client_id": c.google_client_id, "client_secret": c.google_client_secret,
                                                        "refresh_token": c.google_refresh_token, "grant_type": "refresh_token"})
        except httpx.HTTPError as exc:
            raise self._fail("Google sign-in", exc) from exc
        if r.status_code != 200:
            raise ConnectorError("Google refused the saved sign-in; connect Google again in Integrations")
        body = r.json()
        self._access = (body["access_token"], time.time() + int(body.get("expires_in", 3000)) - 60)
        return self._access[0]

    async def _google(self, method: str, url: str, what: str, **kwargs) -> Any:
        token = await self.google_token()
        try:
            async with self._http() as http:
                r = await http.request(method, url, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        except httpx.HTTPError as exc:
            raise self._fail(what, exc) from exc
        if r.status_code >= 400:
            detail = ""
            try:
                detail = str((r.json().get("error") or {}).get("message", ""))
            except ValueError:
                pass
            raise self._fail(what, status=r.status_code, detail=detail)
        return r.json() if r.content else {}

    # ------------------------------------------------------------------ Calendar

    def _day(self, day: str) -> dt.date:
        try:
            return dt.date.fromisoformat(str(day).strip()[:10])
        except ValueError as exc:
            raise ConnectorError("give the day as YYYY-MM-DD") from exc

    async def _busy(self, start: dt.datetime, end: dt.datetime, calendar_id: str) -> list[tuple[dt.datetime, dt.datetime]]:
        body = await self._google("POST", f"{CALENDAR}/freeBusy", "checking the calendar", json={
            "timeMin": start.isoformat(), "timeMax": end.isoformat(), "items": [{"id": calendar_id}]})
        cal = (body.get("calendars") or {}).get(calendar_id) or {}
        if cal.get("errors"):
            raise ConnectorError(f"the calendar {calendar_id} can't be read with this Google account")
        return [(dt.datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                 dt.datetime.fromisoformat(b["end"].replace("Z", "+00:00"))) for b in cal.get("busy") or []]

    async def free_times(self, day: str, duration: int = 30, calendar_id: str | None = None, hours: str | None = None) -> dict:
        tz, d = self.cfg.zone, self._day(day)
        duration = max(5, min(int(duration or 30), 480))
        cal = calendar_id or self.cfg.calendar_id or "primary"
        if d.weekday() not in open_days(self.cfg.calendar_days):
            return {"day": d.isoformat(), "open": False, "times": [], "note": "closed that day"}
        a, b = _hours(hours or self.cfg.calendar_hours)
        start, end = dt.datetime.combine(d, a, tz), dt.datetime.combine(d, b, tz)
        now = dt.datetime.now(tz) + dt.timedelta(minutes=15)
        busy = await self._busy(start, end, cal)
        times, t = [], start
        while t + dt.timedelta(minutes=duration) <= end and len(times) < 8:
            slot_end = t + dt.timedelta(minutes=duration)
            if t >= now and not any(s < slot_end and t < e for s, e in busy):
                times.append(t)
            t += dt.timedelta(minutes=30 if duration >= 30 else duration)
        return {"day": d.isoformat(), "weekday": d.strftime("%A"), "open": True,
                "times": [spoken_time(x) for x in times], "starts": [x.strftime("%Y-%m-%dT%H:%M") for x in times]}

    async def book(self, start: str, summary: str, duration: int = 30, attendee: str | None = None,
                   calendar_id: str | None = None, description: str | None = None) -> dict:
        tz = self.cfg.zone
        try:
            begin = dt.datetime.fromisoformat(str(start).strip()[:16])
        except ValueError as exc:
            raise ConnectorError("give the start as YYYY-MM-DDTHH:MM") from exc
        begin = begin.replace(tzinfo=tz) if begin.tzinfo is None else begin.astimezone(tz)
        duration = max(5, min(int(duration or 30), 480))
        finish = begin + dt.timedelta(minutes=duration)
        a, b = _hours(self.cfg.calendar_hours)
        if begin.weekday() not in open_days(self.cfg.calendar_days) or begin.time() < a or finish.time() > b or finish.date() != begin.date():
            return {"booked": False, "reason": "outside opening hours"}
        if begin < dt.datetime.now(tz):
            return {"booked": False, "reason": "that time has passed"}
        cal = calendar_id or self.cfg.calendar_id or "primary"
        if await self._busy(begin, finish, cal):
            return {"booked": False, "reason": "that time is taken"}
        event: dict[str, Any] = {"summary": str(summary)[:200] or "Appointment",
                                 "start": {"dateTime": begin.isoformat(), "timeZone": str(tz)},
                                 "end": {"dateTime": finish.isoformat(), "timeZone": str(tz)}}
        if description:
            event["description"] = str(description)[:2000]
        params = {}
        if attendee:
            event["attendees"] = [{"email": normalize_email(attendee)}]
            params["sendUpdates"] = "all"
        made = await self._google("POST", f"{CALENDAR}/calendars/{quote(cal, safe='')}/events", "booking",
                                  json=event, params=params)
        return {"booked": True, "start": begin.strftime("%A %B %d, ") + spoken_time(begin), "event_id": made.get("id", "")}

    # ------------------------------------------------------------------ email

    def _message(self, to: str, subject: str, body: str, thread: dict | None = None) -> EmailMessage:
        msg = EmailMessage()
        msg["From"] = email.utils.formataddr((self.cfg.brand_name or "", self.cfg.email_address or self.cfg.google_account))
        msg["To"] = normalize_email(to)
        msg["Subject"] = (subject or "(no subject)").replace("\n", " ")[:200]
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid()
        if thread and thread.get("message_id"):
            msg["In-Reply-To"] = thread["message_id"]
            msg["References"] = thread["message_id"]
        msg.set_content(str(body or "")[:20000])
        return msg

    async def send_email(self, to: str, subject: str, body: str, thread: dict | None = None) -> dict:
        msg = self._message(to, subject, body, thread)
        if self.cfg.email_provider == "gmail":
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
            payload: dict[str, Any] = {"raw": raw}
            if thread and thread.get("thread_id"):
                payload["threadId"] = thread["thread_id"]
            sent = await self._google("POST", f"{GMAIL}/messages/send", "sending the email", json=payload)
            return {"sent": True, "to": msg["To"], "id": sent.get("id", "")}
        c = self.cfg
        if not c.email_ready:
            raise ConnectorError("email isn't set up (Integrations → Email)")

        def go() -> None:
            import smtplib
            ctx = ssl.create_default_context()
            port = int(c.smtp_port or 587)
            factory = self.smtp_factory or (smtplib.SMTP_SSL if port == 465 else smtplib.SMTP)
            kwargs = {"timeout": 20} | ({"context": ctx} if factory is smtplib.SMTP_SSL else {})
            with factory(c.smtp_host, port, **kwargs) as s:
                if port != 465 and self.smtp_factory is None:
                    s.starttls(context=ctx)
                s.login(c.email_address, c.email_password)
                s.send_message(msg)
        try:
            await asyncio.to_thread(go)
        except Exception as exc:  # noqa: BLE001 - reported, never raised with the password in it
            raise self._fail("sending the email", exc) from None
        return {"sent": True, "to": msg["To"]}

    async def fetch_email(self, limit: int = 10) -> list[Incoming]:
        """Unread messages in the inbox, marked read once taken."""
        if self.cfg.email_provider == "gmail":
            listed = await self._google("GET", f"{GMAIL}/messages", "reading email",
                                        params={"q": "is:unread in:inbox -from:me newer_than:2d", "maxResults": limit})
            out = []
            for m in listed.get("messages") or []:
                full = await self._google("GET", f"{GMAIL}/messages/{m['id']}", "reading email", params={"format": "full"})
                headers = {h["name"].lower(): h["value"] for h in (full.get("payload") or {}).get("headers") or []}
                text = _gmail_text(full.get("payload") or {}) or full.get("snippet", "")
                name, addr = email.utils.parseaddr(headers.get("from", ""))
                out.append(Incoming("email", addr, _strip_quoted(text), name, headers.get("subject", ""),
                                    {"thread_id": full.get("threadId"), "message_id": headers.get("message-id", ""),
                                     "subject": headers.get("subject", "")}))
                await self._google("POST", f"{GMAIL}/messages/{m['id']}/modify", "reading email", json={"removeLabelIds": ["UNREAD"]})
            return out
        c = self.cfg
        if not (c.email_address and c.email_password and (c.imap_host or c.smtp_host)):
            return []

        def go() -> list[Incoming]:
            import imaplib
            from email import message_from_bytes, policy
            host = c.imap_host or re.sub(r"^smtp\.", "imap.", c.smtp_host)
            factory = self.imap_factory or (lambda h: imaplib.IMAP4_SSL(h, 993, ssl_context=ssl.create_default_context(), timeout=20))
            box = factory(host)
            try:
                box.login(c.email_address, c.email_password)
                box.select("INBOX")
                _, ids = box.search(None, "UNSEEN")
                out = []
                for num in (ids[0].split() if ids and ids[0] else [])[:limit]:
                    _, data = box.fetch(num, "(RFC822)")
                    raw = next((part[1] for part in data if isinstance(part, tuple)), b"")
                    msg = message_from_bytes(raw, policy=policy.default)
                    body = msg.get_body(preferencelist=("plain",))
                    name, addr = email.utils.parseaddr(str(msg.get("From", "")))
                    if addr.lower() == c.email_address.lower():
                        continue
                    out.append(Incoming("email", addr, _strip_quoted(body.get_content() if body else ""), name,
                                        str(msg.get("Subject", "")), {"message_id": str(msg.get("Message-ID", "")),
                                                                      "subject": str(msg.get("Subject", ""))}))
                    box.store(num, "+FLAGS", "\\Seen")
                return out
            finally:
                try:
                    box.logout()
                except Exception:  # noqa: BLE001
                    pass
        try:
            return await asyncio.to_thread(go)
        except Exception as exc:  # noqa: BLE001
            raise self._fail("reading email", exc) from None

    # ------------------------------------------------------------------ Telegram

    async def _telegram(self, method: str, payload: dict, timeout: float = 15.0) -> Any:
        token = self.cfg.telegram_bot_token
        if not token:
            raise ConnectorError("Telegram isn't set up (Integrations → Telegram)")
        try:
            async with self._http(timeout) as http:
                r = await http.post(f"{TELEGRAM}/bot{token}/{method}", json=payload)
        except httpx.HTTPError as exc:
            raise self._fail(f"Telegram {method}", exc) from None
        body = r.json() if r.content else {}
        if r.status_code >= 400 or not body.get("ok"):
            raise self._fail(f"Telegram {method}", status=r.status_code, detail=str(body.get("description", "")))
        return body.get("result")

    async def send_telegram(self, chat_id: Any, text: str) -> dict:
        if not re.fullmatch(r"-?\d{1,20}|@[A-Za-z0-9_]{5,32}", str(chat_id).strip()):
            raise ConnectorError("that isn't a Telegram chat id")
        sent = await self._telegram("sendMessage", {"chat_id": str(chat_id).strip(), "text": str(text)[:4000]})
        return {"sent": True, "message_id": (sent or {}).get("message_id")}

    async def telegram_updates(self, offset: int | None, wait: int = 25) -> tuple[list[Incoming], int | None]:
        result = await self._telegram("getUpdates", {"timeout": wait, "allowed_updates": ["message"],
                                                     **({"offset": offset} if offset else {})}, timeout=wait + 10)
        out, last = [], offset
        for u in result or []:
            last = int(u["update_id"]) + 1
            m = u.get("message") or {}
            if m.get("text") and (m.get("chat") or {}).get("type") == "private":
                who = m.get("from") or {}
                out.append(Incoming("telegram", str(m["chat"]["id"]), m["text"],
                                    " ".join(x for x in (who.get("first_name"), who.get("last_name")) if x)))
        return out, last

    # ------------------------------------------------------------------ WhatsApp

    async def send_whatsapp(self, to: str, text: str) -> dict:
        c = self.cfg
        if not (c.whatsapp_token and c.whatsapp_phone_number_id):
            raise ConnectorError("WhatsApp isn't set up (Integrations → WhatsApp)")
        number = normalize_phone(to)
        try:
            async with self._http() as http:
                r = await http.post(f"{GRAPH}/{c.whatsapp_phone_number_id}/messages",
                                    headers={"Authorization": f"Bearer {c.whatsapp_token}"},
                                    json={"messaging_product": "whatsapp", "to": number, "type": "text",
                                          "text": {"body": str(text)[:4000]}})
        except httpx.HTTPError as exc:
            raise self._fail("sending the WhatsApp message", exc) from None
        if r.status_code >= 400:
            err = (r.json().get("error") or {}) if r.content else {}
            hint = " (outside the 24-hour window, WhatsApp only allows approved templates)" if err.get("code") in (131047, 470) else ""
            raise self._fail("sending the WhatsApp message", status=r.status_code, detail=str(err.get("message", "")) + hint)
        return {"sent": True, "to": "+" + number, "id": ((r.json().get("messages") or [{}])[0]).get("id", "")}

    def whatsapp_signature_ok(self, body: bytes, header: str) -> bool:
        secret = self.cfg.whatsapp_app_secret
        if not secret or not header.startswith("sha256="):
            return False
        want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(want, header[7:])

    @staticmethod
    def whatsapp_messages(payload: dict) -> list[Incoming]:
        """Text messages in a WhatsApp webhook. Anything shaped differently is skipped, not an error (Meta would
        keep redelivering it), and a message delivered twice is answered once."""
        def items(value: Any, key: str) -> list[dict]:
            got = value.get(key) if isinstance(value, dict) else None
            return [x for x in got if isinstance(x, dict)] if isinstance(got, list) else []

        out = []
        now = time.monotonic()
        for k in [k for k, seen in _WHATSAPP_SEEN.items() if now - seen > 3600]:
            del _WHATSAPP_SEEN[k]
        for entry in items(payload, "entry"):
            for change in items(entry, "changes"):
                value = change.get("value") if isinstance(change.get("value"), dict) else {}
                names = {c.get("wa_id"): str((c.get("profile") if isinstance(c.get("profile"), dict) else {}).get("name", ""))
                         for c in items(value, "contacts")}
                for m in items(value, "messages"):
                    text = m.get("text") if isinstance(m.get("text"), dict) else {}
                    body = text.get("body")
                    if m.get("type") != "text" or not isinstance(body, str) or not body:
                        continue
                    mid = str(m.get("id") or "")
                    if mid:
                        if mid in _WHATSAPP_SEEN:
                            continue
                        _WHATSAPP_SEEN[mid] = now
                        while len(_WHATSAPP_SEEN) > 10000:
                            _WHATSAPP_SEEN.pop(next(iter(_WHATSAPP_SEEN)))
                    out.append(Incoming("whatsapp", "+" + str(m.get("from", "")).lstrip("+"), body,
                                        names.get(m.get("from"), "")))
        return out

    # ------------------------------------------------------------------ "Test connection"

    async def test(self, service: str) -> dict:
        """One cheap, read-only call that shows whether the saved settings work."""
        c = self.cfg
        try:
            if service == "telegram":
                me = await self._telegram("getMe", {})
                return {"ok": True, "message": f"connected as @{(me or {}).get('username', '?')}"}
            if service == "whatsapp":
                if not (c.whatsapp_token and c.whatsapp_phone_number_id):
                    return {"ok": False, "message": "save the access token and phone number ID first"}
                async with self._http() as http:
                    r = await http.get(f"{GRAPH}/{c.whatsapp_phone_number_id}", params={"fields": "display_phone_number"},
                                       headers={"Authorization": f"Bearer {c.whatsapp_token}"})
                if r.status_code >= 400:
                    return {"ok": False, "message": f"Meta answered {r.status_code}; check the token and phone number ID"}
                return {"ok": True, "message": f"connected: {r.json().get('display_phone_number', 'number found')}"}
            if service == "google":
                await self.google_token()
                return {"ok": True, "message": f"connected as {c.google_account or 'your Google account'}"}
            if service == "email":
                if c.email_provider == "gmail":
                    await self.google_token()
                    return {"ok": True, "message": f"using Gmail as {c.google_account or 'your Google account'}"}
                if not c.email_ready:
                    return {"ok": False, "message": "save the address, password and SMTP server first"}

                def login() -> None:
                    import smtplib
                    port = int(c.smtp_port or 587)
                    ctx = ssl.create_default_context()
                    if self.smtp_factory:
                        s = self.smtp_factory(c.smtp_host, port)
                    elif port == 465:
                        s = smtplib.SMTP_SSL(c.smtp_host, port, timeout=15, context=ctx)
                    else:
                        s = smtplib.SMTP(c.smtp_host, port, timeout=15)
                        s.starttls(context=ctx)
                    with s:
                        s.login(c.email_address, c.email_password)
                await asyncio.to_thread(login)
                return {"ok": True, "message": f"signed in to {c.smtp_host}"}
        except ConnectorError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:  # noqa: BLE001 - shown to the person, without secrets
            return {"ok": False, "message": _scrub(f"could not connect ({exc.__class__.__name__})", *self._secrets())}
        return {"ok": False, "message": f"nothing to test for {service!r}"}

    # ------------------------------------------------------------------ replying on a channel

    async def reply(self, incoming_channel: str, to: str, text: str, thread: dict | None = None) -> None:
        if incoming_channel == "telegram":
            await self.send_telegram(to, text)
        elif incoming_channel == "whatsapp":
            await self.send_whatsapp(to, text)
        elif incoming_channel == "email":
            subject = (thread or {}).get("subject") or f"Message from {self.cfg.brand_name or 'us'}"
            if thread and not subject.lower().startswith("re:"):
                subject = "Re: " + subject
            await self.send_email(to, subject, text, thread)
        else:
            raise ConnectorError(f"can't reply on {incoming_channel}")


def _gmail_text(payload: dict) -> str:
    if payload.get("mimeType") == "text/plain" and (payload.get("body") or {}).get("data"):
        data = payload["body"]["data"]
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    for part in payload.get("parts") or []:
        text = _gmail_text(part)
        if text:
            return text
    return ""


def _strip_quoted(text: str) -> str:
    """Keep what the person wrote, not the quoted thread below it."""
    lines = []
    for line in (text or "").splitlines():
        if line.startswith(">") or re.match(r"^On .{5,200} wrote:$", line.strip()):
            break
        lines.append(line)
    return "\n".join(lines).strip()[:4000]
