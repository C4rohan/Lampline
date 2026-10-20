"""Phone calls over SIP through jambonz (open-source, MIT; https://jambonz.org).

jambonz connects SIP trunks, phone systems and enterprise SBCs to apps. A
jambonz application points its "calling webhook" here:

    a call reaches jambonz -> jambonz POSTs the call to /v1/jambonz/call?agent=<folder>
    (signed with the application's webhook secret: Jambonz-Signature) -> we answer with
    a `listen` verb that streams the call's audio to /v1/jambonz/stream with a one-time
    token -> the agent talks over that websocket (linear 16-bit PCM both ways) -> when
    the agent reaches an end step we send `disconnect` and the call hangs up.

Only inbound calls for now; jambonz's REST API can place outbound calls later.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

from pipecat.audio.dtmf.types import KeypadEntry
from pipecat.audio.utils import create_stream_resampler
from pipecat.frames.frames import (
    AudioRawFrame,
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InputDTMFFrame,
    InterruptionFrame,
)
from pipecat.processors.frame_processor import FrameProcessorSetup
from pipecat.serializers.base_serializer import FrameSerializer

TOLERANCE = 300                      # seconds, the same as jambonz's own SDK
SAMPLE_RATE = 8000                   # phone audio; jambonz resamples whatever the trunk uses
RATES = (8000, 16000)                # 16000: HD voice (G.722, Opus or wideband SIP), clearer for listening
SUBPROTOCOLS = ("audio.jambonz.org", "audio.drachtio.org")


def valid_signature(secret: str, header: str, body: bytes, *, now: float | None = None) -> bool:
    """Jambonz-Signature: "t=<unix time>,v1=<hex HMAC-SHA256 of f"{t}.{body}">" (one or more v1 values)."""
    if not (secret and header):
        return False
    timestamp, signatures = None, []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1" and value:
            signatures.append(value)
    if not timestamp or not signatures:
        return False
    try:
        if abs((now or time.time()) - int(timestamp)) > TOLERANCE:
            return False
    except ValueError:
        return False
    text = body.decode("utf-8", "replace")
    candidates = [text]
    try:      # the SDK signs JSON.stringify(body); our raw body normally is exactly that
        candidates.append(json.dumps(json.loads(text), separators=(",", ":"), ensure_ascii=False))
    except ValueError:
        pass
    for data in candidates:
        expected = hmac.new(secret.encode(), f"{timestamp}.{data}".encode(), hashlib.sha256).hexdigest()
        if any(hmac.compare_digest(expected, s) for s in signatures):
            return True
    return False


def listen_verbs(stream_url: str, token: str, action_url: str, sample_rate: int = SAMPLE_RATE) -> list[dict[str, Any]]:
    """Stream the call to us (both ways, as raw PCM) and hang up when the stream ends."""
    rate = sample_rate if sample_rate in RATES else SAMPLE_RATE
    return [
        {"verb": "listen", "url": f"{stream_url}?t={token}", "actionHook": action_url,
         "sampleRate": rate, "mixType": "mono", "passDtmf": True,
         "bidirectionalAudio": {"enabled": True, "streaming": True, "sampleRate": rate},
         "metadata": {"t": token}},
        {"verb": "hangup"},
    ]


def reject_verbs(message: str) -> list[dict[str, Any]]:
    return [{"verb": "say", "text": message}, {"verb": "hangup"}]


def subprotocol(offered: list[str]) -> str | None:
    """jambonz asks for a websocket subprotocol and expects it echoed back."""
    for name in SUBPROTOCOLS:
        if name in offered:
            return name
    return offered[0] if offered else None


class JambonzFrameSerializer(FrameSerializer):
    """Pipecat frames <-> jambonz's listen websocket.

    jambonz sends one JSON text frame with the call's details (read before this
    serializer starts), then binary linear-16 audio, plus JSON text for DTMF.
    We send binary linear-16 audio back (bidirectionalAudio.streaming), killAudio
    when the caller interrupts, and disconnect when the agent ends the call.
    """

    class InputParams(FrameSerializer.InputParams):
        jambonz_sample_rate: int = SAMPLE_RATE
        sample_rate: int | None = None
        hang_up: bool = True

    def __init__(self, params: InputParams | None = None):
        params = params or JambonzFrameSerializer.InputParams()
        super().__init__(params)
        self._params: JambonzFrameSerializer.InputParams = params
        self._rate = params.jambonz_sample_rate
        self._sample_rate = 0
        self._in = create_stream_resampler(clear_after_secs=params.resampler_clear_after_secs)
        self._out = create_stream_resampler(clear_after_secs=params.resampler_clear_after_secs)
        self._ended = False

    async def setup(self, setup: FrameProcessorSetup):
        self._sample_rate = self._params.sample_rate or setup.audio_in_sample_rate

    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, InterruptionFrame):
            return json.dumps({"type": "killAudio"})
        if isinstance(frame, (EndFrame, CancelFrame)):
            if self._params.hang_up and not self._ended:
                self._ended = True
                return json.dumps({"type": "disconnect"})
            return None
        if isinstance(frame, AudioRawFrame):
            audio = await self._out.resample(frame.audio, frame.sample_rate, self._rate)
            return audio or None
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, (bytes, bytearray)):
            audio = await self._in.resample(bytes(data), self._rate, self._sample_rate or self._rate)
            if not audio:
                return None
            return InputAudioRawFrame(audio=audio, num_channels=1, sample_rate=self._sample_rate or self._rate)
        try:
            message = json.loads(data)
        except ValueError:
            return None
        if isinstance(message, dict) and message.get("event") == "dtmf":
            try:
                return InputDTMFFrame(KeypadEntry(str(message.get("dtmf"))))
            except ValueError:
                return None
        return None        # playDone, mark and anything newer: nothing for the agent to do
