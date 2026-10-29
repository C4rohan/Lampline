"""Scenario tests: scripted conversations run through the engine's text chat.

A test file lives in an agent folder under tests/*.yaml:

    name: caller reschedules to Friday
    context: {first_name: Sam}          # optional initial context variables
    turns:
      - user: I need to move my appointment
        expect: {reaches: Reschedule}   # optional per-turn checks
      - user: Friday morning works
    expect:                             # checks over the whole conversation
      reaches: [Reschedule, Goodbye]    # step names or ids, in this order
      not_reaches: [Billing]
      calls_tool: [find_slots]
      says: [Friday]                    # case-insensitive text the agent must say
      not_says: [account number]
      ends: true                        # the conversation reached an end step

Or let a model play the caller, for conversations you don't want to script line
by line (the engine's own model plays it, so vk needs no key):

    name: impatient caller with a billing question
    caller:
      persona: Sam, in a hurry, confused by a bill with a late fee on it
      goal: get the late fee explained or talk to someone in billing
      opening: Hi, I've got a question about my bill.   # optional first line
      max_turns: 8                                      # optional, 1 to 30 (default 8)
    expect:
      reaches: Billing
      not_says: [card number]

The caller is different every run, so keep expectations to what any good
conversation must do, and run it a few times when you change the agent.

Text chat runs the folder as it is on disk, with the model configured in the
engine, so a test exercises exactly what a call would. Results depend on the
model: keep expectations about behaviour (where it goes, what it must or must not
say), not exact wording.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CHECK_KEYS = ("reaches", "not_reaches", "calls_tool", "says", "not_says", "ends")


class TestFileError(Exception):
    pass


@dataclass
class Scenario:
    name: str
    path: str
    turns: list[dict]
    expect: dict
    context: dict
    caller: dict | None = None          # a model plays the caller: {persona, goal, opening, max_turns}


@dataclass
class Transcript:
    """What happened in one conversation, accumulated turn by turn."""
    nodes: list[dict] = field(default_factory=list)   # {"id", "name"} in visit order
    tools: list[str] = field(default_factory=list)
    lines: list[tuple[str, str]] = field(default_factory=list)  # (speaker, text)
    errors: list[str] = field(default_factory=list)
    ended: bool = False

    def add_turn(self, turn: dict) -> None:
        user = (turn.get("user_message") or {}).get("text")
        if user:
            self.lines.append(("caller", user))
        spoke = False
        for event in turn.get("events") or []:
            kind, payload = event.get("type"), event.get("payload") or {}
            if kind == "node_transition":
                node = {"id": str(payload.get("node_id")), "name": payload.get("node_name") or ""}
                if not self.nodes or self.nodes[-1]["id"] != node["id"]:
                    self.nodes.append(node)
                    self.lines.append(("step", node["name"] or node["id"]))
            elif kind == "bot_speech" and payload.get("text"):
                self.lines.append(("agent", payload["text"]))
                spoke = True
            elif kind == "tool_call_started" and payload.get("function_name"):
                self.tools.append(payload["function_name"])
                self.lines.append(("tool", payload["function_name"]))
            elif kind == "execution_error":
                self.errors.append(str(payload.get("message", "execution error")))
        if not spoke and (turn.get("assistant_message") or {}).get("text"):
            self.lines.append(("agent", turn["assistant_message"]["text"]))

    def agent_text(self) -> str:
        return "\n".join(text for who, text in self.lines if who == "agent")


@dataclass
class Result:
    scenario: Scenario
    transcript: Transcript
    failures: list[str]
    seconds: float
    runs: int = 1                 # with `vk test --repeat N`: how many times it ran
    passes: int | None = None     # and how many of those passed (None: one run)

    @property
    def passed(self) -> bool:
        return not self.failures

    @property
    def flaky(self) -> bool:
        """Passed on some runs and failed on others: the agent's behaviour isn't settled."""
        return self.passes is not None and 0 < self.passes < self.runs

    def as_dict(self) -> dict:
        return {
            "name": self.scenario.name,
            "file": self.scenario.path,
            "passed": self.passed,
            "runs": self.runs,
            "passes": self.runs if self.passes is None and self.passed else (self.passes or 0),
            "flaky": self.flaky,
            "failures": self.failures,
            "seconds": round(self.seconds, 2),
            "steps": [n["name"] or n["id"] for n in self.transcript.nodes],
            "tools": self.transcript.tools,
            "transcript": [{"who": w, "text": t} for w, t in self.transcript.lines],
        }


# --- loading ---------------------------------------------------------------------

def _as_list(value: Any) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _check_expect(expect: Any, where: str) -> dict:
    if expect is None:
        return {}
    if not isinstance(expect, dict):
        raise TestFileError(f"{where}: expect must be a mapping")
    unknown = sorted(set(expect) - set(CHECK_KEYS))
    if unknown:
        raise TestFileError(f"{where}: unknown expect key(s) {', '.join(unknown)} "
                            f"(allowed: {', '.join(CHECK_KEYS)})")
    if "ends" in expect and not isinstance(expect["ends"], bool):
        raise TestFileError(f"{where}: expect.ends must be true or false")
    return expect


def load_file(path: Path, rel: str) -> Scenario:
    try:
        doc = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise TestFileError(f"{rel}: not valid YAML ({exc})") from exc
    if not isinstance(doc, dict):
        raise TestFileError(f"{rel}: expected a mapping with name/turns/expect")
    turns = doc.get("turns") or []
    if not isinstance(turns, list) or not all(isinstance(t, dict) and isinstance(t.get("user"), str) for t in turns):
        raise TestFileError(f"{rel}: turns must be a list of {{user: <text>}} entries")
    caller = _check_caller(doc.get("caller"), rel)
    if caller and turns:
        raise TestFileError(f"{rel}: use either turns (scripted) or caller (a model plays the caller), not both")
    for i, turn in enumerate(turns, 1):
        _check_expect(turn.get("expect"), f"{rel} turn {i}")
    context = doc.get("context") or {}
    if not isinstance(context, dict):
        raise TestFileError(f"{rel}: context must be a mapping")
    return Scenario(name=str(doc.get("name") or path.stem), path=rel, turns=turns,
                    expect=_check_expect(doc.get("expect"), rel), context=context, caller=caller)


def _check_caller(caller: Any, rel: str) -> dict | None:
    if caller is None:
        return None
    if not isinstance(caller, dict):
        raise TestFileError(f"{rel}: caller must be a mapping with persona, and optionally goal, opening, max_turns")
    unknown = sorted(set(caller) - {"persona", "goal", "opening", "max_turns"})
    if unknown:
        raise TestFileError(f"{rel}: unknown caller key(s) {', '.join(unknown)} (allowed: persona, goal, opening, max_turns)")
    if not isinstance(caller.get("persona"), str) or len(caller["persona"].strip()) < 5:
        raise TestFileError(f"{rel}: caller.persona describes who is calling, like \"Sam, in a hurry, asking about a bill\"")
    for key in ("goal", "opening"):
        if key in caller and not isinstance(caller[key], str):
            raise TestFileError(f"{rel}: caller.{key} must be text")
    turns = caller.get("max_turns", 8)
    if not isinstance(turns, int) or isinstance(turns, bool) or not 1 <= turns <= 30:
        raise TestFileError(f"{rel}: caller.max_turns must be a whole number from 1 to 30")
    return {"persona": caller["persona"].strip(), "goal": str(caller.get("goal") or "").strip(),
            "opening": str(caller.get("opening") or "").strip(), "max_turns": turns}


def load(folder: str | Path) -> list[Scenario]:
    folder = Path(folder)
    files = sorted((folder / "tests").glob("*.yaml")) + sorted((folder / "tests").glob("*.yml"))
    return [load_file(p, str(p.relative_to(folder))) for p in files]


def node_refs(scenario: Scenario) -> list[tuple[str, str]]:
    """(where, ref) for every step name/id a scenario mentions, for linting."""
    refs = []
    blocks = [("expect", scenario.expect)] + [(f"turn {i}", t.get("expect") or {})
                                              for i, t in enumerate(scenario.turns, 1)]
    for where, block in blocks:
        for key in ("reaches", "not_reaches"):
            refs += [(f"{where}.{key}", str(r)) for r in _as_list(block.get(key))]
    return refs


# --- checking --------------------------------------------------------------------

def _matches(node: dict, ref: str) -> bool:
    return node["id"] == ref or node["name"].strip().lower() == ref.strip().lower()


def evaluate(expect: dict, t: Transcript, prefix: str = "") -> list[str]:
    failures = []
    path = " → ".join(n["name"] or n["id"] for n in t.nodes) or "(none)"

    position = 0  # `reaches` must appear in order
    for ref in map(str, _as_list(expect.get("reaches"))):
        hit = next((i for i in range(position, len(t.nodes)) if _matches(t.nodes[i], ref)), None)
        if hit is None:
            failures.append(f"{prefix}expected to reach {ref!r}; steps were {path}")
        else:
            position = hit + 1
    for ref in map(str, _as_list(expect.get("not_reaches"))):
        if any(_matches(n, ref) for n in t.nodes):
            failures.append(f"{prefix}should not reach {ref!r}; steps were {path}")
    for tool in map(str, _as_list(expect.get("calls_tool"))):
        if tool not in t.tools:
            called = ", ".join(t.tools) or "none"
            failures.append(f"{prefix}expected a call to tool {tool!r}; tools called: {called}")
    said = t.agent_text().lower()
    for text in map(str, _as_list(expect.get("says"))):
        if text.lower() not in said:
            failures.append(f"{prefix}expected the agent to say {text!r}")
    for text in map(str, _as_list(expect.get("not_says"))):
        if text.lower() in said:
            failures.append(f"{prefix}the agent said {text!r}, which it must not")
    if "ends" in expect and expect["ends"] != t.ended:
        failures.append(f"{prefix}expected the conversation {'to end' if expect['ends'] else 'not to end'}"
                        f"; it {'ended' if t.ended else 'did not end'}")
    return failures


def _absorb(session: dict, transcript: Transcript, seen: set[str]) -> None:
    for turn in (session.get("session_data") or {}).get("turns") or []:
        tid = turn.get("id")
        if turn.get("status") == "completed" and tid not in seen:
            seen.add(tid)
            transcript.add_turn(turn)
    transcript.ended = bool(session.get("is_completed")) or \
        (session.get("session_data") or {}).get("status") == "completed"


def _play_caller(client, caller: dict, run_id: str, transcript: Transcript, seen: set[str], failures: list[str]) -> str:
    """Let the engine's model play the caller until it's done, the call ends, or max_turns."""
    for turn in range(1, caller["max_turns"] + 1):
        if transcript.ended:
            break
        line = caller["opening"] if turn == 1 and caller["opening"] else None
        if line is None:
            said = [{"who": w, "text": t} for w, t in transcript.lines if w in ("caller", "agent")]
            nxt = client.caller_line(caller["persona"], caller["goal"], said)
            if nxt.get("done") or not nxt.get("line"):
                break
            line = nxt["line"]
        session = client.send_chat(run_id, line)
        _absorb(session, transcript, seen)
    else:
        if not transcript.ended:
            transcript.lines.append(("note", f"stopped after {caller['max_turns']} caller turns"))
    return run_id


def run(client, scenario: Scenario, context_defaults: dict | None = None) -> Result:
    started = time.monotonic()
    transcript, seen, failures = Transcript(), set(), []
    context = {**(context_defaults or {}), **scenario.context}
    session = client.start_chat(initial_context=context or None,
                                name=f"vk test: {scenario.name}"[:80])
    run_id = session["run_id"]
    try:
        _absorb(session, transcript, seen)
        if scenario.caller:
            run_id = _play_caller(client, scenario.caller, run_id, transcript, seen, failures)
        for i, turn in enumerate(scenario.turns, 1):
            if transcript.ended:
                failures.append(f"turn {i}: the conversation had already ended before {turn['user']!r}")
                break
            session = client.send_chat(run_id, turn["user"])
            _absorb(session, transcript, seen)
            failures += evaluate(turn.get("expect") or {}, transcript, prefix=f"turn {i}: ")
        failures += evaluate(scenario.expect, transcript)
        failures += [f"engine error: {e}" for e in transcript.errors]
    finally:
        if not transcript.ended:
            try:
                client.end_chat(run_id)
            except Exception:  # best effort; the engine times stale sessions out
                pass
    return Result(scenario, transcript, failures, time.monotonic() - started)
