"""The engine for the Phone Bridge end-to-end test (engine/tests/e2e/bridge.e2e.mjs).

A real engine on a port, with the fake model and two speech stand-ins that work on real audio:
  - ToneTTS speaks every sentence as a loud 440 Hz tone, so the far end of the phone call can measure it;
  - EnergySTT "hears" any loud audio as the agent saying a fixed sentence, and records how loud it was.

    python engine/tests/e2e/bridge_server.py <port> <work folder>

Prints "READY <url>" when listening. GET /e2e/heard returns the loudest audio the Test Caller received.
"""

from __future__ import annotations

import array
import math
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import uvicorn  # noqa: E402
import yaml  # noqa: E402
from pipecat.frames.frames import TranscriptionFrame, TTSAudioRawFrame  # noqa: E402
from pipecat.services.settings import STTSettings, TTSSettings  # noqa: E402
from pipecat.services.stt_service import STTService  # noqa: E402
from pipecat.services.tts_service import TTSService  # noqa: E402
from pipecat.utils.time import time_now_iso8601  # noqa: E402

import fakellm  # noqa: E402
from vpengine.server import Engine, create_app  # noqa: E402
from vpengine.services import Config  # noqa: E402

TEMPLATE = HERE.parents[2] / "cli" / "src" / "vk" / "templates" / "test-caller"
HEARD = {"peak": 0.0, "lines": 0}


def rms(pcm: bytes) -> float:
    a = array.array("h", pcm[: len(pcm) // 2 * 2])
    return math.sqrt(sum(x * x for x in a) / len(a)) / 32768 if a else 0.0


class EnergySTT(STTService):
    def __init__(self, **kw):
        super().__init__(settings=STTSettings(model=None, language=None), ttfs_p99_latency=0.1, **kw)
        self.loud = 0

    async def run_stt(self, audio: bytes):
        level = rms(audio)
        HEARD["peak"] = max(HEARD["peak"], level)
        self.loud = self.loud + 1 if level > 0.02 else 0
        if self.loud == 15 and HEARD["lines"] < 2:          # a moment of steady sound: the agent said something
            HEARD["lines"] += 1
            yield TranscriptionFrame(text="Thanks for calling Riverside Dental. How can I help?", user_id="agent",
                                     timestamp=time_now_iso8601(), finalized=True)
        else:
            yield None


class ToneTTS(TTSService):
    def __init__(self, **kw):
        super().__init__(sample_rate=24000, push_start_frame=True, push_stop_frames=True, stop_frame_timeout_s=0.1,
                         settings=TTSSettings(model=None, voice=None, language=None), **kw)

    async def run_tts(self, text: str, context_id: str):
        n = 24000 * 2                                         # two seconds per sentence
        tone = array.array("h", (int(12000 * math.sin(2 * math.pi * 440 * i / 24000)) for i in range(n)))
        yield TTSAudioRawFrame(audio=tone.tobytes(), sample_rate=24000, num_channels=1, context_id=context_id)


def main() -> None:
    from loguru import logger
    logger.remove()
    logger.add(sys.stderr, level="ERROR")
    port, work = int(sys.argv[1]), Path(sys.argv[2])
    agents = work / "agents"
    shutil.rmtree(agents, ignore_errors=True)
    shutil.copytree(TEMPLATE, agents / "test-caller")
    # For this test the caller speaks first, so its voice reaches the far end without waiting on turn-taking.
    doc = yaml.safe_load((agents / "test-caller" / "agent.yaml").read_text())
    start = next(n for n in doc["nodes"] if n["type"] == "startCall")
    start["data"].update(listen_first=False, greeting="Hi, this is Sam calling about my appointment.")
    (agents / "test-caller" / "agent.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    _, llm_url = fakellm.serve()
    engine = Engine(agents, Config(llm="openai", model="fake-model", base_url=llm_url), data_dir=work / "data",
                    speech_factory=lambda: (EnergySTT(), ToneTTS()))
    app = create_app(engine)

    @app.get("/e2e/heard")
    async def heard():
        return HEARD

    print(f"READY http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
