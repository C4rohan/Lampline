"""Moonshine streaming speech to text: settings, readiness, and how lines become transcripts.

No model is downloaded here: a stand-in stream plays Moonshine's events.
"""

from types import SimpleNamespace

import httpx
import pytest

moonshine_voice = pytest.importorskip("moonshine_voice")
from moonshine_voice.transcriber import LineCompleted, LineTextChanged, TranscriptLine  # noqa: E402
from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame  # noqa: E402

from vpengine import moonshine_stt  # noqa: E402
from vpengine.server import Engine, create_app  # noqa: E402
from vpengine.services import Config, ConfigError, make_stt  # noqa: E402
from vpengine.settings import check, local_speech_problem  # noqa: E402


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(moonshine_stt, "model_dir", lambda: tmp_path / "moonshine")
    moonshine_stt._transcribers.clear()
    return tmp_path / "moonshine"


def fake_download(arch):
    group = moonshine_stt._group(arch)
    root = moonshine_stt._root(group)
    for f in group["files"]:
        (root / f["name"]).parent.mkdir(parents=True, exist_ok=True)
        (root / f["name"]).write_bytes(b"x")


def line(line_id, text, complete=False):
    return TranscriptLine(text=text, start_time=0.0, duration=1.0, line_id=line_id, is_complete=complete)


async def test_settings_and_readiness(cache):
    cfg = Config.load({"VPE_STT": "moonshine"})
    assert cfg.moonshine_model == "small-streaming" and not cfg.stt_ready
    with pytest.raises(ConfigError, match="isn't downloaded yet"):
        make_stt(cfg)
    assert "Moonshine model isn't downloaded" in await local_speech_problem(cfg)
    assert (await check("stt", cfg)) == {"ok": False, "message": "download the model first"}

    fake_download("small-streaming")
    assert cfg.stt_ready and await local_speech_problem(cfg) is None
    assert (await check("moonshine", cfg))["ok"]
    assert not Config.load({"VPE_STT": "moonshine", "VPE_MOONSHINE_MODEL": "tiny-streaming"}).stt_ready
    assert cfg.describe()["stt_model"] == "small-streaming"
    with pytest.raises(ConfigError, match="VPE_MOONSHINE_MODEL"):
        Config.load({"VPE_MOONSHINE_MODEL": "medium-streaming"})


async def test_status_endpoint_names_the_model_and_size(cfg, agents_dir):
    from dataclasses import replace
    engine = Engine(agents_dir, replace(cfg, moonshine_model="tiny-streaming"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(engine)), base_url="http://localhost") as c:
        st = (await c.get("/v1/moonshine")).json()
    assert st["installed"] and not st["ready"] and st["model"] == "tiny-streaming" and st["size_mb"] == 46


class FakeStream:
    def __init__(self):
        self.audio = []
        self.forced = None          # what update_transcription returns

    def add_audio(self, samples, rate):
        self.audio.append((len(samples), rate))

    def update_transcription(self, flags=0):
        return self.forced


@pytest.fixture
def stt():
    s = moonshine_stt.MoonshineSTTService()
    s._stream = FakeStream()
    s._user_id = ""
    s._sample_rate = 8000
    s.pushed = []

    async def push(frame, direction=None):
        s.pushed.append(frame)

    s.push_frame = push
    return s


async def test_audio_goes_in_at_16_khz(stt):
    for _ in range(50):                                             # 1 s at 8 kHz, 20 ms at a time
        out = [f async for f in stt.run_stt(b"\x00\x01" * 160)]
        assert out in ([], [None])
    assert stt._stream.audio and {rate for _, rate in stt._stream.audio} == {16000}
    assert 13600 <= sum(n for n, _ in stt._stream.audio) <= 16000   # 1 s at 16 kHz, less what the resampler holds


async def test_interim_text_then_a_completed_line(stt):
    stt._events += [LineTextChanged(line=line(1, "I'd like"), stream_handle=0),
                    LineTextChanged(line=line(1, "I'd like to book"), stream_handle=0),
                    LineCompleted(line=line(1, "I'd like to book.", True), stream_handle=0)]
    frames = stt._drain()
    assert [type(f) for f in frames] == [InterimTranscriptionFrame, InterimTranscriptionFrame, TranscriptionFrame]
    assert frames[-1].text == "I'd like to book."


async def test_the_caller_pausing_finishes_the_line_at_once(stt):
    stt._stream.forced = SimpleNamespace(lines=[line(2, "Tuesday morning please")])
    await stt._finish_line()
    (final,) = stt.pushed
    assert isinstance(final, TranscriptionFrame) and final.finalized and final.text == "Tuesday morning please"

    # Moonshine later completes the same line: nothing is sent twice, interim text stops
    stt._events += [LineTextChanged(line=line(2, "Tuesday morning please"), stream_handle=0),
                    LineCompleted(line=line(2, "Tuesday morning please.", True), stream_handle=0)]
    assert stt._drain() == []

    # ...or with more words the caller added after a short pause: only the new part goes out
    stt._stream.forced = SimpleNamespace(lines=[line(3, "My name is Sam")])
    await stt._finish_line()
    stt._events.append(LineCompleted(line=line(3, "My name is Sam Rivera", True), stream_handle=0))
    (rest,) = stt._drain()
    assert rest.text == "Rivera"

    # a revision of a line already sent is not sent again (the agent keeps what it heard)
    stt._events.append(LineCompleted(line=line(3, "My name's Sam Rivera", True), stream_handle=0))
    assert stt._drain() == []


async def test_nothing_new_means_nothing_sent(stt):
    stt._stream.forced = SimpleNamespace(lines=[line(4, "Yes.", True)])     # already complete: sent by its event
    await stt._finish_line()
    stt._stream.forced = SimpleNamespace(lines=[line(5, "  ")])
    await stt._finish_line()
    assert stt.pushed == []
