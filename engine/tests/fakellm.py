"""Deterministic OpenAI-compatible stand-in for an LLM (stdlib only).

Picks a transition tool when the caller's last message shares words with the
tool's name/description; otherwise replies with text. Lets `vk test` plumbing be
exercised end to end without a real model or API key.
"""
import json, re, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STOP = set("the a an and or to of in on for is are be been it this that with you your caller has have "
           "about wants want says say needs need any some what when from they them their there does do".split())
REQUESTS = []  # bodies seen, for assertions

def words(text):  # crude stems: first 5 letters
    return {w[:5] for w in re.findall(r"[a-záéíóúñ]+", (text or "").lower()) if len(w) > 3 and w not in STOP}

def text_of(msg):
    c = msg.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return c or ""

def interpret(body, props):
    """A scripted page's turn: pick a route whose condition shares words with the caller's, and the answer."""
    msgs = body.get("messages", [])
    system = "\n".join(text_of(m) for m in msgs if m.get("role") == "system")
    user = text_of([m for m in msgs if m.get("role") == "user"][-1]) if any(m.get("role") == "user" for m in msgs) else ""
    options = (props.get("route") or {}).get("enum") or ["none"]
    best, score = "none", 0
    for line in system.splitlines():
        m = re.match(r"\s*- (\w+): (.*)", line)
        if m and m.group(1) in options:
            s2 = len(words(user) & words(m.group(2)))
            if s2 > score:
                best, score = m.group(1), s2
    m = re.search(r"(?:my name is|it's|it is|i am|i'm|call me)\s+(.+)", user, re.I)
    value = (m.group(1) if m else user).strip(" .!") or None
    return {"text": json.dumps({"route": best, "value": None if best != "none" else value})}


def extraction(body):
    """Structured-output request: fill string fields with the caller's words, booleans with true."""
    fmt = body.get("response_format") or {}
    schema = (fmt.get("json_schema") or {}).get("schema") or {}
    msgs = body.get("messages", [])
    said = " ".join(re.findall(r"Caller: (.*)", "\n".join(text_of(m) for m in msgs)))
    out = {}
    for name, spec in (schema.get("properties") or {}).items():
        types = spec.get("type") if isinstance(spec.get("type"), list) else [spec.get("type")]
        out[name] = True if "boolean" in types else (said[-60:] or None)
    return {"text": json.dumps(out)}


DAYS = r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|today|tomorrow)\b"


def arguments(tool, user):
    """Fill a tool's parameters from the caller's words: days, numbers, else the whole message."""
    props = (tool.get("function", {}).get("parameters") or {}).get("properties") or {}
    out = {}
    for name, spec in props.items():
        t = spec.get("type")
        if t == "boolean":
            out[name] = True
        elif t in ("number", "integer"):
            m = re.search(r"\d+", user)
            if m:
                out[name] = int(m.group(0))
        else:
            m = re.search(DAYS, user.lower())
            out[name] = m.group(1) if m else user
    return out


DRAFT = {
    "name": "Hilltop Dental desk", "business_name": "Hilltop Dental",
    "persona": "You are Sam, the friendly front desk at {{business_name}}. Keep answers short.",
    "greeting": "Thanks for calling {{business_name}}, this is Sam. How can I help?",
    "root_instructions": "Find out whether the caller wants an appointment or has a question, then transfer.",
    "goodbye_instructions": "Thank the caller and say goodbye.",
    "variables": [{"name": "business hours", "value": "Monday to Friday, 9 to 5"}],
    "sub_agents": [
        {"name": "Appointments", "label": "book appointment", "when": "The caller wants to book or change an appointment.",
         "instructions": "Ask which day suits them, look up open times, offer two.",
         "collects": [{"name": "preferred day", "type": "string", "description": "the day they want"}], "tools": ["find_open_times"]},
        {"name": "Questions", "label": "general question", "when": "The caller has a question about hours or location.",
         "instructions": "Answer from {{business_hours}}.", "collects": [], "tools": []},
    ],
    "tools": [{"name": "find open times", "description": "Look up open appointment times on a day.", "method": "GET",
               "parameters": [{"name": "day", "type": "string", "description": "the day", "required": True}],
               "example_answer": "{\"times\": [\"9:30 AM\", \"2:00 PM\"]}"}],
}


def decide(body):
    if (body.get("response_format") or {}).get("type") == "json_schema":
        props = (((body.get("response_format") or {}).get("json_schema") or {}).get("schema") or {}).get("properties") or {}
        if "sub_agents" in props:                              # "describe it" drafting
            return {"text": json.dumps(DRAFT)}
        if set(props) == {"route", "value"}:                   # a scripted page reading a caller's turn
            return interpret(body, props)
        return extraction(body)
    msgs, tools = body.get("messages", []), body.get("tools") or []
    if msgs and msgs[-1].get("role") == "tool" and re.search(r'"ok"\s*:', text_of(msgs[-1])):
        return {"text": f"[test model] the tool said {text_of(msgs[-1])[:300]}"}   # an HTTP tool's answer
    last_user = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=None)
    user = text_of(msgs[last_user]) if last_user is not None else ""
    used = {tc["function"]["name"] for m in msgs[(last_user or 0):] for tc in (m.get("tool_calls") or [])}
    hops = len(used)
    best, score = None, 0
    if user and hops < 2:  # a real model rarely chains more than two steps per turn
        uw = words(user)
        for t in tools:
            f = t.get("function", {})
            if f.get("name") in used:
                continue
            s = len(uw & words(f.get("name", "").replace("_", " ") + " " + f.get("description", "")))
            if s > score:
                best, score = f["name"], s
    free = [t["function"]["name"] for t in tools if t.get("function", {}).get("name") not in used]
    if not best and user and hops == 0 and len(free) == 1:
        best = free[0]  # a step with one exit (e.g. the greeting) moves on once the caller speaks
    if best:
        tool = next(t for t in tools if t.get("function", {}).get("name") == best)
        return {"tool": best, "args": arguments(tool, user)}
    system = "\n".join(text_of(m) for m in msgs if m.get("role") == "system")
    lines = [l.strip(" -") for l in system.splitlines() if l.strip()]
    uw = words(user)
    pick = max(lines, key=lambda l: len(uw & words(l)), default="")
    if not uw & words(pick):
        pick = "How can I help you today?"
    return {"text": f"[test model] {pick}"}

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path.startswith("/model"):                    # Fish Audio voice list
            if self.headers.get("Authorization") != "Bearer fish-good-key":
                return self._json(401, {"message": "invalid key", "status": 401})
            mine = "self=true" in self.path
            items = ([{"_id": "my-voice-1", "title": "My clone", "languages": ["en"]}] if mine else
                     [{"_id": "pop-1", "title": "Narrator", "languages": ["en"]}, {"_id": "pop-2", "title": "Friendly", "languages": ["en"]}])
            return self._json(200, {"total": len(items), "items": items})
        if self.path.rstrip("/").endswith("/audio/voices"):
            return self._json(200, {"voices": ["af_heart", "am_adam"]})
        if self.path.rstrip("/").endswith("/models"):
            return self._json(200, {"object": "list", "data": [{"id": "fake-claude", "object": "model", "owned_by": "test"}]})
        self._json(404, {"error": "no route"})
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/v1/asr":                           # Fish Audio speech to text
            REQUESTS.append({"path": self.path, "bytes": len(raw), "model": self.headers.get("model"),
                             "auth": self.headers.get("Authorization"), "wav": b"RIFF" in raw[:400]})
            if self.headers.get("Authorization") == "Bearer fish-broke-key":
                return self._json(402, {"status": 402, "message": "Insufficient API credit."})
            if self.headers.get("Authorization") != "Bearer fish-good-key":
                return self._json(401, {"message": "invalid key", "status": 401})
            return self._json(200, {"text": "<|speaker:0|> I have a parking question", "duration": 1.2, "segments": []})
        if self.path.endswith("/audio/transcriptions"):       # Whisper-style speech to text
            REQUESTS.append({"path": self.path, "bytes": len(raw)})
            return self._json(200, {"text": "I have a parking question"})
        body = json.loads(raw or b"{}")
        if self.path == "/v1/tts":                            # Fish Audio speaking over HTTP: PCM back
            auth = self.headers.get("Authorization")
            REQUESTS.append({"path": self.path, "model": self.headers.get("model"), **body})
            if auth == "Bearer fish-broke-key":
                return self._json(402, {"status": 402, "message": "Insufficient API credit."})
            if auth != "Bearer fish-good-key":
                return self._json(401, {"status": 401, "message": "Invalid Token"})
            data = b"\x02\x00" * 2400
            self.send_response(200); self.send_header("Content-Type", "audio/pcm")
            self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
        if self.path.endswith("/audio/speech"):               # Kokoro-style text to speech: 24 kHz PCM
            REQUESTS.append({"path": self.path, **body})
            data = b"\x01\x00" * 2401                         # odd byte count on purpose: 4802 bytes
            self.send_response(200); self.send_header("Content-Type", "audio/pcm")
            self.send_header("Content-Length", str(len(data) + 1)); self.end_headers()
            self.wfile.write(data + b"\x00"); return
        REQUESTS.append({"path": self.path, **body})
        if not self.path.endswith("/chat/completions"):
            return self._json(404, {"error": "only chat completions"})
        d = decide(body)
        base = {"id": "fake-1", "object": "chat.completion.chunk", "created": int(time.time()), "model": body.get("model")}
        if not body.get("stream"):
            msg = ({"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function",
                    "function": {"name": d["tool"], "arguments": json.dumps(d.get("args") or {})}}]} if "tool" in d
                   else {"role": "assistant", "content": d["text"]})
            return self._json(200, {**base, "object": "chat.completion", "choices": [{"index": 0, "message": msg,
                              "finish_reason": "tool_calls" if "tool" in d else "stop"}],
                              "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.end_headers()
        def send(obj):
            self.wfile.write(b"data: " + json.dumps(obj).encode() + b"\n\n"); self.wfile.flush()
        if "tool" in d:
            send({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1",
                  "type": "function", "function": {"name": d["tool"], "arguments": json.dumps(d.get("args") or {})}}]},
                  "finish_reason": None}]})
            send({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        else:
            for piece in re.findall(r"\S+\s*", d["text"]):
                send({**base, "choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]})
            send({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
        send({**base, "choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}})
        self.wfile.write(b"data: [DONE]\n\n"); self.wfile.flush()


def serve(port=0):
    """Start in a background thread; returns (server, base_url)."""
    import threading
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/v1"


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 9999), H).serve_forever()
