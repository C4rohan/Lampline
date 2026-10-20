"""Phone calls through Twilio Media Streams.

Flow: a caller dials your Twilio number -> Twilio POSTs to /v1/twilio/voice
(checked with your auth token's signature) -> we answer with TwiML that opens
a media stream to /v1/twilio/stream, carrying a one-time token -> the stream
must present that token within two minutes -> the agent talks over the stream.
When the agent reaches an end step, the call is hung up through Twilio's API.

Twilio must reach the engine over the internet, so this needs a tunnel
(for example Cloudflare Tunnel) and VPE_PUBLIC_URL set to its https address.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass, field
from xml.sax.saxutils import quoteattr

TOKEN_TTL = 120.0


def signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    """Twilio's request signature: HMAC-SHA1 over the URL plus sorted form fields."""
    payload = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()


def valid_signature(auth_token: str, url: str, params: dict[str, str], given: str) -> bool:
    if not auth_token or not given:
        return False
    return hmac.compare_digest(signature(auth_token, url, params), given)


def stream_twiml(stream_url: str, token: str, after_url: str = "") -> str:
    """Stream the call to us. When we close the stream, Twilio carries on with what follows: a
    <Redirect> to after_url, where we answer with a transfer (<Dial>) or <Hangup/>."""
    from xml.sax.saxutils import escape
    after = f"<Redirect method=\"POST\">{escape(after_url)}</Redirect>" if after_url else ""
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            "<Response><Connect>"
            f"<Stream url={quoteattr(stream_url)}><Parameter name=\"t\" value={quoteattr(token)}/></Stream>"
            f"</Connect>{after}</Response>")


def hangup_twiml() -> str:
    return "<?xml version=\"1.0\" encoding=\"UTF-8\"?><Response><Hangup/></Response>"


def reject_twiml(message: str) -> str:
    from xml.sax.saxutils import escape
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            f"<Response><Say>{escape(message)}</Say><Hangup/></Response>")


@dataclass
class PendingCall:
    agent: str
    call_sid: str
    issued: float = field(default_factory=time.monotonic)
    context: dict = field(default_factory=dict)    # values for the session (outbound calls, caller number)
    contact: str = ""


class StreamTokens:
    """One-time tokens linking the signed voice webhook to the media stream."""

    def __init__(self, ttl: float = TOKEN_TTL):
        self.ttl = ttl
        self._pending: dict[str, PendingCall] = {}

    def issue(self, agent: str, call_sid: str, context: dict | None = None, contact: str = "") -> str:
        self._expire()
        token = secrets.token_urlsafe(24)
        self._pending[token] = PendingCall(agent, call_sid, context=dict(context or {}), contact=contact)
        return token

    def redeem(self, token: str | None, call_sid: str | None) -> PendingCall | None:
        self._expire()
        pending = self._pending.pop(token or "", None)
        if pending is None or (call_sid and pending.call_sid and pending.call_sid != call_sid):
            return None
        return pending

    def _expire(self) -> None:
        now = time.monotonic()
        for t, p in list(self._pending.items()):
            if now - p.issued > self.ttl:
                del self._pending[t]
