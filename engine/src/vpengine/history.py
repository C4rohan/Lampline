"""Call history: every finished conversation, kept on this computer.

When a chat or call ends the engine stores what happened in a small SQLite
file next to records and knowledge (VPE_DATA_DIR, by default
~/.local/share/vpengine/calls.sqlite3):

- when, which agent, the channel (phone, web test, Telegram, ...), who it was
  with, how long it took and how it ended (or who it was transferred to);
- the transcript and the call's events (steps, tool calls, response times);
- the details it collected (not the agent's own constants).

Recordings are off by default. With VPE_CALL_RECORDINGS=on, voice calls are
also saved as stereo WAV files (caller left, agent right) in recordings/.

Each call also keeps its response time per turn (p50/p95), a cost estimate (pricing.py) and, when the
agent has call checks turned on, its grade.

Everything older than VPE_CALL_HISTORY_DAYS (30) is deleted, recordings
included, checked at start and every six hours. VPE_CALL_HISTORY=off keeps no
new calls; calls already kept stay until they age out or you delete them. This is the engine's own record,
on the machine it runs on: use synthetic test data, not real callers'
personal or health information, unless that machine is set up for it.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
import wave
from pathlib import Path
from typing import Any

from .pricing import percentile

MAX_EVENTS = 2000
MAX_TEXT = 4000
SKIP = {"variables"}                       # the final variables are stored once, not every change
ID = re.compile(r"^[0-9a-f]{16}$")
SCHEMA_VERSION = 1
GRADES = ("passed", "failed", "error")
MAX_CHECKS = 12


def _short(value: Any) -> Any:
    if isinstance(value, str):
        return value if len(value) <= MAX_TEXT else value[:MAX_TEXT] + "…"
    if isinstance(value, dict):
        return {k: _short(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_short(v) for v in value[:200]]
    return value


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


def _channel(session, meta: dict) -> str:
    channel = str(meta.get("channel", "web") or "web")
    if channel == "web":                   # the Test call page: a browser call, or a typed chat
        return "browser" if hasattr(session, "transport") else "chat"
    return channel


class CallHistory:
    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self._db: sqlite3.Connection | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ storage
    @property
    def path(self) -> Path:
        return self.folder / "calls.sqlite3"

    @property
    def recordings(self) -> Path:
        return self.folder / "recordings"

    def db(self) -> sqlite3.Connection:
        with self._lock:
            if self._db is None:
                self.folder.mkdir(parents=True, exist_ok=True)
                self._db = sqlite3.connect(self.path, check_same_thread=False)
                self._db.row_factory = sqlite3.Row
                db = self._db
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS calls (
                        id TEXT PRIMARY KEY, agent TEXT, agent_name TEXT, channel TEXT, contact TEXT,
                        started REAL, ended REAL, outcome TEXT, transferred_to TEXT,
                        steps TEXT, collected TEXT, events TEXT, turns INTEGER, response_ms INTEGER);
                    CREATE INDEX IF NOT EXISTS calls_started ON calls (started DESC);
                """)
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version > SCHEMA_VERSION:
                    self._db = None
                    db.close()
                    raise RuntimeError(f"{self.path} was written by a newer Lampline (schema {version}); "
                                       "upgrade the engine to read it")
                if version < 1:                    # v1: response-time spread, cost estimate, automatic grading
                    have = {r[1] for r in db.execute("PRAGMA table_info(calls)")}
                    for col, kind in (("latencies", "TEXT"), ("p50_ms", "INTEGER"), ("p95_ms", "INTEGER"),
                                      ("max_ms", "INTEGER"), ("cost", "REAL"), ("cost_detail", "TEXT"),
                                      ("grade", "TEXT DEFAULT ''"), ("grade_results", "TEXT")):
                        if col not in have:
                            db.execute(f"ALTER TABLE calls ADD COLUMN {col} {kind}")
                    db.executescript("""
                        CREATE TABLE IF NOT EXISTS checks (
                            agent TEXT PRIMARY KEY, goal TEXT, criteria TEXT, enabled INTEGER, updated REAL, by TEXT);
                        PRAGMA user_version = 1;
                    """)
                    db.commit()
            return self._db

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def recording_path(self, call_id: str) -> Path | None:
        if not ID.match(call_id or ""):
            return None
        p = self.recordings / f"{call_id}.wav"
        return p if p.is_file() else None

    # ------------------------------------------------------------------ writing
    def save(self, session, cost: dict | None = None) -> dict:
        """Store a finished session, with its cost estimate (pricing.estimate); returns the row as the API shows it."""
        meta = getattr(session, "meta", {}) or {}
        events = [e for e in session.events if e.get("type") not in SKIP][-MAX_EVENTS:]
        ended = next((e for e in reversed(events) if e.get("type") == "call_ended"), {})
        names = session.flow.collected()            # what this call gathered, as transfers send it
        collected = {k: _jsonable(session.flow.variables[k]) for k in names
                     if k not in ("channel", "contact") and session.flow.variables.get(k) not in (None, "", [], {})}
        metrics = [e["response_ms"] for e in events if e.get("type") == "turn_metrics" and isinstance(e.get("response_ms"), (int, float))]
        steps = [session.graph.steps[i].name for i in session.flow.visited if i in session.graph.steps]
        row = {
            "id": session.id, "agent": meta.get("agent", ""), "agent_name": session.graph.name,
            "channel": _channel(session, meta), "contact": str(meta.get("contact", "") or ""),
            "started": session.created, "ended": time.time(),
            "outcome": ("error" if session.error and ended.get("reason") in (None, "", "disconnected")
                        else str(ended.get("reason", "") or "")),
            "transferred_to": str((ended.get("transfer") or {}).get("to", "")),
            "steps": json.dumps(steps), "collected": json.dumps(_short(collected)),
            "events": json.dumps([_short(_jsonable(e)) for e in events]),
            "turns": sum(1 for e in events if e.get("type") == "user_speech"),
            "response_ms": int(sum(metrics) / len(metrics)) if metrics else None,
            "latencies": json.dumps([int(m) for m in metrics][-500:]),
            "p50_ms": percentile(metrics, 50), "p95_ms": percentile(metrics, 95),
            "max_ms": int(max(metrics)) if metrics else None,
            "cost": (cost or {}).get("total"), "cost_detail": json.dumps(cost) if cost else None,
            "grade": "", "grade_results": None,
        }
        with self._lock:
            self.db().execute(f"INSERT OR REPLACE INTO calls ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                              list(row.values()))
            self.db().commit()
        return self._public(row)

    def save_recording(self, call_id: str, pcm: bytes, sample_rate: int, channels: int) -> Path | None:
        if not ID.match(call_id or "") or not pcm:
            return None
        self.recordings.mkdir(parents=True, exist_ok=True)
        path = self.recordings / f"{call_id}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(pcm)
        return path

    def prune(self, days: int) -> int:
        """Delete calls (and recordings) older than `days`."""
        cutoff = time.time() - max(1, days) * 86400
        with self._lock:
            if not self.path.exists():
                return 0
            old = [r["id"] for r in self.db().execute("SELECT id FROM calls WHERE started < ?", (cutoff,))]
            for cid in old:
                self._delete(cid)
            self.db().commit()
        if self.recordings.is_dir():           # recordings whose call is gone (a crash between the two writes)
            known = {r["id"] for r in self.db().execute("SELECT id FROM calls")}
            for p in self.recordings.glob("*.wav"):
                if p.stem not in known and p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
        return len(old)

    def delete(self, call_id: str) -> bool:
        with self._lock:
            found = self._delete(call_id)
            self.db().commit()
        return found

    def _delete(self, call_id: str) -> bool:
        cur = self.db().execute("DELETE FROM calls WHERE id = ?", (call_id,))
        rec = self.recording_path(call_id)
        if rec:
            rec.unlink(missing_ok=True)
        return cur.rowcount > 0

    # ------------------------------------------------------------------ reading
    def list(self, *, agent: str = "", channel: str = "", query: str = "", before: float | None = None,
             limit: int = 50, grade: str = "") -> list[dict]:
        if not self.path.exists():
            return []
        sql, args = "SELECT * FROM calls WHERE 1=1", []
        if agent:
            sql += " AND agent = ?"
            args.append(agent)
        if channel:
            sql += " AND channel = ?"
            args.append(channel)
        if grade in GRADES:
            sql += " AND grade = ?"
            args.append(grade)
        if query:
            like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            sql += " AND (contact LIKE ? ESCAPE '\\' OR collected LIKE ? ESCAPE '\\' OR events LIKE ? ESCAPE '\\')"
            args += [like, like, like]
        if before:
            sql += " AND started < ?"
            args.append(before)
        sql += " ORDER BY started DESC LIMIT ?"
        args.append(max(1, min(int(limit), 200)))
        with self._lock:
            return [self._public(dict(r)) for r in self.db().execute(sql, args)]

    def get(self, call_id: str) -> dict | None:
        if not self.path.exists():
            return None
        with self._lock:
            r = self.db().execute("SELECT * FROM calls WHERE id = ?", (call_id,)).fetchone()
        if r is None:
            return None
        out = self._public(dict(r))
        events = json.loads(r["events"] or "[]")
        out["events"] = events
        out["transcript"] = [{"who": "caller" if e["type"] == "user_speech" else "agent", "text": e.get("text", ""), "t": e.get("t")}
                             for e in events if e.get("type") in ("user_speech", "bot_speech") and not e.get("filler")]
        return out

    def _public(self, r: dict) -> dict:
        return {"id": r["id"], "agent": r["agent"], "agent_name": r["agent_name"], "channel": r["channel"],
                "contact": r["contact"], "started": r["started"], "ended": r["ended"],
                "duration_seconds": round((r["ended"] or r["started"]) - r["started"], 1),
                "outcome": r["outcome"], "transferred_to": r["transferred_to"],
                "steps": json.loads(r["steps"] or "[]"), "collected": json.loads(r["collected"] or "{}"),
                "turns": r["turns"], "response_ms": r["response_ms"],
                "p50_ms": r.get("p50_ms"), "p95_ms": r.get("p95_ms"), "max_ms": r.get("max_ms"),
                "cost": r.get("cost"), "cost_detail": json.loads(r.get("cost_detail") or "null"),
                "grade": r.get("grade") or "", "grade_results": json.loads(r.get("grade_results") or "null"),
                "recording": self.recording_path(r["id"]) is not None}

    # ------------------------------------------------------------------ automatic grading
    def set_grade(self, call_id: str, grade: str, results: dict | None) -> bool:
        with self._lock:
            cur = self.db().execute("UPDATE calls SET grade = ?, grade_results = ? WHERE id = ?",
                                    (grade if grade in GRADES else "", json.dumps(results) if results else None, call_id))
            self.db().commit()
        return cur.rowcount > 0

    def checks(self, agent: str | None = None) -> list[dict]:
        """The checks each agent's real calls are graded against."""
        with self._lock:
            rows = self.db().execute("SELECT * FROM checks" + (" WHERE agent = ?" if agent is not None else "") + " ORDER BY agent",
                                     (agent,) if agent is not None else ()).fetchall()
        return [{"agent": r["agent"], "goal": r["goal"] or "", "criteria": json.loads(r["criteria"] or "[]"),
                 "enabled": bool(r["enabled"]), "updated": r["updated"], "by": r["by"] or ""} for r in rows]

    def save_checks(self, agent: str, *, goal: str, criteria: list[str], enabled: bool, by: str = "") -> dict:
        crit = [str(c).strip()[:300] for c in criteria if str(c).strip()][:MAX_CHECKS]
        if enabled and not (goal.strip() or crit):
            raise ValueError("add what a good call achieves, or at least one check")
        with self._lock:
            self.db().execute("INSERT OR REPLACE INTO checks (agent, goal, criteria, enabled, updated, by) VALUES (?, ?, ?, ?, ?, ?)",
                              (agent, goal.strip()[:500], json.dumps(crit), int(enabled), time.time(), by[:80]))
            self.db().commit()
        return self.checks(agent)[0]

    def delete_checks(self, agent: str) -> bool:
        with self._lock:
            cur = self.db().execute("DELETE FROM checks WHERE agent = ?", (agent,))
            self.db().commit()
        return cur.rowcount > 0

    # ------------------------------------------------------------------ summary
    def summary(self, *, agent: str = "", days: int = 7) -> dict:
        """Calls in the last `days`: how many, response times across every turn, cost, and grades."""
        out = {"days": days, "calls": 0, "turns": 0, "p50_ms": None, "p95_ms": None, "cost": 0.0, "cost_per_call": None,
               "costed": 0, "graded": 0, "passed": 0, "failed": 0}
        if not self.path.exists():
            return out
        sql, args = "SELECT latencies, cost, grade, turns FROM calls WHERE started >= ?", [time.time() - max(1, days) * 86400]
        if agent:
            sql += " AND agent = ?"
            args.append(agent)
        with self._lock:
            rows = self.db().execute(sql, args).fetchall()
        lat: list[float] = []
        for r in rows:
            lat += json.loads(r["latencies"] or "[]")
            if r["cost"] is not None:
                out["cost"] += r["cost"]
                out["costed"] += 1
            if r["grade"] in ("passed", "failed"):
                out["graded"] += 1
                out[r["grade"]] += 1
            out["turns"] += r["turns"] or 0
        out["calls"] = len(rows)
        out["p50_ms"], out["p95_ms"] = percentile(lat, 50), percentile(lat, 95)
        out["cost"] = round(out["cost"], 4)
        out["cost_per_call"] = round(out["cost"] / out["costed"], 4) if out["costed"] else None
        return out
