"""Triggers: what starts an agent besides an incoming phone call, and what
happens after a conversation. They live in agent.yaml under `triggers:` and
run in the engine.

    triggers:
    - id: telegram-chat           # people chat with this agent on Telegram
      type: message
      channel: telegram           # telegram | whatsapp | email
      enabled: true

    - id: notify-desk             # after each conversation, send a note
      type: after_call
      when: reached_end           # always | reached_end | collected:<variable>
      channels: [phone, whatsapp] # where the conversation happened (default: all but browser tests)
      send:
        via: email                # email | whatsapp | telegram | webhook
        to: desk@example.test
        subject: "New request from {{caller_name | a caller}}"
        text: "{{summary}}\\n\\n{{details}}"

    - id: reminders               # at set times, call or message a list of people
      type: schedule
      cron: "0 17 * * 1-5"        # minute hour day-of-month month weekday, in the business's time zone
      action: call                # call | message
      channel: whatsapp           # for action: message
      recipients:
      - {to: "+15551234567", context: {caller_name: Sam, appointment_time: "9:30 AM"}}
      recipients_from:            # or fetch the list: a JSON array of {to, context}
        url: https://scheduling.example.test/v1/reminders/tomorrow
        headers: {Authorization: "Bearer {{secrets.SCHEDULING_KEY}}"}
      text: "Hi {{caller_name}}, a reminder of your visit tomorrow at {{appointment_time}}."
      enabled: false

The API trigger (start a call or message from another system) and an
after-call HTTP request are the "API Trigger" and "Webhook" nodes. Outbound calls and messages are off until
you turn them on in Integrations, and respect quiet hours and a daily limit.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from .tools import SECRET_NAME, _address, _headers, template_names

TYPES = ("message", "after_call", "schedule")
CHANNELS = ("telegram", "whatsapp", "email")
CONVERSATION_CHANNELS = ("phone", "web", "telegram", "whatsapp", "email")
DEFAULT_CHANNELS = ("phone", "telegram", "whatsapp", "email")       # not browser tests
VIA = ("email", "whatsapp", "telegram", "webhook")
ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
KEYS = {"message": ("id", "type", "enabled", "channel", "description"),
        "after_call": ("id", "type", "enabled", "when", "channels", "send", "description"),
        "schedule": ("id", "type", "enabled", "cron", "action", "channel", "recipients", "recipients_from", "text",
                     "description")}
SEND_KEYS = ("via", "to", "subject", "text", "url", "method", "headers", "payload")
TEMPLATE_VARIABLES = ("summary", "details", "transcript", "agent", "channel", "contact", "ended_reason",
                      "duration_seconds", "session_id", "steps")


# --------------------------------------------------------------------- cron

_FIELDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_NAMES = {"sun": 0, "mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6,
          "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
          "nov": 11, "dec": 12}


class CronError(ValueError):
    pass


def parse_cron(expr: str) -> list[set[int]]:
    """Five fields: minute hour day-of-month month weekday (0 or 7 = Sunday). *, lists, ranges, steps, names."""
    parts = str(expr or "").split()
    if len(parts) != 5:
        raise CronError("cron needs five fields: minute hour day-of-month month weekday, like 0 17 * * 1-5")
    out = []
    for i, (part, (lo, hi)) in enumerate(zip(parts, _FIELDS)):
        values: set[int] = set()
        for item in part.lower().split(","):
            rng, _, step = item.partition("/")
            try:
                every = int(step) if step else 1
                if rng == "*":
                    a, b = lo, hi
                elif "-" in rng:
                    x, y = rng.split("-", 1)
                    a, b = int(_NAMES.get(x, x)), int(_NAMES.get(y, y))
                else:
                    a = int(_NAMES.get(rng, rng))
                    b = hi if step else a
            except ValueError as exc:
                raise CronError(f"can't read {item!r} in {expr!r}") from exc
            if every < 1 or not (lo <= a <= hi and lo <= b <= hi) or a > b:
                raise CronError(f"{item!r} is out of range in {expr!r}")
            values |= set(range(a, b + 1, every))
        if i == 4 and 7 in values:                 # 7 is Sunday too
            values = (values - {7}) | {0}
        out.append(values)
    return out


def cron_matches(expr: str, when: dt.datetime) -> bool:
    minute, hour, dom, month, dow = parse_cron(expr)
    weekday = (when.weekday() + 1) % 7            # cron: Sunday = 0
    some_days, some_weekdays = len(dom) < 31, len(dow) < 7
    if some_days and some_weekdays:               # both restricted: either one matching is enough (classic cron)
        day_ok = when.day in dom or weekday in dow
    else:
        day_ok = (when.day in dom) and (weekday in dow)
    return when.minute in minute and when.hour in hour and when.month in month and day_ok


def describe_cron(expr: str) -> str:
    """A rough plain-words reading for the UI."""
    try:
        minute, hour, dom, month, dow = parse_cron(expr)
    except CronError:
        return ""
    if len(minute) == 1 and len(hour) == 1:
        at = dt.time(min(hour), min(minute)).strftime("%I:%M %p").lstrip("0")
    else:
        at = "several times"
    days = ("every day" if len(dow) == 7 and len(dom) == 31 else
            "weekdays" if dow == {1, 2, 3, 4, 5} and len(dom) == 31 else
            ", ".join(("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")[d] for d in sorted(dow)) if len(dom) == 31 else
            f"day {', '.join(map(str, sorted(dom)))} of the month")
    return f"{days} at {at}" if at != "several times" else f"{days}, several times a day"


# --------------------------------------------------------------------- checks

def problems(trigger: Any) -> list[tuple[str, str]]:
    if not isinstance(trigger, dict):
        return [("error", "each trigger must be a mapping with an id and a type")]
    out: list[tuple[str, str]] = []
    err = lambda m: out.append(("error", m))     # noqa: E731
    warn = lambda m: out.append(("warning", m))  # noqa: E731
    kind = trigger.get("type")
    if kind not in TYPES:
        return [("error", f"type must be one of {', '.join(TYPES)}")]
    if not isinstance(trigger.get("id"), str) or not ID.match(trigger["id"]):
        err("id must be lowercase letters, digits and dashes (up to 40)")
    unknown = sorted(set(trigger) - set(KEYS[kind]))
    if unknown:
        err(f"unknown key(s) {', '.join(unknown)} for a {kind} trigger (known: {', '.join(KEYS[kind])})")
    if "enabled" in trigger and not isinstance(trigger["enabled"], bool):
        err("enabled must be true or false")
    if kind == "message":
        if trigger.get("channel") not in CHANNELS:
            err(f"channel must be one of {', '.join(CHANNELS)}")
    elif kind == "after_call":
        when = str(trigger.get("when") or "always")
        if when not in ("always", "reached_end") and not re.fullmatch(r"collected:[A-Za-z_]\w{0,63}", when):
            err("when must be always, reached_end or collected:<variable>")
        channels = trigger.get("channels")
        if channels is not None and (not isinstance(channels, list) or set(channels) - set(CONVERSATION_CHANNELS)):
            err(f"channels must be a list of {', '.join(CONVERSATION_CHANNELS)}")
        _send(trigger.get("send"), err, warn)
    else:
        try:
            parse_cron(trigger.get("cron") or "")
        except CronError as exc:
            err(str(exc))
        action = trigger.get("action")
        if action not in ("call", "message"):
            err("action must be call or message")
        if action == "message" and trigger.get("channel") not in CHANNELS:
            err(f"a message schedule needs channel: one of {', '.join(CHANNELS)}")
        recipients = trigger.get("recipients") or []
        if not isinstance(recipients, list) or not all(isinstance(r, dict) and r.get("to") for r in recipients):
            err("recipients must be a list of {to, context}")
        elif len(recipients) > 500:
            err("at most 500 recipients in a list; use recipients_from for more")
        else:
            for r in recipients:
                if r.get("context") is not None and not isinstance(r.get("context"), dict):
                    err("each recipient's context must be a mapping")
                    break
        src = trigger.get("recipients_from")
        if src is not None:
            if not isinstance(src, dict) or set(src) - {"url", "headers"}:
                err("recipients_from takes url and headers")
            else:
                _address(src.get("url") or "", err, warn)
                _headers(src.get("headers") or {}, err)
                if any(not n.startswith("secrets.") for n in template_names([src.get("url"), src.get("headers")])):
                    err("recipients_from can only use {{secrets.NAME}} in its url and headers")
        if not recipients and not src:
            warn("no recipients yet: add some, or recipients_from")
        if any(n.startswith("secrets.") for n in template_names([trigger.get("text"), recipients])):
            err("message text and recipients can't use secrets")
    if trigger.get("enabled") and kind == "schedule":
        warn("runs on its own while the engine is running (outbound must also be on in Integrations)")
    return out


def _send(send: Any, err, warn) -> None:
    if not isinstance(send, dict):
        err("send is required: {via, to, text} (or via: webhook with url)")
        return
    unknown = sorted(set(send) - set(SEND_KEYS))
    if unknown:
        err(f"send: unknown key(s) {', '.join(unknown)}")
    via = send.get("via")
    if via not in VIA:
        err(f"send.via must be one of {', '.join(VIA)}")
        return
    if via == "webhook":
        _address(send.get("url") or "", err, warn)
        _headers(send.get("headers") or {}, err)
        if str(send.get("method") or "POST").upper() not in ("POST", "PUT", "PATCH"):
            err("send.method must be POST, PUT or PATCH")
        if any(not n.startswith("secrets.") for n in template_names(send.get("url"))):
            err("the webhook address can only use {{secrets.NAME}}")
        bad = [n for n in template_names(send.get("headers")) if n.startswith("secrets.") and not SECRET_NAME.match(n[8:])]
        if bad:
            err("secret names use capital letters, digits and underscores")
        return
    if not str(send.get("to") or "").strip():
        err(f"send.to is required (who gets the {via})")
    if not str(send.get("text") or "").strip():
        err("send.text is required")
    if any(n.startswith("secrets.") for n in template_names([send.get("to"), send.get("text"), send.get("subject")])):
        err("send.to, subject and text can't use secrets")


def enabled(trigger: dict) -> bool:
    return bool(trigger.get("enabled", True))
