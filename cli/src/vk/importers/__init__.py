"""Import agents exported from Dialogflow CX or CX Agent Studio.

    from vk.importers import convert
    agent, report = convert(path_or_zip_bytes, source="auto")

Importers only read the export: nothing in it is fetched or run.
"""

from __future__ import annotations

from typing import Any

from .common import Export, ImportError_, Report

SOURCES = {"dfcx": "Dialogflow CX", "ces": "CX Agent Studio"}


def detect(ex: Export) -> str:
    names = list(ex.files)
    if any(n == "agent.json" or n.endswith("/agent.json") for n in names) and any("/flows/" in f"/{n}" or "/playbooks/" in f"/{n}" for n in names):
        return "dfcx"
    if any(n.rsplit("/", 1)[-1] in ("app.json", "app.yaml", "app.yml") for n in names):
        return "ces"
    if any(n == "agent.json" or n.endswith("/agent.json") for n in names):
        return "dfcx"
    raise ImportError_("couldn't tell what this export is: expected a Dialogflow CX JSON package (agent.json, flows/) "
                       "or a CX Agent Studio app export (app.json, agents/)")


def convert(source: Any, source_type: str = "auto", name: str = "") -> tuple[Any, Report]:
    ex = Export.open(source)
    kind = detect(ex) if source_type in ("", "auto") else source_type
    if kind == "dfcx":
        from . import dfcx
        return dfcx.convert(ex, name)
    if kind == "ces":
        from . import ces
        return ces.convert(ex, name)
    raise ImportError_(f"unknown source {source_type!r}; use dfcx, ces or auto")



__all__ = ["convert", "detect", "Export", "ImportError_", "Report", "SOURCES"]
