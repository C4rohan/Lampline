"""Fish Audio: listening (ASR) and the voice list, against a fake Fish API."""

import asyncio
import re

import httpx
import pytest

from vpengine.fish import FishSTTService, clean_transcript, list_voices
from vpengine.services import Config, ConfigError, make_stt


def fish_base(fake_url: str) -> str:
    return fake_url.rsplit("/v1", 1)[0]


def test_transcript_cleanup():
    assert clean_transcript("<|speaker:0|> Hello  there <|speaker:1|> hi") == "Hello there hi"


def test_listening_provider_is_wired():
    assert isinstance(make_stt(Config(stt="fish", fish_key="k")), FishSTTService)
    assert Config(stt="fish", fish_key="k", tts="fish").voice_ready       # one key covers both directions
    with pytest.raises(ConfigError, match="Fish Audio API key"):
        make_stt(Config(stt="fish"))


async def test_speech_to_text_request(fake_url, requests_seen):
    from pipecat.frames.frames import ErrorFrame, TranscriptionFrame
    from pipecat.audio.utils import pcm_to_wav

    wav = pcm_to_wav(b"\x10\x00" * 16000, 16000)
    stt = FishSTTService(api_key="fish-good-key", base_url=fish_base(fake_url))
    frames = [f async for f in stt.run_stt(wav)]
    await stt._close()
    assert isinstance(frames[0], TranscriptionFrame) and frames[0].text == "I have a parking question"
    sent = [r for r in requests_seen if r["path"] == "/v1/asr"][-1]
    assert sent["model"] == "transcribe-1" and sent["wav"] and sent["auth"] == "Bearer fish-good-key"

    bad = FishSTTService(api_key="wrong", base_url=fish_base(fake_url))
    frames = [f async for f in bad.run_stt(wav)]
    await bad._close()
    assert isinstance(frames[0], ErrorFrame) and "rejected" in frames[0].error

    broke = FishSTTService(api_key="fish-broke-key", base_url=fish_base(fake_url))
    frames = [f async for f in broke.run_stt(wav)]
    await broke._close()
    assert "no API credit" in frames[0].error and "Whisper on this computer" in frames[0].error


async def test_fish_listening_in_a_call(cfg, fake_url):
    """VAD cuts a segment, Fish transcribes it, and the call moves on."""
    import voicefakes as vf
    from conftest import mini_definition
    from pipecat.frames.frames import InputAudioRawFrame, VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame
    from vpengine.graph import Graph
    from vpengine.session import VoiceSession

    stt = FishSTTService(api_key="fish-good-key", base_url=fish_base(fake_url))
    g = Graph.from_definition("Mini", mini_definition(), {"business_name": "Riverside"})
    tts = vf.FakeTTS()
    s = VoiceSession(g, cfg, vf.FakeTransport(), stt=stt, tts=tts, vad=None)
    runner = asyncio.create_task(s.run())
    try:
        for _ in range(100):
            if tts.spoken:
                break
            await asyncio.sleep(0.05)
        await s.queue([VADUserStartedSpeakingFrame()])
        await s.queue([InputAudioRawFrame(audio=b"\x10\x00" * 1600, sample_rate=16000, num_channels=1) for _ in range(8)])
        await s.queue([VADUserStoppedSpeakingFrame()])
        for _ in range(200):
            if any(e["type"] == "node_transition" and e["node_name"] == "Help" for e in s.events):
                break
            await asyncio.sleep(0.05)
        assert [e["text"] for e in s.events if e["type"] == "user_speech"] == ["I have a parking question"]
        assert any(e["type"] == "node_transition" and e["node_name"] == "Help" for e in s.events)
    finally:
        await s.stop()
        await asyncio.wait({runner}, timeout=5)


async def test_voice_list(fake_url):
    voices = await list_voices("fish-good-key", base_url=fish_base(fake_url))
    assert voices["mine"] == [{"id": "my-voice-1", "title": "My clone", "languages": ["en"], "mine": True}]
    assert [v["id"] for v in voices["popular"]] == ["pop-1", "pop-2"]
    with pytest.raises(PermissionError):
        await list_voices("wrong", base_url=fish_base(fake_url))


def test_every_voice_setting_reaches_the_service():
    from pipecat.services.fish.tts import FishAudioTTSService
    from vpengine.services import FISH_TTS_MODELS, make_tts

    assert FISH_TTS_MODELS == ("s2.1-pro-free", "s2.1-pro", "s2-pro", "drama-3", "s1")
    every = dict(tts="fish", fish_key="k", fish_voice="v-1", fish_model="s2.1-pro", fish_latency="low",
                 fish_speed="1.2", fish_volume="-3", fish_temperature="0.4", fish_top_p="0.9", fish_normalize="off")
    over_http = make_tts(Config(**every))._body
    assert over_http == {"format": "pcm", "sample_rate": 24000, "latency": "low", "normalize": False, "reference_id": "v-1",
                         "prosody": {"speed": 1.2, "volume": -3.0}, "temperature": 0.4, "top_p": 0.9}
    tts = make_tts(Config(**every, fish_connection="live"))
    st = tts._settings
    assert isinstance(tts, FishAudioTTSService)
    assert (st.model, st.voice, st.latency, st.prosody_speed, st.prosody_volume, st.temperature, st.top_p, st.normalize) == \
        ("s2.1-pro", "v-1", "low", 1.2, -3, 0.4, 0.9, False)
    plain = make_tts(Config(tts="fish", fish_key="k", fish_connection="live"))._settings
    assert (plain.model, plain.latency, plain.normalize) == ("s2.1-pro-free", "balanced", True)


@pytest.mark.parametrize("changes,message", [
    ({"fish_speed": "3"}, "Speed must be between 0.5 and 2"),
    ({"fish_volume": "loud"}, "Volume (dB) must be a number"),
    ({"fish_top_p": "1.5"}, "Top P must be between 0 and 1"),
    ({"fish_model": "s9"}, "VPE_FISH_MODEL must be one of"),
    ({"fish_latency": "instant"}, "VPE_FISH_LATENCY must be one of"),
    ({"fish_stt_language": "english"}, "two-letter code"),
])
def test_bad_fish_settings_are_rejected(changes, message):
    with pytest.raises(ConfigError, match=re.escape(message)):
        Config.load({}, changes)


def test_transcription_form_fields():
    basic = FishSTTService(api_key="k")._form()
    assert basic == {"ignore_timestamps": "true", "language": "en"}
    pro = FishSTTService(api_key="k", model="transcribe-1-pro", language="")._form()
    assert pro == {"ignore_timestamps": "true", "tag_audio_events": "false", "diarize": "false"}
    assert make_stt(Config(stt="fish", fish_key="k", fish_stt_language="es"))._lang == "es"


def test_free_model_speaks_over_http_and_paid_over_live():
    from pipecat.services.fish.tts import FishAudioTTSService
    from vpengine.fish import FishHTTPTTSService
    from vpengine.services import make_tts

    assert isinstance(make_tts(Config(tts="fish", fish_key="k")), FishHTTPTTSService)              # s2.1-pro-free
    assert isinstance(make_tts(Config(tts="fish", fish_key="k", fish_model="s2.1-pro")), FishHTTPTTSService)
    assert isinstance(make_tts(Config(tts="fish", fish_key="k", fish_model="s2.1-pro", fish_connection="live")),
                      FishAudioTTSService)
    assert isinstance(make_tts(Config(tts="fish", fish_key="k", fish_connection="live")), FishAudioTTSService)


async def test_http_speaking(fake_url, requests_seen):
    from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame
    from vpengine.fish import FishHTTPTTSService

    tts = FishHTTPTTSService(api_key="fish-good-key", model="s2.1-pro-free", voice="v-1", speed=1.1, volume=-2,
                             temperature=0.5, base_url=fish_base(fake_url))
    frames = [f async for f in tts.run_tts("Hello there.", "c")]
    await tts._close()
    audio = b"".join(f.audio for f in frames if isinstance(f, TTSAudioRawFrame))
    assert len(audio) == 4800 and all(f.sample_rate == 24000 for f in frames if isinstance(f, TTSAudioRawFrame))
    sent = [r for r in requests_seen if r["path"] == "/v1/tts"][-1]
    assert sent["model"] == "s2.1-pro-free" and sent["reference_id"] == "v-1" and sent["format"] == "pcm"
    assert sent["prosody"] == {"speed": 1.1, "volume": -2} and sent["temperature"] == 0.5 and sent["text"] == "Hello there."

    broke = FishHTTPTTSService(api_key="fish-broke-key", model="s2.1-pro", base_url=fish_base(fake_url))
    frames = [f async for f in broke.run_tts("Hi", "c")]
    await broke._close()
    assert isinstance(frames[0], ErrorFrame) and "no API credit for the s2.1-pro voice" in frames[0].error
