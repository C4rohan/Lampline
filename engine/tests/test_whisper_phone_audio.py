"""Built-in Whisper reads 16 kHz audio, so phone calls (8 kHz) must be converted first.

Without it, phone audio plays twice as fast to Whisper and comes out as nonsense.
No model needed: a stand-in records what Whisper would have been given.
"""

import numpy as np
import pytest

from vpengine import builtin_speech as bs


class FakeWhisper:
    def __init__(self):
        self.given = []

    def transcribe(self, audio, **kw):
        self.given.append(audio)
        return iter([]), None


@pytest.fixture
def stt(monkeypatch):
    fake = FakeWhisper()
    monkeypatch.setattr(bs, "whisper_ready", lambda model: True)
    monkeypatch.setattr(bs, "load_whisper", lambda model: fake)
    s = bs.make_builtin_stt("base.en")
    s.fake = fake
    return s


@pytest.mark.parametrize("rate", [8000, 24000])
async def test_call_audio_is_converted_to_16_khz(stt, rate):
    stt._sample_rate = rate
    tone = (np.sin(np.arange(rate) * 2 * np.pi * 440 / rate) * 8000).astype(np.int16)    # 1 s at the call's rate
    [f async for f in stt.run_stt(tone.tobytes())]
    (heard,) = stt.fake.given
    assert abs(len(heard) - 16000) <= 32               # 1 s at 16 kHz
    assert 0.2 < float(np.abs(heard).max()) < 0.3      # same loudness (8000 / 32768)


async def test_16_khz_audio_is_left_alone(stt):
    stt._sample_rate = 16000
    audio = np.arange(16000, dtype=np.int16).tobytes()
    [f async for f in stt.run_stt(audio)]
    (heard,) = stt.fake.given
    assert len(heard) == 16000 and heard[100] == pytest.approx(100 / 32768)
