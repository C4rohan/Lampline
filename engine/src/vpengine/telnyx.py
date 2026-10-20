"""Phone calls through Telnyx (a TeXML application with media streaming).

Flow: a caller dials your Telnyx number -> Telnyx POSTs to /v1/telnyx/voice
(signed with ed25519; checked against your account's public key) -> we answer
with TeXML that opens a bidirectional media stream to /v1/telnyx/stream,
carrying a one-time token -> the agent talks over the stream. With an API key
set, the call is hung up through Telnyx when the agent reaches an end step.
"""

from __future__ import annotations

import base64
import time
from xml.sax.saxutils import escape, quoteattr

TOLERANCE = 300   # seconds, same as Telnyx's own SDK


API = "https://api.telnyx.com/v2"


def valid_signature(public_key_b64: str, timestamp: str, signature_b64: str, body: bytes,
                    *, now: float | None = None) -> bool:
    """Telnyx signs f"{timestamp}|{raw body}" with ed25519 (base64 key and signature)."""
    if not (public_key_b64 and timestamp and signature_b64):
        return False
    try:
        if abs((now or time.time()) - int(timestamp)) > TOLERANCE:
            return False
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64, validate=True))
        key.verify(base64.b64decode(signature_b64, validate=True), f"{timestamp}|".encode() + body)
        return True
    except Exception:  # noqa: BLE001 - any parse or verify failure means "not from Telnyx"
        return False


def stream_texml(stream_url: str, token: str) -> str:
    url = f"{stream_url}?t={token}"
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            "<Response><Connect>"
            f"<Stream url={quoteattr(url)} bidirectionalMode=\"rtp\" bidirectionalCodec=\"PCMU\">"
            f"<Parameter name=\"t\" value={quoteattr(token)}/></Stream>"
            "</Connect></Response>")


def reject_texml(message: str) -> str:
    return ("<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
            f"<Response><Say>{escape(message)}</Say><Hangup/></Response>")


def token_from_start(start: dict) -> str | None:
    """The one-time token, wherever Telnyx put our <Parameter>."""
    for key in ("custom_parameters", "customParameters", "custom_params"):
        params = start.get(key)
        if isinstance(params, dict) and params.get("t"):
            return str(params["t"])
        if isinstance(params, list):
            for p in params:
                if isinstance(p, dict) and p.get("name") == "t":
                    return str(p.get("value"))
    return None
