"""Turn taking settings, and the per-turn latency event."""

import pytest
from pipecat.frames.frames import BotStartedSpeakingFrame, MetricsFrame, VADUserStoppedSpeakingFrame
from pipecat.metrics.metrics import TTFBMetricsData, TurnMetricsData
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy, TurnAnalyzerUserTurnStopStrategy

from test_voice import call, names, until  # noqa: F401  (fixture)
from vpengine.services import Config, ConfigError, make_turn_strategies
from vpengine.settings import public_view


def test_smart_turn_is_the_default_with_a_three_second_limit():
    cfg = Config.load({})
    assert cfg.turn_detection == "smart"
    (stop,) = make_turn_strategies(cfg).stop
    assert isinstance(stop, TurnAnalyzerUserTurnStopStrategy)
    assert stop._turn_analyzer._params.stop_secs == 3.0


def test_the_pause_setting_means_the_limit_or_the_fixed_silence():
    (smart,) = make_turn_strategies(Config.load({"VPE_TURN_PAUSE": "2"})).stop
    assert smart._turn_analyzer._params.stop_secs == 2.0
    (pause,) = make_turn_strategies(Config.load({"VPE_TURN_DETECTION": "pause"})).stop
    assert isinstance(pause, SpeechTimeoutUserTurnStopStrategy) and pause._user_speech_timeout == 0.8
    (pause,) = make_turn_strategies(Config.load({"VPE_TURN_DETECTION": "pause", "VPE_TURN_PAUSE": "1.2"})).stop
    assert pause._user_speech_timeout == 1.2


def test_turn_settings_are_checked(tmp_path):
    with pytest.raises(ConfigError, match="between 0.2 and 6"):
        Config.load({"VPE_TURN_PAUSE": "9"})
    with pytest.raises(ConfigError, match="VPE_TURN_DETECTION"):
        Config.load({"VPE_TURN_DETECTION": "sometimes"})
    view = public_view(Config.load({"VPE_TURN_DETECTION": "pause"}), tmp_path / "s.json")
    assert view["fields"]["turn_detection"]["value"] == "pause"
    assert view["status"]["turn_detection"] == "pause"


async def test_each_reply_reports_how_long_the_caller_waited(call):  # noqa: F811
    s, tts = await call()
    await until(lambda: tts.spoken)                     # the greeting is not a reply: no event
    await s.queue([BotStartedSpeakingFrame()])
    await s.queue([
        VADUserStoppedSpeakingFrame(stop_secs=0.2),
        MetricsFrame(data=[TurnMetricsData(processor="turn", is_complete=True, probability=0.9, e2e_processing_time_ms=140.0)]),
        MetricsFrame(data=[TTFBMetricsData(processor=s.stt.name, value=0.12)]),
        MetricsFrame(data=[TTFBMetricsData(processor=s.llm.name, value=0.35)]),
        MetricsFrame(data=[TTFBMetricsData(processor=s.tts.name, value=0.09)]),
        MetricsFrame(data=[TTFBMetricsData(processor=s.llm.name, value=9.0)]),   # a later call: first one counts
        BotStartedSpeakingFrame(),
    ])
    await until(lambda: names(s, "turn_metrics", "response_ms"))
    (m,) = [e for e in s.events if e["type"] == "turn_metrics"]
    assert m["response_ms"] >= 200
    assert (m["turn_ms"], m["listening_ms"], m["model_ms"], m["speaking_ms"]) == (140, 120, 350, 90)

    await s.queue([BotStartedSpeakingFrame()])           # the agent speaking again without a caller turn
    await s.queue([VADUserStoppedSpeakingFrame(stop_secs=0.2), BotStartedSpeakingFrame()])
    await until(lambda: len(names(s, "turn_metrics", "response_ms")) == 2)
    second = [e for e in s.events if e["type"] == "turn_metrics"][1]
    assert "model_ms" not in second                      # nothing carried over from the last turn
