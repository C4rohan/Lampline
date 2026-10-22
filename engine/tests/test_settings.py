"""Integration settings: stored outside the repo, keys never shown, providers wired up."""

import asyncio
import json
import os
import stat

import httpx
import pytest

from vpengine.server import Engine, create_app
from vpengine.services import Config, ConfigError, SpeechEndpointTTSService, make_stt, make_tts
from vpengine.settings import SettingsStore, check, public_view

KEY = "sk-ant-test-0000000000001234"


@pytest.fixture
def store(tmp_path):
    return SettingsStore(tmp_path / "cfg" / "settings.json", env={"VPE_LLM": "anthropic"})


def test_defaults_env_then_saved(tmp_path):
    env = {"ANTHROPIC_API_KEY": "from-env-key-abcd", "VPE_TTS": "kokoro-server", "BRAND_NAME": '"Acme Voice"'}
    s = SettingsStore(tmp_path / "s.json", env=env)
    cfg = s.config()
    assert cfg.anthropic_key == "from-env-key-abcd" and cfg.tts == "kokoro-server" and cfg.brand_name == "Acme Voice"
    assert cfg.sources["tts"] == "env" and "stt" not in cfg.sources
    cfg = s.update({"tts": "fish", "brand_name": "Riverside Voice"})
    assert cfg.tts == "fish" and cfg.sources["tts"] == "saved" and cfg.brand_name == "Riverside Voice"
    cfg = s.update({"tts": ""})                     # clearing a saved value falls back to env
    assert cfg.tts == "kokoro-server" and cfg.sources["tts"] == "env"


def test_file_is_private_and_outside_the_repo(store):
    store.update({"anthropic_key": KEY})
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700
    assert json.loads(store.path.read_text()) == {"anthropic_key": KEY}


def test_bad_values_are_rejected_and_not_written(store):
    with pytest.raises(ConfigError, match="VPE_TTS must be one of"):
        store.update({"tts": "elevenlabs"})
    with pytest.raises(ConfigError, match="unknown setting"):
        store.update({"evil": "x"})
    with pytest.raises(ConfigError, match="temperature"):
        store.update({"temperature": "warm"})
    assert not store.path.exists()


def test_public_view_never_contains_keys(store):
    cfg = store.update({"anthropic_key": KEY, "fish_key": "fish-secret-5678abcd", "tts": "fish"})
    view = public_view(cfg, store.path)
    text = json.dumps(view)
    assert KEY not in text and "fish-secret" not in text
    assert view["fields"]["anthropic_key"] == {"label": "Anthropic API key", "source": "saved",
                                               "env": "ANTHROPIC_API_KEY", "set": True, "hint": "…1234"}
    assert view["fields"]["tts"]["value"] == "fish" and view["fields"]["tts"]["choices"] == ["deepgram", "fish", "kokoro", "kokoro-server", "qwen3-tts"]
    assert view["status"]["tts"] == "fish" and view["status"]["tts_ready"]


def test_provider_switch_picks_a_default_model():
    assert Config.load({"VPE_LLM": "openai"}).model == "gpt-4.1-mini"
    assert Config.load({}).model == "claude-haiku-5-5"
    assert Config.load({"VPE_LLM": "openai", "VPE_LLM_MODEL": "llama3"}).model == "llama3"


def test_readiness():
    assert not Config().voice_ready
    assert Config(deepgram_key="k").voice_ready
    assert Config(stt="whisper-server", tts="kokoro-server").voice_ready          # local servers, no keys
    assert not Config(stt="whisper-server", tts="fish").voice_ready         # Fish needs its key
    assert Config(stt="whisper-server", tts="fish", fish_key="k").voice_ready


def test_speech_services_are_built_for_each_provider():
    from pipecat.services.deepgram.stt import DeepgramSTTService
    from pipecat.services.fish.tts import FishAudioTTSService
    from pipecat.services.openai.stt import OpenAISTTService

    assert isinstance(make_stt(Config(deepgram_key="k")), DeepgramSTTService)
    assert isinstance(make_stt(Config(stt="whisper-server")), OpenAISTTService)
    fish = make_tts(Config(tts="fish", fish_key="k", fish_voice="voice-123", fish_connection="live"))
    assert isinstance(fish, FishAudioTTSService)
    assert isinstance(make_tts(Config(tts="kokoro-server")), SpeechEndpointTTSService)
    with pytest.raises(ConfigError, match="Fish Audio API key"):
        make_tts(Config(tts="fish"))


async def test_kokoro_style_speech_server(fake_url, requests_seen):
    from pipecat.frames.frames import ErrorFrame, TTSAudioRawFrame

    tts = SpeechEndpointTTSService(base_url=fake_url, voice="af_heart")
    frames = [f async for f in tts.run_tts("Hello there.", "ctx")]
    await tts._close()
    audio = b"".join(f.audio for f in frames if isinstance(f, TTSAudioRawFrame))
    assert len(audio) == 4802 and len(audio) % 2 == 0 and all(f.sample_rate == 24000 for f in frames)
    sent = [r for r in requests_seen if r["path"].endswith("/audio/speech")][-1]
    assert sent["voice"] == "af_heart" and sent["response_format"] == "pcm" and sent["input"] == "Hello there."
    broken = SpeechEndpointTTSService(base_url="http://127.0.0.1:9/v1", voice="af_heart")
    frames = [f async for f in broken.run_tts("Hi", "ctx")]
    await broken._close()
    assert isinstance(frames[0], ErrorFrame) and "cannot reach" in frames[0].error


async def test_connection_checks(fake_url):
    assert (await check("stt", Config(stt="whisper-server", whisper_url=fake_url)))["ok"]
    assert (await check("tts", Config(tts="kokoro-server", kokoro_url=fake_url)))["ok"]
    assert (await check("llm", Config(llm="openai", base_url=fake_url)))["ok"]
    down = await check("tts", Config(tts="kokoro-server", kokoro_url="http://127.0.0.1:9/v1"))
    assert not down["ok"] and "could not connect" in down["message"]
    assert (await check("tts", Config(tts="fish")))["message"] == "no API key saved"


# -- through the API -------------------------------------------------------------

@pytest.fixture
async def api(cfg, agents_dir, tmp_path):
    store = SettingsStore(tmp_path / "settings.json", env={"VPE_LLM": "openai", "VPE_LLM_BASE_URL": cfg.base_url,
                                                           "VPE_LLM_MODEL": "fake-model"})
    engine = Engine(agents_dir, store.config(), store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)),
                                 base_url="http://localhost", timeout=30) as c:
        yield c, engine


async def test_settings_api_round_trip(api):
    c, engine = api
    view = (await c.get("/v1/settings")).json()
    assert view["fields"]["llm"]["value"] == "openai" and view["fields"]["llm"]["source"] == "env"
    r = await c.put("/v1/settings", json={"brand_name": "Riverside Voice", "deepgram_key": "dg-secret-key-9876"})
    assert r.status_code == 200 and "dg-secret" not in r.text
    assert r.json()["fields"]["deepgram_key"]["hint"] == "…9876"
    assert engine.cfg.voice_ready                                   # new sessions use it right away
    assert (await c.get("/v1/health")).json()["brand"] == "Riverside Voice"
    assert (await c.put("/v1/settings", json={"stt": "carrier-pigeon"})).status_code == 422


async def test_settings_cannot_be_changed_through_a_tunnel(api):
    c, _ = api
    r = await c.put("/v1/settings", json={"brand_name": "x"}, headers={"cf-connecting-ip": "203.0.113.9"})
    assert r.status_code == 403
    r = await c.put("/v1/settings", json={"brand_name": "x"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403


async def test_settings_check_endpoint(api, fake_url):
    c, _ = api
    assert (await c.post("/v1/settings/check/llm")).json()["ok"]
    assert (await c.post("/v1/settings/check/nonsense")).status_code == 404


async def test_calls_explain_a_local_speech_server_that_is_not_running(cfg, agents_dir, fake_url):
    from dataclasses import replace
    down = replace(cfg, stt="whisper-server", whisper_url=fake_url, tts="kokoro-server", kokoro_url="http://127.0.0.1:9/v1")
    engine = Engine(agents_dir, down)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost") as c:
        r = await c.post("/v1/calls/webrtc/offer", json={"agent": "front-desk", "sdp": "x", "type": "offer"})
    assert r.status_code == 503 and "Kokoro isn't running" in r.json()["detail"] and "Start local speech" in r.json()["detail"]


def test_auto_download_fetches_only_what_the_settings_choose(cfg, agents_dir, tmp_path, monkeypatch):
    """scripts/*-up.sh --free set VPE_AUTO_DOWNLOAD=on: the chosen local speech models download at start."""
    import dataclasses
    from vpengine import builtin_speech, moonshine_stt
    from vpengine.server import Engine
    from vpengine.services import Config

    assert Config.load(env={"VPE_AUTO_DOWNLOAD": "on"}).auto_download == "on" and Config().auto_download == "off"
    started = []
    monkeypatch.setattr(builtin_speech, "kokoro_ready", lambda: False)
    monkeypatch.setattr(moonshine_stt, "ready", lambda arch=None: False)
    engine = Engine(agents_dir, dataclasses.replace(cfg, stt="moonshine", tts="kokoro"), data_dir=tmp_path / "data")
    monkeypatch.setattr(engine.downloads, "start", lambda m: started.append(("builtin", m)))
    monkeypatch.setattr(engine.moonshine, "start", lambda m: started.append(("moonshine", m)))
    assert engine.start_downloads() == ["whisper and kokoro", "moonshine"] and len(started) == 2
    started.clear()
    engine.cfg = dataclasses.replace(cfg, stt="deepgram", tts="deepgram")      # cloud speech: nothing to fetch
    assert engine.start_downloads() == [] and started == []

    def missing(m):
        raise RuntimeError("not installed here")
    engine.cfg = dataclasses.replace(cfg, stt="deepgram", tts="kokoro")
    monkeypatch.setattr(engine.downloads, "start", missing)
    assert engine.start_downloads() == []                                       # says so in the log, keeps running
