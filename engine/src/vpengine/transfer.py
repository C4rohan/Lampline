"""Live-agent transfer: an end step hands the caller to a person, with what the agent collected.

An end step's data may say:

    transfer_to: "+15551234567"                # a phone number (E.164), or
    transfer_to: "sip:desk@pbx.example.com"    # a SIP address on your phone system or contact center
    transfer_details: [caller_name, reason]    # what to send; empty: everything collected
    transfer_uui: "{{account_id}}"             # optional User-to-User data (Avaya, Cisco)

When the call reaches the step, the agent says its line, then the carrier
connects the caller instead of hanging up:

- Twilio: the stream's TwiML ends with a <Redirect> back to us; we answer
  <Dial><Number> or <Dial><Sip>, the details as X- headers on the SIP address.
- Telnyx: the Call Control transfer command, details as custom SIP headers.
- jambonz: the listen verb's actionHook answers with a dial verb and headers.

Details travel as SIP headers: X-Lampline-Session (the session ID, to look the
call up afterwards) and X-Lampline-<name> per detail. A phone number on the
public network can't carry headers, so for those the details stay in the
call's events (and any after-call trigger), keyed by the same session ID.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape

PHONE = re.compile(r"^\+[1-9]\d{6,14}$")
SIP = re.compile(r"^sips?:[^\s@<>\"]+@[A-Za-z0-9.\-\[\]:]+(;[^\s<>\"]*)?$")
HEADER_PREFIX = "X-Lampline-"
MAX_VALUE = 200
MAX_HEADERS = 12
TARGET_UNSAFE = re.compile(r"[^0-9A-Za-z+*#._\-]")


def valid_target(to: str) -> bool:
    return bool(PHONE.match(to) or SIP.match(to))


def _header_name(name: str) -> str:
    return HEADER_PREFIX + (re.sub(r"[^A-Za-z0-9_\-]", "", name)[:40] or "detail")


def _header_value(value: Any) -> str:
    """ASCII, one line, short: SIP headers travel through phone systems that expect exactly that."""
    if isinstance(value, bool):
        text = "yes" if value else "no"
    elif isinstance(value, (list, tuple)):
        text = ", ".join(str(v) for v in value)
    else:
        text = str(value)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()
    return text[:MAX_VALUE]


@dataclass
class Transfer:
    to: str
    session_id: str
    details: dict[str, str] = field(default_factory=dict)
    uui: str = ""

    @property
    def sip(self) -> bool:
        return self.to.startswith(("sip:", "sips:"))

    def headers(self) -> dict[str, str]:
        out = {f"{HEADER_PREFIX}Session": self.session_id}
        for name, value in list(self.details.items())[:MAX_HEADERS]:
            if value:
                out[_header_name(name)] = value
        return out

    def uui_header(self) -> str:
        """User-to-User, hex encoded as RFC 7433 expects."""
        return f"{self.uui.encode().hex()};encoding=hex" if self.uui else ""

    def event(self) -> dict:
        return {"to": self.to, "details": dict(self.details), "session_id": self.session_id,
                **({"uui": self.uui} if self.uui else {})}

    # -- per carrier -------------------------------------------------------------------

    def twiml(self) -> str:
        if self.sip:
            params = [*self.headers().items(), *([("User-to-User", self.uui_header())] if self.uui else [])]
            sep = "&" if "?" in self.to else "?"
            uri = self.to + sep + "&".join(f"{quote(k)}={quote(v, safe='')}" for k, v in params)
            target = f"<Sip>{escape(uri)}</Sip>"
        else:
            target = f"<Number>{escape(self.to)}</Number>"
        return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
                f"<Response><Dial answerOnBridge=\"true\">{target}</Dial></Response>")

    def jambonz_verbs(self) -> list[dict]:
        target = {"type": "sip", "sipUri": self.to} if self.sip else {"type": "phone", "number": self.to}
        headers = self.headers() if self.sip else {}
        if self.sip and self.uui:
            headers["User-to-User"] = self.uui_header()
        return [{"verb": "dial", "answerOnBridge": True, "target": [target], **({"headers": headers} if headers else {})},
                {"verb": "hangup"}]

    def telnyx_body(self) -> dict:
        body: dict[str, Any] = {"to": self.to}
        if self.sip:
            body["custom_headers"] = [{"name": k, "value": v} for k, v in self.headers().items()]
            if self.uui:
                body["sip_headers"] = [{"name": "User-to-User", "value": self.uui_header()}]
        return body


def build(step, variables: dict[str, Any], session_id: str, render, collected: list[str] | None = None) -> Transfer | None:
    """The transfer an end step asks for, or None. Raises ValueError for an address that can't be dialled.

    collected: the details this call gathered (not the agent's own constants), sent when the
    step doesn't list which details to send.
    """
    raw = str(step.data.get("transfer_to") or "").strip() if step.kind == "end" else ""
    if not raw:
        return None
    if re.fullmatch(r"\{\{\s*[A-Za-z_]\w*\s*\}\}", raw):
        to = render(raw, variables).strip()          # the whole address from one variable (a lookup): checked below
    else:
        # A variable inside an address ("sip:{{extension}}@pbx...") is filled with address characters only, so a
        # value the caller influenced can't add SIP parameters or headers (;transport=, ?X-...=) or another host.
        to = render(raw, {k: TARGET_UNSAFE.sub("", str(v)) for k, v in variables.items()}).strip()
    if not valid_target(to):
        raise ValueError(f"{step.name}: “{to}” isn't a phone number like +15551234567 or a SIP address like sip:desk@pbx.example.com")
    wanted = step.data.get("transfer_details")
    names = [str(n) for n in wanted] if isinstance(wanted, list) and wanted else list(collected if collected is not None else variables)
    details = {n: _header_value(variables[n]) for n in names if variables.get(n) not in (None, "", [], {})}
    uui = _header_value(render(str(step.data.get("transfer_uui") or ""), variables)) if step.data.get("transfer_uui") else ""
    return Transfer(to=to, session_id=session_id, details=details, uui=uui)


class PendingTransfers:
    """Transfers waiting for the carrier to ask what's next, by its call ID (a few minutes at most)."""

    def __init__(self, ttl: float = 300.0):
        import time
        self._now = time.monotonic
        self._ttl = ttl
        self._items: dict[str, tuple[Transfer, float]] = {}

    def add(self, call_id: str, transfer: Transfer) -> None:
        if call_id:
            self._expire()
            self._items[call_id] = (transfer, self._now() + self._ttl)

    def pop(self, call_id: str | None) -> Transfer | None:
        self._expire()
        item = self._items.pop(call_id or "", None)
        return item[0] if item else None

    def _expire(self) -> None:
        now = self._now()
        for key in [k for k, (_, until) in self._items.items() if until < now]:
            del self._items[key]
