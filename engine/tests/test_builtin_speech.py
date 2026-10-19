"""Built-in speech with the real models: Kokoro says a sentence, Whisper hears it back.

Downloads about 500 MB the first time, so it only runs with VPE_TEST_MODELS=1
(models cache in VPE_MODEL_CACHE, default ~/.cache/vpengine).
"""

import asyncio
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("VPE_TEST_MODELS") != "1", reason="set VPE_TEST_MODELS=1 to download and run the real models")


async def test_download_speak_and_hear_back():
    from pipecat.frames.frames import TTSAudioRawFrame
    from pipecat.audio.utils import create_stream_resampler
    from vpengine import builtin_speech as bs

    d = bs.Downloads()
    d.start("base.en")
    while d.state == "downloading":
        await asyncio.sleep(0.5)
    assert d.state == "idle", d.message
    st = d.status("base.en")
    assert st["kokoro_ready"] and st["whisper_ready"] and st["installed"]

    tts = bs.BuiltinKokoroTTS(voice="af_heart")
    frames = [f async for f in tts.run_tts("I have a question about parking.", "ctx")]
    audio = [f for f in frames if isinstance(f, TTSAudioRawFrame)]
    assert audio, frames
    pcm = b"".join(f.audio for f in audio)
    assert audio[0].sample_rate == 24000 and len(pcm) > 24000          # more than half a second

    pcm16k = await create_stream_resampler().resample(pcm, 24000, 16000)
    stt = bs.make_builtin_stt("base.en")
    heard = [f async for f in stt.run_stt(pcm16k)]
    text = " ".join(getattr(f, "text", "") for f in heard).lower()
    assert "parking" in text, heard


async def test_a_call_with_built_in_speech(cfg):
    """Greeting is spoken by Kokoro; the caller's (synthesized) voice is heard by Whisper and moves the call."""
    from dataclasses import replace
    from pipecat.audio.utils import create_stream_resampler
    from pipecat.frames.frames import (InputAudioRawFrame, OutputAudioRawFrame, TTSAudioRawFrame,
                                       VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
    import voicefakes as vf
    from conftest import mini_definition
    from vpengine import builtin_speech as bs
    from vpengine.graph import Graph
    from vpengine.services import make_stt, make_tts
    from vpengine.session import VoiceSession

    local = replace(cfg, stt="whisper", tts="kokoro")
    assert local.voice_ready
    caller = b"".join([f.audio async for f in bs.BuiltinKokoroTTS(voice="am_adam").run_tts(
        "Hi, I have a parking question.", "c") if isinstance(f, TTSAudioRawFrame)])
    caller16k = await create_stream_resampler().resample(caller, 24000, 16000)

    g = Graph.from_definition("Mini", mini_definition(), {"business_name": "Riverside"})
    transport = vf.FakeTransport()
    s = VoiceSession(g, local, transport, stt=make_stt(local), tts=make_tts(local), vad=None)
    runner = asyncio.create_task(s.run())

    async def until(cond, timeout=60):
        end = asyncio.get_running_loop().time() + timeout
        while not cond():
            assert asyncio.get_running_loop().time() < end, s.events
            await asyncio.sleep(0.1)
    try:
        spoken = lambda: sum(len(f.audio) for f in transport.out_frames if isinstance(f, (TTSAudioRawFrame, OutputAudioRawFrame)))
        await until(lambda: spoken() > 24000)                     # the greeting came out as audio
        await s.queue([VADUserStartedSpeakingFrame()])
        step = 3200                                               # 0.1 s at 16 kHz
        await s.queue([InputAudioRawFrame(audio=caller16k[i:i + step], sample_rate=16000, num_channels=1)
                       for i in range(0, len(caller16k), step)])
        await s.queue([VADUserStoppedSpeakingFrame()])
        await until(lambda: any(e["type"] == "node_transition" and e["node_name"] == "Help" for e in s.events))
        heard = [e["text"].lower() for e in s.events if e["type"] == "user_speech"]
        assert any("parking" in h for h in heard), heard
    finally:
        await s.stop()
        await asyncio.wait({runner}, timeout=5)
