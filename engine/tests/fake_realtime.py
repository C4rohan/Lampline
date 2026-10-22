"""A tiny stand-in for an OpenAI Realtime server: enough of the protocol to drive a call in tests."""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import uuid

import websockets

RATE = 24000
USAGE = {"total_tokens": 2, "input_tokens": 1, "output_tokens": 1, "input_token_details": {}, "output_token_details": {}}


def _id() -> str:
    return uuid.uuid4().hex[:12]


class FakeRealtime:
    def __init__(self, reply: str = "Sure, I can help with that."):
        self.reply = reply
        self.seen: list[dict] = []            # client events, in order
        self.caller_turn: tuple[str, str] | None = None   # (what the caller says, tool the model calls)
        self.speak_delay = 0.0               # seconds before answering a response.create
        self.turn_done_delay = 0.0           # seconds before the caller turn's response.done (a slow server)
        self._appended = 0
        self.url = ""
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        threading.Thread(target=self._run, daemon=True).start()
        self._ready.wait(5)

    def _run(self):
        self._loop = asyncio.new_event_loop()
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        async with websockets.serve(self._handle, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            self.url = f"ws://127.0.0.1:{port}/v1/realtime"
            self._ready.set()
            await asyncio.Future()

    def of(self, kind: str) -> list[dict]:
        return [e for e in self.seen if e.get("type") == kind]

    async def _send(self, ws, **evt):
        await ws.send(json.dumps({"event_id": _id(), **evt}))

    async def _handle(self, ws):
        await self._send(ws, type="session.created", session={})
        async for raw in ws:
            evt = json.loads(raw)
            kind = evt.get("type")
            if kind != "input_audio_buffer.append":
                self.seen.append(evt)
            if kind == "session.update":
                await self._send(ws, type="session.updated", session={})
            elif kind == "response.create":
                instr = (evt.get("response") or {}).get("instructions") or ""
                text = instr.split("nothing else: ", 1)[1] if "nothing else: " in instr else self.reply
                if self.speak_delay:
                    asyncio.get_running_loop().create_task(self._speak(ws, text))
                else:
                    await self._speak(ws, text)
            elif kind == "input_audio_buffer.append":
                self._appended += 1
                if self._appended == 3 and self.caller_turn:           # the caller starts talking: their turn
                    await self._turn(ws, *self.caller_turn)

    async def _speak(self, ws, text: str):
        if self.speak_delay:
            await asyncio.sleep(self.speak_delay)
        rid, iid = _id(), _id()
        await self._send(ws, type="conversation.item.added",
                         item={"id": iid, "object": "realtime.item", "type": "message", "role": "assistant", "content": []})
        audio = base64.b64encode(b"\x10\x00" * RATE).decode()          # a second: resamplers hold back ~0.1 s
        common = dict(response_id=rid, item_id=iid, output_index=0, content_index=0)
        await self._send(ws, type="response.output_audio.delta", delta=audio, **common)
        await self._send(ws, type="response.output_audio_transcript.delta", delta=text, **common)
        await self._send(ws, type="response.done", response={"id": rid, "object": "realtime.response", "status": "completed",
                                                             "status_details": None, "output": [], "usage": USAGE})

    async def _turn(self, ws, said: str, tool: str):
        iid = _id()
        await self._send(ws, type="conversation.item.input_audio_transcription.completed", item_id=iid,
                         content_index=0, transcript=said)
        rid, fid, call = _id(), _id(), "call_" + _id()
        await self._send(ws, type="conversation.item.added",
                         item={"id": fid, "object": "realtime.item", "type": "function_call", "call_id": call, "name": tool,
                               "arguments": "{}"})
        await self._send(ws, type="response.function_call_arguments.done", response_id=rid, item_id=fid, output_index=0,
                         call_id=call, arguments="{}")
        done = dict(type="response.done", response={"id": rid, "object": "realtime.response", "status": "completed",
                                                             "status_details": None, "output": [], "usage": USAGE})
        if self.turn_done_delay:                                       # a slow server: the done arrives later,
            async def later():                                        # after the client has moved on
                await asyncio.sleep(self.turn_done_delay)
                await self._send(ws, **done)
            asyncio.get_running_loop().create_task(later())
        else:
            await self._send(ws, **done)
