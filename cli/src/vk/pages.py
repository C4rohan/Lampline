"""Scripted pages and condition routes: the deterministic half of an agent.

A step is either an LLM step (the model follows its instructions, as before) or
a scripted page (`mode: scripted`): it says exactly what you wrote, asks its
form fields word for word, checks each answer, and moves on by rules you can
read. Mix them freely; group either kind into flows with `flow: Billing`.

    - id: insurance
      type: agentNode
      data:
        name: Insurance details
        flow: Intake
        mode: scripted
        say: Let's get your insurance details.
        form:
        - {name: member_id, type: digits, ask: "What's the member ID on your card?", min_length: 6, max_length: 12, keypad: true}
        - {name: plan, type: choice, ask: "Is your plan Gold, Silver or Bronze?", choices: [Gold, Silver, Bronze]}
        - {name: visit_day, type: date, ask: "What day would you like to come in?"}
        max_attempts: 3
        interpret: assisted        # exact: only the built-in parsers read answers; assisted: the model helps with free text

Transfers out of a scripted page:

    data: {label: gold plan, condition: ..., when: 'plan == "Gold"'}   # checked, in order, once the form is complete
    data: {label: everyone else, condition: ..., otherwise: true}       # when no `when` matched
    data: {label: can't read it, condition: ..., on: no_match}          # after max_attempts failed answers
    data: {label: wants a person, condition: The caller asks for a person.}   # anything else: the model checks it on
                                                                               # every caller turn (like an intent)

LLM steps can have `when` transfers too: they're checked after each reply,
without asking the model. Expressions are a small, safe language: names of
variables, "text", numbers, true/false/null, == != < <= > >=, in, not in,
and, or, not, and parentheses.
"""

from __future__ import annotations

import ast
import datetime as dt
import re
import warnings
from typing import Any

FIELD_TYPES = ("text", "number", "integer", "yes_no", "choice", "date", "time", "digits", "phone", "email")
FIELD_KEYS = ("name", "type", "ask", "required", "reprompt", "confirm", "choices", "min", "max", "min_length",
              "max_length", "pattern", "keypad", "description")
PAGE_KEYS = ("mode", "say", "form", "max_attempts", "interpret", "no_match_say", "done_say")
KEYPAD_TYPES = ("number", "integer", "digits", "phone", "yes_no", "choice")
NAME = re.compile(r"^[A-Za-z_]\w{0,63}$")


def scripted(data: dict | None) -> bool:
    return isinstance(data, dict) and data.get("mode") == "scripted"


def route_kind(edge_data: dict | None) -> str:
    """when | otherwise | no_match | ask (the model decides)."""
    d = edge_data or {}
    if d.get("on") == "no_match":
        return "no_match"
    if d.get("otherwise"):
        return "otherwise"
    if str(d.get("when") or "").strip():
        return "when"
    return "ask"


# --------------------------------------------------------------------- expressions

class ExpressionError(ValueError):
    pass


_OPS = (ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.In, ast.NotIn)
_ALIASES = {"true": True, "false": False, "null": None, "none": None, "True": True, "False": False, "None": None}


def compile_expression(text: str) -> ast.Expression:
    src = str(text or "").strip()
    if not src:
        raise ExpressionError("the condition is empty")
    if len(src) > 500:
        raise ExpressionError("the condition is too long (500 characters at most)")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(src.replace("&&", " and ").replace("||", " or "), mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"can't read {src!r}: {exc.msg}") from None
    for node in ast.walk(tree):
        ok = isinstance(node, (ast.Expression, ast.BoolOp, ast.And, ast.Or, ast.UnaryOp, ast.Not, ast.USub, ast.Compare,
                               ast.Name, ast.Load, ast.Constant, ast.List, ast.Tuple) + _OPS)
        if not ok:
            raise ExpressionError(f"{src!r} uses something conditions don't allow ({type(node).__name__})")
        if isinstance(node, ast.Constant) and not isinstance(node.value, (str, int, float, bool, type(None))):
            raise ExpressionError(f"{src!r} has an unsupported value")
    return tree


def names_in(text: str) -> set[str]:
    try:
        tree = compile_expression(text)
    except ExpressionError:
        return set()
    return {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id not in _ALIASES}


def evaluate(text: str, variables: dict[str, Any]) -> bool:
    """True/False; a comparison that can't be made (a missing value, text against a number) is False."""
    tree = compile_expression(text)

    def val(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in _ALIASES:
                return _ALIASES[node.id]
            return _coerce(variables.get(node.id))
        if isinstance(node, (ast.List, ast.Tuple)):
            return [val(x) for x in node.elts]
        if isinstance(node, ast.UnaryOp):
            v = val(node.operand)
            return (not v) if isinstance(node.op, ast.Not) else -v
        if isinstance(node, ast.BoolOp):
            vals = (val(x) for x in node.values)
            return all(vals) if isinstance(node.op, ast.And) else any(vals)
        if isinstance(node, ast.Compare):
            left = val(node.left)
            for op, right_node in zip(node.ops, node.comparators):
                right = val(right_node)
                if not _compare(op, left, right):
                    return False
                left = right
            return True
        raise ExpressionError("unsupported")

    try:
        return bool(val(tree.body))
    except (TypeError, ExpressionError):
        return False


def _coerce(v: Any) -> Any:
    """Values from forms and tools: numeric text compares as a number."""
    if isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v.strip()):
        return float(v) if "." in v else int(v)
    return v


def _compare(op: ast.cmpop, a: Any, b: Any) -> bool:
    if isinstance(a, str) and isinstance(b, str):
        a, b = a.strip().lower(), b.strip().lower()
    if isinstance(op, (ast.In, ast.NotIn)):
        if isinstance(b, list):
            b = [x.strip().lower() if isinstance(x, str) else x for x in b]
            if isinstance(a, str):
                a = a.strip().lower()
        hit = a in b if b is not None else False
        return hit if isinstance(op, ast.In) else not hit
    if isinstance(op, ast.Eq):
        return a == b
    if isinstance(op, ast.NotEq):
        return a != b
    if a is None or b is None:
        return False
    return {ast.Lt: a < b, ast.LtE: a <= b, ast.Gt: a > b, ast.GtE: a >= b}[type(op)]


# --------------------------------------------------------------------- reading answers

_SMALL = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                     "fourteen fifteen sixteen seventeen eighteen nineteen".split())}
_TENS = {w: 10 * (i + 2) for i, w in enumerate("twenty thirty forty fifty sixty seventy eighty ninety".split())}
_DIGIT_WORDS = {"zero": "0", "oh": "0", "o": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
                "six": "6", "seven": "7", "eight": "8", "nine": "9"}
YES = {"yes", "yeah", "yep", "yup", "sure", "correct", "right", "ok", "okay", "affirmative", "absolutely", "definitely",
       "si", "sí", "that's right", "that is right", "of course", "please do"}
NO = {"no", "nope", "nah", "negative", "incorrect", "wrong", "not really", "no thanks", "don't", "do not"}
_WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"]


def _words(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def spoken_number(text: str) -> float | None:
    """'12', '3.5', 'twenty one', 'a hundred and five' -> number; None when there isn't exactly one."""
    t = (text or "").lower().replace(",", "")
    nums = re.findall(r"-?\d+(?:\.\d+)?", t)
    if len(nums) == 1:
        return float(nums[0]) if "." in nums[0] else int(nums[0])
    if nums:
        return None
    total, cur, seen = 0, 0, False
    for w in _words(t):
        if w in ("a", "and") and seen:
            continue
        if w in _SMALL:
            cur += _SMALL[w]
            seen = True
        elif w in _TENS:
            cur += _TENS[w]
            seen = True
        elif w == "hundred" and seen or w == "hundred" and cur == 0:
            cur = (cur or 1) * 100
            seen = True
        elif w == "thousand":
            total += (cur or 1) * 1000
            cur, seen = 0, True
        elif w == "a" and not seen:
            continue
    return total + cur if seen else None


def spoken_digits(text: str) -> str:
    out = []
    for w in re.findall(r"\d+|[a-z]+", (text or "").lower()):
        if w.isdigit():
            out.append(w)
        elif w in _DIGIT_WORDS:
            out.append(_DIGIT_WORDS[w])
        elif w in ("double", "triple"):
            out.append(w)
    digits, i = [], 0
    while i < len(out):                     # "double five" -> 55
        if out[i] in ("double", "triple") and i + 1 < len(out) and out[i + 1].isdigit():
            digits.append(out[i + 1][0] * (2 if out[i] == "double" else 3) + out[i + 1][1:])
            i += 2
        else:
            if out[i].isdigit():
                digits.append(out[i])
            i += 1
    return "".join(digits)


def choice_options(choices: Any) -> list[tuple[str, list[str]]]:
    """(value, [value, synonyms…]) for each choice."""
    out = []
    for c in choices or []:
        if isinstance(c, dict):
            value = str(c.get("value") or "")
            out.append((value, [value] + [str(s) for s in c.get("synonyms") or []]))
        else:
            out.append((str(c), [str(c)]))
    return out


def parse_answer(field: dict, text: str, *, today: dt.date | None = None, keypad: bool = False) -> tuple[bool, Any, str]:
    """(ok, value, why-not). Deterministic: the same words always give the same answer."""
    kind = field.get("type") or "text"
    raw = (text or "").strip()
    low = raw.lower().strip(" .!?")
    if not raw:
        return False, None, "no answer"
    if kind == "text":
        value: Any = raw
    elif kind in ("number", "integer"):
        n = spoken_number(raw)
        if n is None:
            return False, None, "not a number"
        if kind == "integer":
            if isinstance(n, float) and not n.is_integer():
                return False, None, "not a whole number"
            n = int(n)
        if field.get("min") is not None and n < float(field["min"]):
            return False, None, f"below {field['min']}"
        if field.get("max") is not None and n > float(field["max"]):
            return False, None, f"above {field['max']}"
        value = n
    elif kind == "yes_no":
        if keypad and low in ("1", "2"):
            return True, low == "1", ""
        words = " ".join(_words(low))
        yes = any(re.search(rf"\b{re.escape(y)}\b", words) for y in YES)
        no = any(re.search(rf"\b{re.escape(n)}\b", words) for n in NO)
        if yes == no:
            return False, None, "not a yes or no"
        value = yes
    elif kind == "choice":
        options = choice_options(field.get("choices"))
        if keypad and low.isdigit() and 1 <= int(low) <= len(options):
            return True, options[int(low) - 1][0], ""
        words = " " + " ".join(_words(low)) + " "
        hits = {v for v, names in options if any(f" {' '.join(_words(n))} " in words for n in names if _words(n))}
        if len(hits) != 1:
            return False, None, "not one of the choices" if not hits else "more than one choice"
        value = hits.pop()
    elif kind == "date":
        d = parse_date(low, today or dt.date.today())
        if d is None:
            return False, None, "not a date"
        value = d.isoformat()
    elif kind == "time":
        t = parse_time(low)
        if t is None:
            return False, None, "not a time"
        value = t.strftime("%H:%M")
    elif kind in ("digits", "phone"):
        digits = spoken_digits(raw)
        lo = int(field.get("min_length") or (7 if kind == "phone" else 1))
        hi = int(field.get("max_length") or (15 if kind == "phone" else 64))
        if not digits or not lo <= len(digits) <= hi:
            return False, None, f"needs {lo}{'' if lo == hi else f' to {hi}'} digits"
        value = ("+" + digits if raw.strip().startswith("+") else digits) if kind == "phone" else digits
    elif kind == "email":
        t = re.sub(r"\s+at\s+", "@", low)
        t = re.sub(r"\s+dot\s+", ".", t).replace(" ", "")
        m = re.search(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", t)
        if not m:
            return False, None, "not an email address"
        value = m.group(0)
    else:
        return False, None, f"unknown type {kind}"
    pattern = field.get("pattern")
    if pattern and isinstance(value, str) and not re.fullmatch(str(pattern), value):
        return False, None, "doesn't match the expected format"
    return True, value, ""


def parse_date(text: str, today: dt.date) -> dt.date | None:
    t = text.lower().strip()
    if re.search(r"\btoday\b", t):
        return today
    if re.search(r"\bday after tomorrow\b", t):
        return today + dt.timedelta(days=2)
    if re.search(r"\btomorrow\b", t):
        return today + dt.timedelta(days=1)
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if m:
        return _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
    if m:
        year = int(m.group(3)) if m.group(3) else today.year
        year += 2000 if year < 100 else 0
        d = _safe_date(year, int(m.group(1)), int(m.group(2)))
        return d if d is None or m.group(3) or d >= today else _safe_date(year + 1, int(m.group(1)), int(m.group(2)))
    for i, name in enumerate(_MONTHS):
        m = re.search(rf"\b{name[:3]}[a-z]*\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b|\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{name[:3]}[a-z]*\b", t)
        if m:
            day = int(m.group(1) or m.group(2))
            d = _safe_date(today.year, i + 1, day)
            return d if d is None or d >= today else _safe_date(today.year + 1, i + 1, day)
    for i, name in enumerate(_WEEKDAYS):
        if re.search(rf"\b{name}\b", t):
            ahead = (i - today.weekday()) % 7 or 7
            if re.search(rf"\bnext\s+{name}\b", t) and ahead < 7:
                ahead += 7 if (i - today.weekday()) % 7 == 0 else 0
            return today + dt.timedelta(days=ahead)
    m = re.search(r"\bthe\s+(\d{1,2})(?:st|nd|rd|th)\b", t)
    if m:
        d = _safe_date(today.year, today.month, int(m.group(1)))
        if d is not None and d < today:
            nm = today.month % 12 + 1
            d = _safe_date(today.year + (today.month == 12), nm, int(m.group(1)))
        return d
    return None


def _safe_date(y: int, m: int, d: int) -> dt.date | None:
    try:
        return dt.date(y, m, d)
    except ValueError:
        return None


def parse_time(text: str) -> dt.time | None:
    t = text.lower().strip().replace(".", "")
    if re.search(r"\bnoon\b|\bmidday\b", t):
        return dt.time(12, 0)
    if re.search(r"\bmidnight\b", t):
        return dt.time(0, 0)
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm|a m|p m|in the morning|in the afternoon|in the evening)?\b", t)
    if not m:
        n = spoken_number(t.split(" o'clock")[0]) if re.search(r"o'?clock|\bpm\b|\bam\b", t) else None
        if n is None or not 1 <= int(n) <= 12:
            return None
        h, mins, ap = int(n), 0, "pm" if "pm" in t or "afternoon" in t or "evening" in t else "am" if "am" in t or "morning" in t else None
    else:
        h, mins, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ap is None and m.group(2) is None and not re.search(r"o'?clock", t):
            return None                                   # a bare "3" isn't clearly a time
        ap = ("pm" if ap and ("p" in ap or "afternoon" in ap or "evening" in ap) else "am" if ap else None)
    if h > 23 or mins > 59:
        return None
    if ap == "pm" and h < 12:
        h += 12
    elif ap == "am" and h == 12:
        h = 0
    return dt.time(h, mins)


# --------------------------------------------------------------------- checks

def problems(data: dict) -> list[tuple[str, str]]:
    """(severity, message) for a scripted page's settings."""
    out: list[tuple[str, str]] = []
    err = lambda m: out.append(("error", m))     # noqa: E731
    warn = lambda m: out.append(("warning", m))  # noqa: E731
    form = data.get("form") or []
    if not isinstance(form, list):
        return [("error", "form must be a list of fields")]
    for when in ("on_entry", "on_done"):
        if data.get(when) is not None and not (isinstance(data[when], list) and all(isinstance(x, str) for x in data[when])):
            err(f"{when} must be a list of tool names")
    if data.get("interpret", "assisted") not in ("assisted", "exact"):
        err("interpret must be assisted or exact")
    attempts = data.get("max_attempts", 3)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 10:
        err("max_attempts must be a whole number from 1 to 10")
    seen: set[str] = set()
    for f in form:
        if not isinstance(f, dict) or not NAME.match(str(f.get("name") or "")):
            err("each field needs a name made of letters, digits and underscores")
            continue
        where = f"field {f['name']}"
        if f["name"] in seen:
            err(f"{where} is listed twice")
        seen.add(f["name"])
        unknown = sorted(set(f) - set(FIELD_KEYS))
        if unknown:
            err(f"{where}: unknown key(s) {', '.join(unknown)}")
        kind = f.get("type") or "text"
        if kind not in FIELD_TYPES:
            err(f"{where}: type must be one of {', '.join(FIELD_TYPES)}")
        if not str(f.get("ask") or "").strip():
            err(f"{where}: needs the exact question to ask (ask)")
        if kind == "choice" and not choice_options(f.get("choices")):
            err(f"{where}: a choice field needs choices")
        if f.get("pattern"):
            try:
                re.compile(str(f["pattern"]))
            except re.error as exc:
                err(f"{where}: pattern isn't a valid regular expression ({exc})")
        if f.get("keypad") and kind not in KEYPAD_TYPES:
            warn(f"{where}: keypad input only works for {', '.join(KEYPAD_TYPES)}")
        for k in ("min", "max", "min_length", "max_length"):
            if f.get(k) is not None and not isinstance(f[k], (int, float)):
                err(f"{where}: {k} must be a number")
    return out


def describe(data: dict) -> str:
    """Plain instructions for a scripted page, for platforms that only run LLM steps (CX Agent Studio, playbooks)."""
    lines = []
    if data.get("on_entry"):
        lines.append("First use " + ", ".join(map(str, data["on_entry"])) + ".")
    if str(data.get("say") or "").strip():
        lines.append(f"Say exactly: “{data['say'].strip()}”")
    form = [f for f in data.get("form") or [] if isinstance(f, dict)]
    if form:
        lines.append("Then ask these questions one at a time, word for word, and check each answer:")
        for f in form:
            extra = ""
            if f.get("type") == "choice":
                extra = f" (one of: {', '.join(v for v, _ in choice_options(f.get('choices')))})"
            elif f.get("type") and f.get("type") != "text":
                extra = f" ({str(f['type']).replace('_', ' or ')})"
            lines.append(f"- {f.get('name')}: “{str(f.get('ask') or '').strip()}”{extra}")
            if f.get("reprompt"):
                lines.append(f"  If the answer doesn't fit, say: “{f['reprompt']}”")
    if data.get("on_done"):
        lines.append("When you have every answer, use " + ", ".join(map(str, data["on_done"])) + ".")
    if str(data.get("done_say") or "").strip():
        lines.append(f"When you have everything, say: “{data['done_say'].strip()}”")
    return "\n".join(lines) or "Follow the script for this step."


# --------------------------------------------------------------------- Dialogflow CX conditions

_DF_VAR = re.compile(r"\$(?:session\.params|page\.params|flow)\.([A-Za-z_][\w-]*)")


def from_dialogflow(cond: str) -> str | None:
    """'$session.params.plan = "gold" AND $session.params.age >= 18' -> 'plan == "gold" and age >= 18'.
    None when it uses something we can't express (functions, system values)."""
    c = str(cond or "").strip()
    if not c:
        return None
    c = _DF_VAR.sub(lambda m: re.sub(r"\W", "_", m.group(1)), c)
    if "$" in c or "(" in c and re.search(r"\w\(", c):
        return None
    c = re.sub(r"(?<![<>!=])=(?!=)", "==", c)
    c = re.sub(r"\bAND\b", "and", c)
    c = re.sub(r"\bOR\b", "or", c)
    c = re.sub(r"\bNOT\b", "not", c)
    c = re.sub(r"\bnull\b", "null", c, flags=re.I)
    c = re.sub(r"\btrue\b", "true", c, flags=re.I)
    c = re.sub(r"\bfalse\b", "false", c, flags=re.I)
    try:
        compile_expression(c)
    except ExpressionError:
        return None
    return _outer(c)


def _outer(text: str) -> str:
    """Drop parentheses that wrap the whole expression."""
    t = text.strip()
    while t.startswith("(") and t.endswith(")"):
        depth = 0
        for i, ch in enumerate(t):
            depth += ch == "("
            depth -= ch == ")"
            if depth == 0 and i < len(t) - 1:
                return t
        t = t[1:-1].strip()
    return t


def to_dialogflow(expr: str) -> str:
    """Our condition -> Dialogflow CX's condition syntax."""
    return _outer(_to_dialogflow(expr))


def _to_dialogflow(expr: str) -> str:
    tree = compile_expression(expr)

    def out(node: ast.AST) -> str:
        if isinstance(node, ast.Constant):
            if node.value is None:
                return "null"
            if isinstance(node.value, bool):
                return "true" if node.value else "false"
            return f'"{node.value}"' if isinstance(node.value, str) else str(node.value)
        if isinstance(node, ast.Name):
            return {"true": "true", "false": "false", "null": "null"}.get(node.id, f"$session.params.{node.id}")
        if isinstance(node, ast.BoolOp):
            joiner = " AND " if isinstance(node.op, ast.And) else " OR "
            return "(" + joiner.join(out(v) for v in node.values) + ")"
        if isinstance(node, ast.UnaryOp):
            return f"NOT {out(node.operand)}" if isinstance(node.op, ast.Not) else f"-{out(node.operand)}"
        if isinstance(node, ast.Compare):
            parts, left = [], node.left
            for op, right in zip(node.ops, node.comparators):
                if isinstance(op, (ast.In, ast.NotIn)) and isinstance(right, (ast.List, ast.Tuple)):
                    alts = " OR ".join(f"{out(left)} = {out(x)}" for x in right.elts)
                    parts.append(f"({alts})" if isinstance(op, ast.In) else f"NOT ({alts})")
                else:
                    sym = {ast.Eq: "=", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}.get(type(op), "=")
                    parts.append(f"{out(left)} {sym} {out(right)}")
                left = right
            return parts[0] if len(parts) == 1 else "(" + " AND ".join(parts) + ")"
        if isinstance(node, (ast.List, ast.Tuple)):
            return "[" + ", ".join(out(x) for x in node.elts) + "]"
        raise ExpressionError("can't write that for Dialogflow CX")

    text = out(tree.body)
    return text[1:-1] if text.startswith("(") and text.endswith(")") and text.count("(") == 1 else text
