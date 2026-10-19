"""Stand-ins for a transport, speech-to-text and text-to-speech, so the voice
pipeline can be exercised without audio hardware or a Deepgram key."""

from pipecat.frames.frames import (
    Frame,
    TranscriptionFrame,
    TTSAudioRawFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.settings import STTSettings, TTSSettings
from pipecat.services.stt_service import STTService
from pipecat.services.tts_service import TTSService
from pipecat.utils.time import time_now_iso8601


class Passthrough(FrameProcessor):
    def __init__(self, name, seen=None):
        super().__init__(name=name)
        self.seen = seen

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if self.seen is not None and direction == FrameDirection.DOWNSTREAM:
            self.seen.append(frame)
        await self.push_frame(frame, direction)


class FakeTransport:
    def __init__(self):
        self.out_frames = []
        self._in = Passthrough("fake-in")
        self._out = Passthrough("fake-out", self.out_frames)

    def input(self):
        return self._in

    def output(self):
        return self._out


class FakeSTT(STTService):
    """Never hears audio; tests inject TranscriptionFrames directly."""

    def __init__(self, **kw):
        super().__init__(settings=STTSettings(model=None, language=None), ttfs_p99_latency=0.1, **kw)

    async def run_stt(self, audio: bytes):
        yield None


class FakeTTS(TTSService):
    """Turns each sentence into a few bytes of silence and records the text."""

    def __init__(self, **kw):
        super().__init__(sample_rate=24000, push_start_frame=True, push_stop_frames=True,
                         stop_frame_timeout_s=0.05, settings=TTSSettings(model=None, voice=None, language=None), **kw)
        self.spoken = []

    async def run_tts(self, text: str, context_id: str):
        self.spoken.append(text)
        yield TTSAudioRawFrame(audio=b"\x00\x00" * 240, sample_rate=24000, num_channels=1, context_id=context_id)


def transcription(text: str) -> TranscriptionFrame:
    return TranscriptionFrame(text=text, user_id="caller", timestamp=time_now_iso8601(), finalized=True)
