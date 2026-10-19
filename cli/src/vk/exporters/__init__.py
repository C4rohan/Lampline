"""Export agents to Dialogflow CX (playbooks) or CX Agent Studio.

    from vk.exporters import export
    zip_bytes, report = export(agent, "ces")      # or "dfcx"

Nothing secret leaves: {{secrets.NAME}} becomes a placeholder to fill in there.
"""

from __future__ import annotations

from vk.agentfs import Agent

from .common import ExportError, Report

TARGETS = {"dfcx": "Dialogflow CX", "ces": "CX Agent Studio"}


def export(agent: Agent, target: str, **options) -> tuple[bytes, Report]:
    if target == "ces":
        from . import ces
        return ces.export(agent)
    if target == "dfcx":
        from . import dfcx
        return dfcx.export(agent, **options)
    raise ExportError(f"unknown target {target!r}; use dfcx or ces")


def build(agent: Agent, target: str, **options) -> tuple[dict[str, str], Report]:
    """The files instead of a zip (for writing into a folder)."""
    if target == "ces":
        from . import ces
        return ces.build(agent)
    if target == "dfcx":
        from . import dfcx
        return dfcx.build(agent, **options)
    raise ExportError(f"unknown target {target!r}; use dfcx or ces")


__all__ = ["export", "build", "ExportError", "Report", "TARGETS"]
