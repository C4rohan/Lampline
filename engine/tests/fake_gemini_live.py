"""A stand-in for Gemini Live's session (google-genai's AsyncSession): enough to drive a call in tests.

It answers the way the real service does, with google.genai types: audio in model_turn parts, what it said in
output_transcription, then turn_complete; the caller's words in input_transcription; tool calls in tool_call.
"""

from __future__ import annotations

import asyncio
import contextlib
import re

from google.genai import types

RATE = 24000
USAGE = types.UsageMetadata(prompt_token_count=10, response_token_count=5, total_token_count=15)


class FakeGeminiLive:
    def __init__(self, reply: str = "Sure, I can help with that."):
        self.reply = reply
        self.configs: list = []                 # each connect's LiveConnectConfig
        self.turns: list[tuple[str, bool]] = []  # (text sent as a turn, turn_complete)
        self.tool_responses: list[dict] = []
        self.audio_in = 0
        self.caller_turn: tuple[str, str] | None = None   # (what the caller says, the route tool the model calls)
        self._queue: asyncio.Queue | None = None

    # the client: client.aio.live.connect(model=..., config=...) -> async context manager yielding the session
    @property
    def aio(self):
        return self

    @property
    def live(self):
        return self

    @contextlib.asynccontextmanager
    async def connect(self, *, model, config):
        self.configs.append(config)
        self._queue = asyncio.Queue()
        yield self

    # the session
    async def send_client_content(self, *, turns=None, turn_complete=True):
        text = " ".join(p.text or "" for t in (turns or []) for p in (t.parts or []))
        self.turns.append((text, turn_complete))
        if turn_complete:
            m = re.search(r"nothing else: (.*)$", text, re.S)
            await self._speak(m.group(1) if m else self.reply)

    async def send_realtime_input(self, *, audio=None, text=None, **kw):
        if audio is not None:
            self.audio_in += 1
            if self.audio_in == 3 and self.caller_turn:
                said, tool = self.caller_turn
                await self._put(types.LiveServerMessage(server_content=types.LiveServerContent(
                    input_transcription=types.Transcription(text=said))))
                await self._put(types.LiveServerMessage(tool_call=types.LiveServerToolCall(
                    function_calls=[types.FunctionCall(id="fc_1", name=tool, args={})])))

    async def send_tool_response(self, *, function_responses):
        responses = function_responses if isinstance(function_responses, list) else [function_responses]
        for r in responses:
            self.tool_responses.append({"id": r.id, "name": r.name, "response": dict(r.response or {})})
        await self._speak(self.reply)

    async def _speak(self, text: str):
        audio = b"\x10\x00" * RATE                                   # a second: resamplers hold back ~0.1 s
        await self._put(types.LiveServerMessage(server_content=types.LiveServerContent(
            model_turn=types.Content(role="model", parts=[types.Part(inline_data=types.Blob(
                data=audio, mime_type=f"audio/pcm;rate={RATE}"))]))))
        await self._put(types.LiveServerMessage(server_content=types.LiveServerContent(
            output_transcription=types.Transcription(text=text))))
        await self._put(types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True),
                                                usage_metadata=USAGE))

    async def _put(self, msg):
        await self._queue.put(msg)

    async def receive(self):
        while True:
            msg = await self._queue.get()
            if msg is None:
                return
            yield msg

    async def close(self):
        if self._queue is not None:
            await self._queue.put(None)
