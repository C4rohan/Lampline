"""Evals: real phone calls to a voice agent, placed by you and played by a Test Caller.

A run says who calls (a caller agent, usually the Test Caller template), which number, how the audio gets there,
who the caller is and what they want, and what has to be true afterwards. The engine plays the caller; the call
itself goes out through:

    google_voice   Google Voice in Chrome or Edge. The Lampline Phone Bridge extension connects that tab's call
                   audio to the Test Caller directly (no cables, Windows or Mac). You press Call in Google Voice.
    speaker        any phone on speaker next to the computer (or a virtual audio cable): the Evals page itself
                   uses the chosen microphone and speaker.

When the conversation ends, the engine's model grades the transcript against the goal and each criterion.

Runs are kept in evals.sqlite3 next to the call history (schema version in PRAGMA user_version). They hold the
number called and what was said, so they follow the call-history rule: this computer, or VPE_REMOTE_EDITING.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .pricing import percentile

log = logging.getLogger("vpengine.evals")

SCHEMA_VERSION = 3
METHODS = ("google_voice", "speaker")
BRIDGE_TTL = 20 * 60                    # a bridge token is good for 20 minutes, until its call starts
MAX_CRITERIA = 12
ID = re.compile(r"^ev_[A-Za-z0-9_-]{8,40}$")
CASE_ID = re.compile(r"^tc_[A-Za-z0-9_-]{8,40}$")
MAX_CASES = 500
MAX_SCRIPT = 120_000                    # characters of a test script sent to the model
PHONE = re.compile(r"^\+[1-9]\d{6,14}$")
DONE = ("passed", "failed", "error", "cancelled")

GRADER = """You grade a test phone call. A test caller phoned a business's voice agent; you read the transcript and
decide whether the call went the way it should. "Agent" is the voice agent being tested; "Caller" is the test caller.

Judge only from the transcript. A criterion passes only if the transcript clearly shows it; when it's unclear,
it fails and the reason says what was missing. Keep each reason to one short sentence.
Reply with one JSON object and nothing else."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["goal_met", "criteria", "summary"],
          "properties": {
              "goal_met": {"type": "boolean"},
              "summary": {"type": "string"},
              "criteria": {"type": "array", "items": {
                  "type": "object", "additionalProperties": False, "required": ["criterion", "passed", "reason"],
                  "properties": {"criterion": {"type": "string"}, "passed": {"type": "boolean"},
                                 "reason": {"type": "string"}}}}}}


class EvalError(ValueError):
    """A run that can't be started as asked (shown to the person)."""


def normalize_number(raw: str) -> str:
    digits = re.sub(r"[^\d+]", "", str(raw or ""))
    if digits and not digits.startswith("+"):
        digits = "+" + (digits if len(digits) > 10 else "1" + digits)      # 10 digits: a US/Canada number
    if not PHONE.match(digits):
        raise EvalError("the number to call needs its country code, like +15551234567")
    return digits


def google_voice_url(number: str) -> str:
    return "https://voice.google.com/u/0/calls?a=nc,%2B" + number.lstrip("+")


def transcript(events: list[dict]) -> list[dict]:
    """What was said, from the Test Caller's side: its own lines are the caller, what it heard is the agent."""
    out = []
    for e in events:
        if e.get("type") == "user_speech" and e.get("text"):
            out.append({"who": "agent", "text": str(e["text"]), "t": e.get("t")})
        elif e.get("type") == "bot_speech" and e.get("text") and not e.get("filler"):
            out.append({"who": "caller", "text": str(e["text"]), "t": e.get("t"),
                        **({"interruption": True} if e.get("interruption") else {})})
    return out


class EvalStore:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._db: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        self._bridges: dict[str, tuple[str, float]] = {}       # token -> (run id, expiry)

    @property
    def path(self) -> Path:
        return self.folder / "evals.sqlite3"

    def db(self) -> sqlite3.Connection:
        with self._lock:
            if self._db is None:
                self.folder.mkdir(parents=True, exist_ok=True)
                db = sqlite3.connect(self.path, check_same_thread=False)
                db.row_factory = sqlite3.Row
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    db.close()
                    raise RuntimeError(f"{self.path} was written by a newer Lampline (schema {version}); "
                                       "upgrade the engine to read it")
                if version < 1:
                    db.executescript("""
                        CREATE TABLE IF NOT EXISTS runs (
                            id TEXT PRIMARY KEY, created REAL NOT NULL, caller_agent TEXT NOT NULL,
                            method TEXT NOT NULL, number TEXT NOT NULL, persona TEXT NOT NULL, goal TEXT NOT NULL,
                            criteria TEXT NOT NULL, max_minutes INTEGER NOT NULL, status TEXT NOT NULL,
                            session_id TEXT, started REAL, ended REAL, ended_reason TEXT,
                            transcript TEXT, results TEXT, summary TEXT, error TEXT, by TEXT);
                        CREATE INDEX IF NOT EXISTS runs_created ON runs (created DESC);
                        PRAGMA user_version = 1;
                    """)
                    version = 1
                if version < 2:                    # v2: saved test cases, and which case a run came from
                    db.executescript("""
                        CREATE TABLE IF NOT EXISTS cases (
                            id TEXT PRIMARY KEY, created REAL NOT NULL, name TEXT NOT NULL, persona TEXT NOT NULL,
                            goal TEXT NOT NULL, criteria TEXT NOT NULL, number TEXT NOT NULL DEFAULT '',
                            source TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '');
                        ALTER TABLE runs ADD COLUMN case_id TEXT;
                        PRAGMA user_version = 2;
                    """)
                    version = 2
                if version < 3:                    # v3: a tougher caller's options, the agent timed from the caller
                    db.executescript("""
                        ALTER TABLE runs ADD COLUMN options TEXT;
                        ALTER TABLE runs ADD COLUMN latencies TEXT;
                        PRAGMA user_version = 3;
                    """)
                self._db = db
            return self._db

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # ------------------------------------------------------------------ runs
    def create(self, *, caller_agent: str, method: str, number: str, persona: str, goal: str,
               criteria: list[str], max_minutes: int, by: str = "", case_id: str = "",
               options: dict | None = None) -> dict:
        if method not in METHODS:
            raise EvalError("method is google_voice or speaker")
        number = normalize_number(number)
        persona, goal = str(persona or "").strip()[:3000], str(goal or "").strip()[:500]
        if not persona or not goal:
            raise EvalError("describe who is calling (persona) and what they want (goal)")
        crit = [str(c).strip()[:300] for c in (criteria or []) if str(c).strip()]
        if len(crit) > MAX_CRITERIA:
            raise EvalError(f"at most {MAX_CRITERIA} criteria")
        minutes = max(1, min(int(max_minutes or 5), 20))
        rid = "ev_" + secrets.token_urlsafe(12)
        with self._lock:
            self.db().execute(
                "INSERT INTO runs (id, created, caller_agent, method, number, persona, goal, criteria, max_minutes, "
                "status, by, case_id, options) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, time.time(), caller_agent, method, number, persona, goal, json.dumps(crit), minutes,
                 "waiting", by, case_id if CASE_ID.match(case_id or "") else None,
                 json.dumps(options) if options else None))
            self.db().commit()
        return self.get(rid)

    def update(self, rid: str, **fields: Any) -> None:
        if not fields:
            return
        for k in ("transcript", "results", "latencies"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k])
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock:
            self.db().execute(f"UPDATE runs SET {cols} WHERE id = ?", (*fields.values(), rid))
            self.db().commit()

    def get(self, rid: str) -> dict | None:
        if not ID.match(rid or ""):
            return None
        with self._lock:
            row = self.db().execute("SELECT * FROM runs WHERE id = ?", (rid,)).fetchone()
        return self._row(row) if row else None

    def list(self, limit: int = 50, before: float | None = None) -> dict:
        limit = max(1, min(int(limit or 50), 200))
        with self._lock:
            rows = self.db().execute(
                "SELECT * FROM runs WHERE created < ? ORDER BY created DESC LIMIT ?",
                (before if before is not None else time.time() + 1, limit + 1)).fetchall()
        runs = [self._row(r, brief=True) for r in rows[:limit]]
        return {"runs": runs, "next_before": runs[-1]["created"] if len(rows) > limit else None}

    def delete(self, rid: str) -> bool:
        if not ID.match(rid or ""):
            return False
        with self._lock:
            n = self.db().execute("DELETE FROM runs WHERE id = ?", (rid,)).rowcount
            self.db().commit()
        return bool(n)

    @staticmethod
    def _row(row: sqlite3.Row, brief: bool = False) -> dict:
        d = dict(row)
        d["criteria"] = json.loads(d["criteria"] or "[]")
        d["results"] = json.loads(d["results"]) if d.get("results") else None
        d["transcript"] = json.loads(d["transcript"]) if d.get("transcript") else []
        d["duration"] = round(d["ended"] - d["started"]) if d.get("started") and d.get("ended") else None
        d["options"] = json.loads(d["options"]) if d.get("options") else None
        lat = json.loads(d.pop("latencies", None) or "[]")
        d["agent_ms"] = ({"turns": len(lat), "p50": percentile(lat, 50), "p95": percentile(lat, 95), "max": max(lat)}
                         if lat else None)
        if brief:
            d.pop("transcript", None)
        return d

    # ------------------------------------------------------------------ test cases
    def save_cases(self, cases: list[dict], source: str = "") -> list[dict]:
        """Keep test cases (from an imported script or typed in). Bad ones are refused as a whole."""
        rows = []
        for c in cases:
            if not isinstance(c, dict):
                raise EvalError("each test case is an object")
            name = str(c.get("name") or "").strip()[:120]
            persona, goal = str(c.get("persona") or "").strip()[:1500], str(c.get("goal") or "").strip()[:500]
            if not name or not persona or not goal:
                raise EvalError("each test case needs a name, who is calling, and what they want")
            crit = [str(x).strip()[:300] for x in (c.get("criteria") or []) if str(x).strip()][:MAX_CRITERIA]
            number = ""
            if c.get("number"):
                try:
                    number = normalize_number(str(c["number"]))
                except EvalError:
                    number = ""
            rows.append(("tc_" + secrets.token_urlsafe(12), time.time(), name, persona, goal, json.dumps(crit), number,
                         str(source or c.get("source") or "")[:120], str(c.get("notes") or "")[:1000]))
        with self._lock:
            have = self.db().execute("SELECT COUNT(*) FROM cases").fetchone()[0]
            if have + len(rows) > MAX_CASES:
                raise EvalError(f"at most {MAX_CASES} test cases; delete some first")
            self.db().executemany("INSERT INTO cases (id, created, name, persona, goal, criteria, number, source, notes) "
                                  "VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self.db().commit()
        return [self.case(r[0]) for r in rows]

    def case(self, cid: str) -> dict | None:
        if not CASE_ID.match(cid or ""):
            return None
        with self._lock:
            row = self.db().execute("SELECT * FROM cases WHERE id = ?", (cid,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["criteria"] = json.loads(d["criteria"] or "[]")
        return d

    def cases(self) -> list[dict]:
        with self._lock:
            ids = [r[0] for r in self.db().execute("SELECT id FROM cases ORDER BY created, name").fetchall()]
        return [c for c in (self.case(i) for i in ids) if c]

    def delete_case(self, cid: str) -> bool:
        if not CASE_ID.match(cid or ""):
            return False
        with self._lock:
            n = self.db().execute("DELETE FROM cases WHERE id = ?", (cid,)).rowcount
            self.db().commit()
        return bool(n)

    # ------------------------------------------------------------------ bridge tokens
    def bridge_token(self, rid: str) -> str:
        token = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._bridges = {k: v for k, v in self._bridges.items() if v[1] > now}
            self._bridges[token] = (rid, now + BRIDGE_TTL)
        return token

    def redeem(self, token: str) -> str | None:
        """The run a bridge token belongs to; a token starts one call only."""
        with self._lock:
            rid, expiry = self._bridges.pop(token or "", ("", 0.0))
        return rid if rid and expiry > time.time() else None


# ---------------------------------------------------------------------- grading

async def grade(llm, run: dict, said: list[dict]) -> dict:
    """The model's verdict on the goal and each criterion."""
    from pipecat.processors.aggregators.llm_context import LLMContext

    from .flow import parse_json_object

    lines = "\n".join(f"{'Agent' if x['who'] == 'agent' else 'Caller'}: {x['text']}" for x in said)[-16000:]
    crit = run["criteria"]
    ask = "\n".join([
        f"The caller's goal: {run['goal']}",
        "Criteria (grade each one, in this order):" if crit else "There are no extra criteria; return an empty list.",
        *[f"{i + 1}. {c}" for i, c in enumerate(crit)],
        "",
        "Transcript:",
        lines or "(nothing was said)",
    ])
    ctx = LLMContext(messages=[{"role": "user", "content": ask}])
    text = await asyncio.wait_for(llm.run_inference(ctx, max_tokens=900, system_instruction=GRADER,
                                                    response_schema=SCHEMA), 60)
    out = parse_json_object(text) or {}
    got = [x for x in out.get("criteria") or [] if isinstance(x, dict)]
    results = []
    for i, c in enumerate(crit):
        g = got[i] if i < len(got) else {}
        results.append({"criterion": c, "passed": g.get("passed") is True,
                        "reason": str(g.get("reason") or "not graded")[:300]})
    return {"goal_met": out.get("goal_met") is True, "criteria": results,
            "summary": str(out.get("summary") or "")[:1000]}


def verdict(results: dict) -> str:
    return "passed" if results.get("goal_met") and all(c["passed"] for c in results.get("criteria", [])) else "failed"


# ---------------------------------------------------------------------- replaying a call

def replay_persona(caller_lines: list[str], persona: str = "") -> str:
    """A persona that says what a past caller said, in order: to play a failed call again."""
    lines = [str(x).strip() for x in caller_lines if str(x).strip()][:40]
    if not lines:
        raise EvalError("that call has nothing the caller said to replay")
    steps = "\n".join(f"{i + 1}. {x[:300]}" for i, x in enumerate(lines))
    who = (persona.strip() + "\n\n") if persona.strip() else ""
    return (f"{who}Replay a past call: say these lines in this order, one per turn, waiting for the agent each time. "
            "Keep the wording. If the agent asks something none of them answers, answer briefly and go on with the "
            f"next line. After the last line, say goodbye.\n{steps}")[:3000]


# ---------------------------------------------------------------------- importing test scripts

CONVERTER = """You turn test scripts for phone systems (IVR and voice-agent tests from tools like Hammer, Cyara,
Botium, spreadsheets, or plain notes) into test cases for an AI test caller that phones the system and talks to it.

For each test in the script, write one test case:
- name: the test's own name or id, short.
- persona: who the caller is and how they behave, plus exactly what to say or give at each point, in order
  ("When asked for the account number, say 4 4 7 2 1. When asked to confirm, say yes."). Keep any data the script
  uses as-is. Where the script presses keys (DTMF), tell the caller to say those digits instead.
- goal: what the call is trying to get done, in a few words.
- criteria: what must be true afterwards, one short check each, from the script's expected prompts and results
  ("The system asks for the account number", "The system reads back a balance", "The call is transferred to an
  agent"). Quote expected prompts loosely: say what the system should ask or say, not the exact recording.
- number: the number dialed, if the script has one (with country code), else empty.
- notes: anything the test caller can't do (keypad tones, waiting for a callback, checking a screen pop or a
  database), in one sentence; else empty.

Use only what the script says; don't invent tests. Reply with one JSON object and nothing else."""

CONVERT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["cases"], "properties": {"cases": {
    "type": "array", "items": {"type": "object", "additionalProperties": False,
                               "required": ["name", "persona", "goal", "criteria", "number", "notes"],
                               "properties": {"name": {"type": "string"}, "persona": {"type": "string"},
                                              "goal": {"type": "string"},
                                              "criteria": {"type": "array", "items": {"type": "string"}},
                                              "number": {"type": "string"}, "notes": {"type": "string"}}}}}}


def script_text(raw: bytes | str, filename: str = "") -> str:
    """The text of an uploaded test script (text, CSV, XML, JSON); refuses binaries."""
    if isinstance(raw, bytes):
        if b"\x00" in raw[:4096]:
            raise EvalError("that file isn't text: export the script as text, CSV, XML or JSON")
        for enc in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                raw = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
    text = str(raw).strip()
    if not text:
        raise EvalError("the script is empty")
    if len(text) > MAX_SCRIPT:
        raise EvalError(f"the script is too long ({len(text):,} characters; at most {MAX_SCRIPT:,}): import it in parts")
    return text


async def convert_script(llm, text: str, filename: str = "") -> list[dict]:
    """Test cases the model reads out of a test script. Nothing is saved; the person picks which to keep."""
    from pipecat.processors.aggregators.llm_context import LLMContext

    from .flow import parse_json_object

    ask = (f"File: {filename}\n\n" if filename else "") + "Test script:\n" + text
    ctx = LLMContext(messages=[{"role": "user", "content": ask}])
    out = parse_json_object(await asyncio.wait_for(
        llm.run_inference(ctx, max_tokens=8000, system_instruction=CONVERTER, response_schema=CONVERT_SCHEMA), 180)) or {}
    cases = []
    for c in out.get("cases") or []:
        if not isinstance(c, dict):
            continue
        crit = [str(x).strip()[:300] for x in (c.get("criteria") or []) if isinstance(x, str) and x.strip()][:MAX_CRITERIA]
        case = {"name": str(c.get("name") or "").strip()[:120], "persona": str(c.get("persona") or "").strip()[:1500],
                "goal": str(c.get("goal") or "").strip()[:500], "criteria": crit,
                "number": str(c.get("number") or "").strip()[:40], "notes": str(c.get("notes") or "").strip()[:1000]}
        if case["name"] and case["persona"] and case["goal"]:
            cases.append(case)
    if not cases:
        raise EvalError("no test cases could be read from that script")
    return cases[:MAX_CASES]
