"""Connector actions an agent can use as tools (send an email, a WhatsApp or
Telegram message, find free times or book in Google Calendar).

    tools:
    - name: send_confirmation
      connector: email.send
      description: Email the caller their booking details.
      with: {to: "{{caller_email}}", subject: "Your appointment"}   # fixed or from variables
      mock: {sent: true}

The connection itself (keys, accounts) is set up once on the Integrations page;
nothing about it is stored in the agent folder. `with` pins arguments so the
model can't choose them. A recipient (`to`, `chat_id`) is never chosen by the
model: pin it in `with`, or, in a WhatsApp, Telegram or email conversation,
leave it out and the reply goes to whoever wrote in.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Param:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    recipient: bool = False      # never filled by the model


@dataclass(frozen=True)
class Action:
    id: str
    label: str
    service: str                 # which Integrations tile sets it up: email | whatsapp | telegram | google
    description: str
    params: tuple[Param, ...] = field(default_factory=tuple)
    options: tuple[Param, ...] = field(default_factory=tuple)   # only in `with`, never asked of the model
    sends: bool = False          # sends something to a person: limited per conversation


ACTIONS: dict[str, Action] = {a.id: a for a in (
    Action("email.send", "Send an email", "email", "Send an email.",
           (Param("to", description="email address", required=True, recipient=True),
            Param("subject", description="subject line", required=True),
            Param("body", description="the message, in plain text", required=True)),
           sends=True),
    Action("whatsapp.send", "Send a WhatsApp message", "whatsapp", "Send a WhatsApp message.",
           (Param("to", description="phone number with country code, like +15551234567", required=True, recipient=True),
            Param("text", description="the message", required=True)),
           sends=True),
    Action("telegram.send", "Send a Telegram message", "telegram", "Send a Telegram message.",
           (Param("chat_id", description="Telegram chat id", required=True, recipient=True),
            Param("text", description="the message", required=True)),
           sends=True),
    Action("calendar.free_times", "Find free times", "google",
           "Find open times on the calendar for a day.",
           (Param("day", description="the day, as YYYY-MM-DD", required=True),
            Param("duration_minutes", "integer", "how long the appointment is, in minutes")),
           (Param("calendar_id", description="calendar to check (default: the one in Integrations)"),
            Param("hours", description="opening hours like 09:00-17:00 (default: Integrations)"))),
    Action("data.lookup", "Look up a record", "data",
           "Look up a record by what the caller gave.",
           (Param("value", description="the value to look for, as the caller gave it", required=True),),
           (Param("collection", description="the collection to search", required=True),
            Param("field", description="the field to match, like confirmation_code", required=True),
            Param("fields", "list", "only these fields go back to the model (default: all)"),
            Param("where", "mapping", "more fields that must match, fixed or from variables"),
            Param("limit", "integer", "most records to return (default 3)"))),
    Action("data.save", "Save a record", "data",
           "Save the details from this conversation.",
           (),
           (Param("collection", description="the collection to save into", required=True),
            Param("values", "mapping", "the record: field names and values, fixed or from variables", required=True),
            Param("ask", "list", "fields the model fills in from the conversation, like reason"))),
    Action("knowledge.search", "Search knowledge", "knowledge",
           "Search the knowledge base for facts to answer the caller.",
           (Param("query", description="what to look up, in a few words", required=True),),
           (Param("base", description="the knowledge base", required=True),
            Param("results", "integer", "how many passages come back (default 3)"))),
    Action("calendar.book", "Book in Google Calendar", "google",
           "Book an appointment on the calendar.",
           (Param("start", description="start time, as YYYY-MM-DDTHH:MM in the business's time zone", required=True),
            Param("summary", description="a short title, like 'Cleaning for Sam'", required=True),
            Param("duration_minutes", "integer", "how long, in minutes"),
            Param("attendee_email", description="the caller's email, if they want an invitation", recipient=True)),
           (Param("calendar_id", description="calendar to book on (default: the one in Integrations)"),
            Param("description", description="text for the event's description"))),
)}

SERVICES = {"email": "Email", "whatsapp": "WhatsApp", "telegram": "Telegram", "google": "Google Calendar",
            "data": "Data", "knowledge": "Knowledge"}
LOCAL = {"data", "knowledge"}    # kept by the engine itself: nothing to set up in Integrations
_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
MAX_SENDS = 5                    # per conversation, across all sending actions


def problems(tool: dict) -> list[tuple[str, str]]:
    """(severity, message) for a connector tool's `connector` and `with`."""
    out: list[tuple[str, str]] = []
    action = ACTIONS.get(str(tool.get("connector") or ""))
    if action is None:
        return [("error", f"connector must be one of {', '.join(ACTIONS)}")]
    pinned = tool.get("with") or {}
    if not isinstance(pinned, dict):
        return [("error", "with must be a mapping of argument names to values")]
    known = {p.name for p in action.params + action.options}
    unknown = sorted(set(pinned) - known)
    if unknown:
        out.append(("error", f"{action.id} has no argument {', '.join(unknown)} (it takes {', '.join(sorted(known))})"))
    for p in action.params:
        if p.recipient and p.required and p.name not in pinned:
            out.append(("warning", f"{p.name} isn't set in `with`, so this only works in a message conversation, "
                                   "where it replies to whoever wrote in"))
    for p in action.options:
        if p.required and pinned.get(p.name) in (None, "", {}, []):
            out.append(("error", f"{action.id} needs {p.name} in `with`"))
    for k, v in pinned.items():
        if "secrets." in str(v):
            out.append(("error", f"with.{k} can't use secrets"))
    out += _local_problems(action.id, pinned, tool.get("save"))
    if tool.get("url") or tool.get("method") or tool.get("headers") or tool.get("body") or tool.get("parameters"):
        out.append(("error", "a connector tool takes connector, with, description, say_while_running, save and mock; "
                             "the connection is set up in Integrations"))
    return out


def _local_problems(action: str, pinned: dict, save: dict | None = None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    keep = pinned.get("fields")
    if action == "data.lookup" and isinstance(keep, list) and keep and isinstance(save, dict):
        for var, path in save.items():
            m = re.match(r"^records\.\d+\.([^.]+)", str(path))
            if m and m.group(1) not in keep and m.group(1) != "id":
                out.append(("warning", f"save.{var} reads {m.group(1)}, which with.fields leaves out, so it's always empty"))
    for key in ("collection", "base"):
        v = pinned.get(key)
        if isinstance(v, str) and v and "{{" not in v and not _NAME.match(v):
            out.append(("error", f"with.{key}: names are lowercase letters, digits, - and _, starting with a letter"))
    if action == "data.lookup":
        if pinned.get("field") and not _FIELD.match(str(pinned["field"])) and pinned.get("field") != "id":
            out.append(("error", "with.field must be a field name like member_id"))
        if "fields" in pinned and not (isinstance(pinned["fields"], list) and all(isinstance(f, str) for f in pinned["fields"])):
            out.append(("error", "with.fields must be a list of field names"))
        if "where" in pinned and not isinstance(pinned["where"], dict):
            out.append(("error", "with.where must map field names to values"))
    if action == "data.save":
        values = pinned.get("values")
        if values is not None and not isinstance(values, dict):
            out.append(("error", "with.values must map field names to values"))
        ask = pinned.get("ask", [])
        if not (isinstance(ask, list) and all(isinstance(a, str) and re.match(r"^[a-z][a-z0-9_]{0,40}$", a) for a in ask)):
            out.append(("error", "with.ask must be a list of lowercase names like reason"))
        elif isinstance(values, dict):
            unused = [a for a in ask if not any(f"{{{{{a}}}}}" in str(v).replace(" ", "") for v in values.values())]
            if unused:
                out.append(("warning", f"with.ask: {', '.join(unused)} isn't used in values, so what the model fills in is dropped"))
    for key in ("limit", "results"):
        if key in pinned and not (isinstance(pinned[key], int) and 1 <= pinned[key] <= 10):
            out.append(("error", f"with.{key} must be a whole number from 1 to 10"))
    return out


def model_params(tool: dict) -> list[Param]:
    """What the model fills in: the action's parameters, minus pinned ones and recipients."""
    action = ACTIONS[tool["connector"]]
    pinned = tool.get("with") or {}
    if action.id == "data.save":                  # the fields it asks for, named in `with.ask`
        ask = pinned.get("ask") if isinstance(pinned.get("ask"), list) else []
        return [Param(str(a), description=str(a).replace("_", " "), required=True) for a in ask if isinstance(a, str)]
    return [p for p in action.params if p.name not in pinned and not p.recipient]
