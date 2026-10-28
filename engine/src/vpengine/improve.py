"""Improve an agent from its failing test cases: run, propose, re-run, keep only what helps.

    run        each chosen test case is played as a text chat against an in-memory copy of the agent:
               the engine's model plays the caller (persona and goal), tools are mocked so nothing
               is booked or sent, and the transcript is graded against the case's goal and checks
    propose    the model reads the failed checks and their reasons and suggests new prompts for the
               steps it thinks caused them (prompts only: no steps, routes, tools or values change)
    re-run     every case runs again on the changed copy; the change is kept only if more checks pass
               and no case that passed before fails now
    you        nothing is saved until you apply it; applying saves through the builder, which keeps
               the previous version in the agent's history

The idea of improving the system around a model (its prompts) rather than the model itself, by
proposing changes and probing them, follows Google Research's Harness² (github.com/google-research/harness2).
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from . import evals as _evals

log = logging.getLogger("vpengine.improve")

MAX_CASES = 10
MAX_ROUNDS = 3
MAX_TURNS = 12              # caller turns per test chat
MAX_EDITS = 4               # steps changed per round
MAX_PROMPT = 8000
EDITABLE = ("startCall", "agentNode", "endCall", "globalNode")
VARIABLE = re.compile(r"\{\{\s*([A-Za-z_]\w*)")
ID = re.compile(r"^im_[A-Za-z0-9_-]{8,40}$")

CALLER = "\n\n".join([
    "You are role-playing a CALLER phoning a business, to test its voice agent. You are not the agent.",
    "Who you are: {persona}",
    "What you want from this call: {goal}",
    "Speak like a real caller on the phone: one or two short sentences, plain words, no stage directions, "
    "no quotation marks. Answer the agent's questions. When it asks for details, make up plausible ones "
    "(a name, a day, a 555-01xx phone number); never use real people's details.",
    "When what you wanted is done, or the agent has said goodbye, or you would hang up, set done to true "
    "and line to null. Reply with one JSON object and nothing else.",
])
CALLER_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["line", "done"],
                 "properties": {"line": {"type": ["string", "null"]}, "done": {"type": "boolean"}}}

PROPOSER = """You improve a phone agent's prompts. The agent is a set of steps; each step has a prompt that tells the
voice agent what to do while the call is in that step. Global steps add their prompt to every step.

You get the steps, then test calls that failed: what the caller wanted, which checks failed and why, and what was said.
Change as little as you can to fix the failures:
- edit only the prompts of the steps that caused a failure; keep everything that already works
- keep every {{variable}} that a prompt uses
- don't invent facts the agent doesn't already have (prices, hours, policies, names); if a fix needs a fact,
  tell the agent to ask, check or transfer instead
- write prompts the way they're already written: plain instructions, short sentences
Reply with one JSON object and nothing else."""

PROPOSER_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["edits", "notes"],
                   "properties": {
                       "notes": {"type": "string"},
                       "edits": {"type": "array", "items": {
                           "type": "object", "additionalProperties": False,
                           "required": ["step_id", "prompt", "why"],
                           "properties": {"step_id": {"type": "string"}, "prompt": {"type": "string"},
                                          "why": {"type": "string"}}}}}}


class ImproveError(ValueError):
    pass


def fingerprint(definition: dict) -> str:
    return hashlib.sha256(json.dumps(definition, sort_keys=True, default=str).encode()).hexdigest()[:16]


def steps(definition: dict) -> list[dict]:
    """The steps whose prompts may change: id, name, kind and prompt."""
    out = []
    for n in definition.get("nodes") or []:
        if isinstance(n, dict) and n.get("type") in EDITABLE and isinstance(n.get("data"), dict):
            out.append({"id": str(n.get("id")), "name": str(n["data"].get("name") or n.get("id")),
                        "type": n["type"], "prompt": str(n["data"].get("prompt") or "")})
    return out


def apply_edits(definition: dict, edits: list[dict]) -> dict:
    """A copy of the definition with these steps' prompts replaced."""
    out = copy.deepcopy(definition)
    by_id = {str(n.get("id")): n for n in out.get("nodes") or [] if isinstance(n, dict)}
    for e in edits:
        by_id[e["step_id"]]["data"]["prompt"] = e["prompt"]
    return out


def clean_edits(definition: dict, raw: Any) -> list[dict]:
    """The model's edits that are safe to try: a known step, a real change, every variable kept."""
    known = {s["id"]: s for s in steps(definition)}
    out, seen = [], set()
    for e in (raw or [])[:MAX_EDITS * 2]:
        if not isinstance(e, dict):
            continue
        sid, prompt = str(e.get("step_id") or ""), str(e.get("prompt") or "").strip()
        step = known.get(sid)
        if not step or sid in seen or not prompt or len(prompt) > MAX_PROMPT or prompt == step["prompt"].strip():
            continue
        if set(VARIABLE.findall(step["prompt"])) - set(VARIABLE.findall(prompt)):
            continue                                   # it dropped a value the step uses
        seen.add(sid)
        out.append({"step_id": sid, "name": step["name"], "before": step["prompt"], "prompt": prompt,
                    "why": str(e.get("why") or "")[:400]})
        if len(out) == MAX_EDITS:
            break
    return out


async def caller_line(llm, persona: str, goal: str, said: list[dict]) -> tuple[str | None, bool]:
    """What a role-played caller says next (None when they're done)."""
    from pipecat.processors.aggregators.llm_context import LLMContext

    from .flow import parse_json_object
    lines = "\n".join(f"{'Caller' if x['who'] == 'caller' else 'Agent'}: {x['text'][:1000]}" for x in said[-60:])
    ctx = LLMContext(messages=[{"role": "user", "content": (lines or "(the call has just connected; the agent "
                                                                     "hasn't spoken yet)") + "\n\nWhat does the caller say next?"}])
    text = await asyncio.wait_for(llm.run_inference(
        ctx, max_tokens=200, system_instruction=CALLER.format(persona=persona[:2000], goal=goal[:500] or "(not given)"),
        response_schema=CALLER_SCHEMA), 45)
    out = parse_json_object(text) or {}
    line = str(out.get("line") or "").strip()[:500] or None
    return line, bool(out.get("done")) or line is None


async def propose(llm, definition: dict, failures: list[dict]) -> tuple[list[dict], str]:
    from pipecat.processors.aggregators.llm_context import LLMContext

    from .flow import parse_json_object
    parts = ["Steps:"]
    for s in steps(definition):
        parts.append(f"[{s['id']}] {s['name']} ({s['type']}):\n{s['prompt'] or '(no prompt)'}\n")
    parts.append("Failed test calls:")
    for f in failures[:MAX_CASES]:
        failed = [c for c in f["results"]["criteria"] if not c["passed"]]
        parts.append(f"\n## {f['name']}\nThe caller wanted: {f['goal']}")
        if not f["results"].get("goal_met"):
            parts.append("Goal NOT met.")
        parts += [f"Failed check: {c['criterion']} (why: {c['reason']})" for c in failed]
        talk = "\n".join(f"{'Caller' if x['who'] == 'caller' else 'Agent'}: {x['text'][:400]}" for x in f["transcript"])
        parts.append("What was said:\n" + talk[-4000:])
    ctx = LLMContext(messages=[{"role": "user", "content": "\n".join(parts)[-30000:]}])
    text = await asyncio.wait_for(llm.run_inference(ctx, max_tokens=3000, system_instruction=PROPOSER,
                                                    response_schema=PROPOSER_SCHEMA), 120)
    out = parse_json_object(text) or {}
    return clean_edits(definition, out.get("edits")), str(out.get("notes") or "")[:1000]


def said_in(events: list[dict]) -> list[dict]:
    """What was said in a test chat: the agent's lines and the role-played caller's."""
    out = []
    for e in events:
        if e.get("type") == "bot_speech" and e.get("text") and not e.get("filler"):
            out.append({"who": "agent", "text": str(e["text"])})
        elif e.get("type") == "user_speech" and e.get("text"):
            out.append({"who": "caller", "text": str(e["text"])})
    return out


async def play_case(llm, new_session: Callable[[Any], Any], doc: dict, definition: dict, case: dict) -> dict:
    """One test case as a text chat against this version of the agent, graded."""
    from .graph import Graph
    graph = Graph.from_definition(doc.get("name") or "agent", definition, doc.get("context_variables"), doc.get("tools"))
    session = new_session(graph)
    events: list[dict] = []
    try:
        events += await session.open()
        for _ in range(MAX_TURNS):
            if session.flow.ended:
                break
            line, done = await caller_line(llm, case["persona"], case["goal"], said_in(events))
            if done or not line:
                break
            events += await session.send(line)
    finally:
        await session.close()
    said = said_in(events)
    results = await _evals.grade(llm, {"goal": case["goal"], "criteria": case.get("criteria") or []}, said)
    return {"case_id": case["id"], "name": case["name"], "goal": case["goal"], "verdict": _evals.verdict(results),
            "results": results, "transcript": said}


def score(results: list[dict]) -> dict:
    checks = sum(int(r["results"].get("goal_met") is True) + sum(c["passed"] for c in r["results"]["criteria"])
                 for r in results if r.get("results"))
    total = sum(1 + len(r["results"]["criteria"]) for r in results if r.get("results"))
    return {"cases": len(results), "passed": sum(r["verdict"] == "passed" for r in results),
            "checks": checks, "checks_total": total}


def better(old: list[dict], new: list[dict]) -> bool:
    """More checks pass, and no case that passed before fails now."""
    was = {r["case_id"]: r["verdict"] for r in old}
    if any(was.get(r["case_id"]) == "passed" and r["verdict"] != "passed" for r in new):
        return False
    return score(new)["checks"] > score(old)["checks"]


@dataclass
class Job:
    agent: str
    base: dict                          # the agent as read when the job started (builder.read)
    cases: list[dict]
    rounds: int
    id: str = field(default_factory=lambda: "im_" + secrets.token_urlsafe(12))
    status: str = "running"             # running | done | error | cancelled
    stage: str = "Starting"
    log: list[str] = field(default_factory=list)
    baseline: list[dict] = field(default_factory=list)
    best: list[dict] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    edits: list[dict] = field(default_factory=list)     # the kept changes, step by step (latest prompt wins)
    definition: dict | None = None                      # the improved definition, when there is one
    error: str = ""
    started: float = field(default_factory=time.time)
    finished: float | None = None
    applied: bool = False
    task: asyncio.Task | None = None

    def note(self, text: str) -> None:
        self.log.append(text)
        self.stage = text
        log.info("improve %s: %s", self.agent, text)

    def view(self) -> dict:
        return {"id": self.id, "agent": self.agent, "status": self.status, "stage": self.stage, "log": self.log[-60:],
                "rounds": self.rounds, "cases": [{"id": c["id"], "name": c["name"]} for c in self.cases],
                "baseline": {"score": score(self.baseline), "results": self.baseline} if self.baseline else None,
                "best": {"score": score(self.best), "results": self.best} if self.best else None,
                "attempts": self.attempts, "edits": self.edits, "improved": self.definition is not None,
                "error": self.error, "started": self.started, "finished": self.finished, "applied": self.applied}


Play = Callable[[dict, dict], Awaitable[dict]]


async def run(job: Job, llm, play: Play) -> None:
    """The loop. `play(definition, case)` runs one test chat and returns its graded result."""
    async def all_cases(definition: dict, label: str) -> list[dict]:
        out = []
        for i, case in enumerate(job.cases):
            job.note(f"{label}: {case['name']} ({i + 1} of {len(job.cases)})")
            out.append(await play(definition, case))
        return out

    try:
        current = job.base["definition"]
        job.baseline = job.best = await all_cases(current, "Testing the agent as it is")
        s = score(job.best)
        job.note(f"As it is: {s['passed']} of {s['cases']} cases pass, {s['checks']} of {s['checks_total']} checks")
        for rnd in range(1, job.rounds + 1):
            failures = [r for r in job.best if r["verdict"] != "passed" and r.get("results")]
            if not failures:
                job.note("Every case passes; nothing to improve")
                break
            job.note(f"Round {rnd}: asking the model for prompt changes")
            edits, notes = await propose(llm, current, failures)
            if not edits:
                job.attempts.append({"round": rnd, "edits": [], "notes": notes, "kept": False, "score": None})
                job.note(f"Round {rnd}: no usable changes suggested")
                break
            trial = apply_edits(current, edits)
            results = await all_cases(trial, f"Round {rnd}: testing the changes")
            kept = better(job.best, results)
            job.attempts.append({"round": rnd, "edits": edits, "notes": notes, "kept": kept, "score": score(results)})
            s = score(results)
            if kept:
                current, job.best, job.definition = trial, results, trial
                kept_ids = {e["step_id"] for e in edits}
                job.edits = [e for e in job.edits if e["step_id"] not in kept_ids] + edits
                job.note(f"Round {rnd}: kept ({s['passed']} of {s['cases']} cases, {s['checks']} of "
                         f"{s['checks_total']} checks)")
            else:
                job.note(f"Round {rnd}: not kept ({s['checks']} of {s['checks_total']} checks, or a passing case "
                         f"failed)")
        # each kept edit's "before" is the agent as it was, not an earlier round's version
        original = {s["id"]: s["prompt"] for s in steps(job.base["definition"])}
        for e in job.edits:
            e["before"] = original.get(e["step_id"], e["before"])
        job.status = "done"
        job.note("Done: review the changes and apply them" if job.definition else "Done: no change was kept")
    except asyncio.CancelledError:
        job.status = "cancelled"
        job.note("Stopped")
    except Exception as exc:  # noqa: BLE001 - shown on the page
        log.exception("improve %s failed", job.agent)
        job.status, job.error = "error", str(exc)[:500]
        job.note(f"Stopped by an error: {job.error}")
    finally:
        job.finished = time.time()


def check_request(cases: list[dict], rounds: Any) -> int:
    if not cases:
        raise ImproveError("choose at least one test case")
    if len(cases) > MAX_CASES:
        raise ImproveError(f"at most {MAX_CASES} test cases per run")
    try:
        n = int(rounds if rounds is not None else 2)
    except (TypeError, ValueError):
        raise ImproveError("rounds is a number") from None
    if not 1 <= n <= MAX_ROUNDS:
        raise ImproveError(f"rounds is 1 to {MAX_ROUNDS}")
    return n
