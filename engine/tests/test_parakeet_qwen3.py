"""Parakeet speech to text (on this computer) and Qwen3-TTS (a vLLM-Omni server)."""

from __future__ import annotations

import numpy as np
import pytest

from vpengine import parakeet_stt
from vpengine.services import Config, ConfigError, SpeechEndpointTTSService, make_stt, make_tts


class FakeModel:
    def __init__(self):
        self.seen = []

    def recognize(self, audio, sample_rate):
        self.seen.append((audio, sample_rate))
        return "book a cleaning"


def test_transcribe_resamples_phone_audio_and_leads_with_silence():
    m = FakeModel()
    pcm = (np.sin(np.arange(8000) / 5) * 8000).astype(np.int16).tobytes()      # 1 s at 8 kHz
    assert parakeet_stt.transcribe(m, pcm, 8000) == "book a cleaning"
    audio, rate = m.seen[0]
    lead = int(parakeet_stt.RATE * parakeet_stt.LEAD_SECS)
    assert rate == 16000 and audio.dtype == np.float32
    assert abs(len(audio) - (16000 + lead)) < 50 and not audio[:lead].any() and audio[lead:].any()


async def test_service_sends_a_final_transcript():
    from pipecat.frames.frames import ErrorFrame, TranscriptionFrame

    svc = parakeet_stt.ParakeetSTTService()
    assert svc.wants_wav_segments is False
    frames = [f async for f in svc.run_stt(b"\x00\x00" * 1600)]
    assert isinstance(frames[0], ErrorFrame)                                    # not loaded yet
    svc._model = FakeModel()
    frames = [f async for f in svc.run_stt(b"\x00\x00" * 1600)]
    assert isinstance(frames[-1], TranscriptionFrame) and frames[-1].text == "book a cleaning" and frames[-1].finalized


def test_not_downloaded(tmp_path, monkeypatch):
    monkeypatch.setattr("vpengine.builtin_speech.CACHE", tmp_path)
    assert not parakeet_stt.ready() and not Config(stt="parakeet").stt_ready
    with pytest.raises(ConfigError, match="Parakeet model isn't downloaded"):
        make_stt(Config(stt="parakeet"))
    d = parakeet_stt.model_dir()
    d.mkdir(parents=True)
    for f in ("encoder-model.int8.onnx", "decoder_joint-model.int8.onnx", "config.json", "vocab.txt"):
        (d / f).write_text("x")
    assert parakeet_stt.ready() and Config(stt="parakeet").stt_ready
    assert parakeet_stt.Downloads().status()["size_mb"] == 661


def test_qwen3_tts_asks_vllm_omni_to_stream():
    tts = make_tts(Config(tts="qwen3-tts", qwen3_tts_url="http://gpu-box:8091/v1", qwen3_tts_voice="ryan"))
    assert isinstance(tts, SpeechEndpointTTSService) and tts._url == "http://gpu-box:8091/v1/audio/speech"
    assert tts._model == "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice" and tts._voice == "ryan"
    assert tts._extra == {"stream": True, "stream_format": "audio", "language": "Auto"}
    with pytest.raises(ConfigError, match="Qwen3-TTS server URL"):
        make_tts(Config(tts="qwen3-tts", qwen3_tts_url=""))
    assert Config(tts="qwen3-tts").describe()["tts_voice"].startswith("vivian")


async def test_qwen3_request_fields(fake_url, requests_seen):
    from pipecat.frames.frames import TTSAudioRawFrame

    tts = SpeechEndpointTTSService(base_url=fake_url, voice="vivian", model="Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
                                   extra={"stream": True, "stream_format": "audio", "language": "Auto"})
    frames = [f async for f in tts.run_tts("Hello there.", "ctx")]
    await tts._close()
    assert any(isinstance(f, TTSAudioRawFrame) for f in frames)
    sent = [r for r in requests_seen if r["path"].endswith("/audio/speech")][-1]
    assert sent["stream"] is True and sent["stream_format"] == "audio" and sent["response_format"] == "pcm"
    assert sent["voice"] == "vivian" and sent["language"] == "Auto"


@pytest.mark.skipif(not parakeet_stt.ready(), reason="the Parakeet model isn't downloaded here")
def test_real_model_if_downloaded():
    m = parakeet_stt.load()
    assert parakeet_stt.transcribe(m, b"\x00\x00" * 16000, 16000) == ""
