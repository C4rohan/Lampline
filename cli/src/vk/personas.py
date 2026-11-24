"""Ready-made caller personas: the tough-caller pack that ships with vk.

Each file in personas/ is a small YAML document:

    name: angry-wants-human            # how a test refers to it
    description: one line for lists    # shown on the Evals page and in docs
    persona: who the caller is and how they talk, for the model playing them

In a test file, `caller: {persona: <name>, goal: ...}` uses the named persona's
text; any other `persona:` value is used as written, so inline personas keep
working. Made-up callers only, and each one is about behaviour (how they talk),
not identity.
"""

from __future__ import annotations

import re
from importlib import resources

import yaml

NAME = re.compile(r"[a-z0-9]+(?:[-_][a-z0-9]+)+")     # persona names look like angry-wants-human


def _load() -> dict[str, dict]:
    """{name: {name, description, persona}} for every shipped personas/*.yaml file."""
    try:
        root = resources.files("vk") / "personas"
        files = sorted(root.iterdir(), key=lambda p: p.name)
    except (FileNotFoundError, NotADirectoryError):
        return {}
    out = {}
    for path in files:
        if not path.name.endswith((".yaml", ".yml")):
            continue
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        name = str(doc.get("name") or path.name.rsplit(".", 1)[0]).strip()
        if name:
            out[name] = {"name": name,
                         "description": str(doc.get("description") or "").strip(),
                         "persona": str(doc.get("persona") or "").strip()}
    return out


PERSONAS = _load()


def lookup(name: str) -> dict | None:
    """The shipped persona called `name`, or None."""
    return PERSONAS.get(str(name or "").strip())


def looks_like_name(text: str) -> bool:
    """True when `text` can't be an inline persona but a name: one lowercase slug like angry-wants-human."""
    return bool(NAME.fullmatch(str(text or "").strip()))


def names() -> list[str]:
    return sorted(PERSONAS)


def all() -> list[dict]:
    """Every shipped persona, in name order (for the Evals page's presets and docs)."""
    return list(PERSONAS.values())
