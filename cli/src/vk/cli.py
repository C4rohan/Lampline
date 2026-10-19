"""vk command line.

    vk lint agents/front-desk [--format json] [--strict]
    vk test agents/front-desk [-k reschedule] [--url http://localhost:7860]   # tests/*.yaml through the engine's text chat
    vk templates list | vk templates install front-desk [--dir agents/x]
    vk import dfcx|ces EXPORT [--dir agents/x]          # Dialogflow CX or CX Agent Studio export -> folder
    vk export dfcx|ces agents/front-desk [--out x.zip]  # folder -> an import for those platforms
    vk skills install [--target claude|agents|gemini]

Agents are folders (agent.yaml + prompts/*.md); the engine runs them as they are on disk.
Exit codes: 0 ok, 1 failure (lint errors, engine error), 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from importlib import resources
from pathlib import Path

from vk import __version__, agentfs, scenario
from vk.engine_client import ApiError, EngineClient
from vk.lint import has_errors, lint


def _out(msg: str = "") -> None:
    print(msg)


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _load_folder(path: str) -> agentfs.Agent:
    return agentfs.load(path)


def _print_findings(findings, fmt: str) -> None:
    if fmt == "json":
        _out(json.dumps([f.as_dict() for f in findings], indent=2))
        return
    if not findings:
        _out("lint: no problems found")
        return
    for f in findings:
        _out(f"{f.severity.upper():7} {f.code}  {f.where}: {f.message}")
    errors = sum(f.severity == "error" for f in findings)
    _out(f"\n{errors} error(s), {len(findings) - errors} warning(s)")


# --- commands --------------------------------------------------------------------

def cmd_lint(args) -> int:
    worst = 0
    for folder in args.dirs:
        agent = _load_folder(folder)
        findings = lint(agent, folder)
        if len(args.dirs) > 1 and args.format == "text":
            _out(f"== {folder}")
        _print_findings(findings, args.format)
        if has_errors(findings) or (args.strict and findings):
            worst = 1
    return worst


SKILL_TARGETS = {"claude": ".claude/skills", "agents": ".agents/skills", "gemini": ".gemini/skills"}


def cmd_skills_install(args) -> int:
    dest_root = Path(args.dest or SKILL_TARGETS[args.target])
    src_root = resources.files("vk") / "skills"
    installed = []
    for skill in sorted(p for p in src_root.iterdir() if p.is_dir()):
        dest = dest_root / skill.name
        dest.mkdir(parents=True, exist_ok=True)
        with resources.as_file(skill / "SKILL.md") as src:
            shutil.copyfile(src, dest / "SKILL.md")
        installed.append(skill.name)
    _out(f"installed {', '.join(installed)} into {dest_root}/")
    return 0


def _templates() -> dict:
    root = resources.files("vk") / "templates"
    return {p.name: p for p in sorted(root.iterdir(), key=lambda p: p.name) if (p / "agent.yaml").is_file()}


def cmd_templates_list(args) -> int:
    import yaml

    for name, path in _templates().items():
        doc = yaml.safe_load((path / "agent.yaml").read_text(encoding="utf-8"))
        setup = doc.get("setup") or {}
        langs = "/".join(setup.get("languages") or [])
        tests = len([p for p in (path / "tests").iterdir()]) if (path / "tests").is_dir() else 0
        _out(f"{name:24} {setup.get('purpose', '')} [{langs}, {len(doc.get('nodes') or [])} steps, {tests} tests]")
    return 0


def _copy_tree(src, dest: Path) -> int:
    count = 0
    for item in src.iterdir():
        if item.is_dir():
            count += _copy_tree(item, dest / item.name)
        elif item.name.endswith((".yaml", ".yml", ".md")):
            (dest).mkdir(parents=True, exist_ok=True)
            (dest / item.name).write_text(item.read_text(encoding="utf-8"), encoding="utf-8")
            count += 1
    return count


def cmd_templates_install(args) -> int:
    templates = _templates()
    if args.name not in templates:
        _err(f"no template {args.name!r}; available: {', '.join(templates)}")
        return 1
    dest = Path(args.dir or f"agents/{args.name}")
    if dest.exists() and any(dest.iterdir()) and not args.force:
        _err(f"{dest} already exists and isn't empty (use --force to overwrite)")
        return 1
    count = _copy_tree(templates[args.name], dest)
    agent = agentfs.load(dest)
    if args.name_as:
        agent.name = args.name_as
        agentfs.write(agent, dest)
    _out(f"installed template {args.name} into {dest}/ [{count} files]")
    _out(f"next: edit context_variables in {dest}/agent.yaml, then `vk lint {dest}` and open it in the engine")
    return 0


def cmd_import(args) -> int:
    from . import importers
    from .lint import lint as run_lint

    src = Path(args.export)
    if not src.exists():
        _err(f"{src} doesn't exist")
        return 1
    try:
        agent, report = importers.convert(src, args.source, args.name or "")
    except importers.ImportError_ as exc:
        _err(f"couldn't import: {exc}")
        return 1
    if not args.name and agent.name == "Imported agent":
        agent.name = src.stem.replace("_", " ").replace("-", " ").strip() or agent.name
    dest = Path(args.dir or f"agents/{re.sub(r'[^a-z0-9]+', '-', agent.name.lower()).strip('-') or 'imported'}")
    if dest.exists() and any(dest.iterdir()) and not args.force:
        _err(f"{dest} already exists and isn't empty (use --force to overwrite)")
        return 1
    agentfs.write(agent, dest)
    (dest / "import-report.md").write_text(report.markdown(agent.name), encoding="utf-8")
    counts = ", ".join(f"{v} {k}" for k, v in report.counts.items() if v)
    _out(f"imported {agent.name!r} from {report.source} into {dest}/ ({counts})")
    findings = run_lint(agent, dest)
    errors = sum(1 for f in findings if f.severity == "error")
    _out(f"{len(report.notes)} thing(s) to review in {dest}/import-report.md; lint: {errors} error(s), {len(findings) - errors} warning(s)")
    _out(f"next: read the report, `vk lint {dest}`, then test it with synthetic data")
    return 0 if not errors else 1


def cmd_export(args) -> int:
    from . import exporters
    from .exporters.common import safe_rel

    try:
        agent = _load_folder(args.dir)
    except agentfs.FolderError as exc:
        _err(str(exc))
        return 1
    options = {"time_zone": args.time_zone, "language": args.language} if args.target == "dfcx" else {}
    try:
        if args.folder:
            files, report = exporters.build(agent, args.target, **options)
        else:
            data, report = exporters.export(agent, args.target, **options)
    except exporters.ExportError as exc:
        _err(f"couldn't export: {exc}")
        return 1
    slug = Path(args.dir).resolve().name
    out = Path(args.out or f"{slug}-{'dialogflow-cx' if args.target == 'dfcx' else 'cx-agent-studio'}{'' if args.folder else '.zip'}")
    if out.exists() and not args.force and (out.is_file() or any(out.iterdir())):
        _err(f"{out} already exists (use --force to overwrite)")
        return 1
    if args.folder:
        root = out.resolve()
        for rel, text in files.items():
            try:
                path = (out / safe_rel(rel)).resolve()
            except exporters.ExportError as exc:
                _err(str(exc))
                return 1
            if not path.is_relative_to(root):
                _err(f"refusing to write {rel!r} outside {out}")
                return 1
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    else:
        out.write_bytes(data)
    _out(f"exported {agent.name!r} for {report.target} to {out} ({', '.join(f'{v} {k}' for k, v in report.counts.items() if v)})")
    for n in report.notes:
        _out(f"  - {n}")
    _out("import it there: " + ("Agent settings → Restore (JSON package), then Validate" if args.target == "dfcx"
                                else "Import app (or apps.importApp with validateOnly to check it first)"))
    return 0


def _print_result(r: scenario.Result, verbose: bool) -> None:
    mark = "PASS" if r.passed else "FAIL"
    steps = " → ".join(n["name"] or n["id"] for n in r.transcript.nodes) or "no steps"
    _out(f"{mark}  {r.scenario.name}  ({len(r.scenario.turns)} turn(s), {r.seconds:.1f}s)  {steps}")
    for f in r.failures:
        _out(f"      - {f}")
    if verbose or not r.passed:
        for who, text in r.transcript.lines:
            _out(f"        {who:>6}: {text}")


def cmd_test(args) -> int:
    folder = Path(args.dir)
    agent = _load_folder(folder)
    try:
        scenarios = scenario.load(folder)
    except scenario.TestFileError as exc:
        _err(f"error: {exc}")
        return 1
    if args.k:
        scenarios = [s for s in scenarios if args.k.lower() in s.name.lower() or args.k in s.path]
    if not scenarios:
        _err(f"no tests found in {folder}/tests/*.yaml" + (f" matching {args.k!r}" if args.k else ""))
        return 1
    url = args.url or os.environ.get("VK_ENGINE_URL") or "http://localhost:7860"
    return _test_on_engine(args, agent, folder, scenarios, url.rstrip("/"))


def _report(args, results) -> int:
    failed = sum(not r.passed for r in results)
    if args.format == "json":
        _out(json.dumps([r.as_dict() for r in results], indent=2, ensure_ascii=False))
    else:
        _out(f"\n{len(results) - failed} passed, {failed} failed")
    return 1 if failed else 0


def _test_on_engine(args, agent, folder: Path, scenarios, url: str) -> int:
    """Run tests on the engine with the folder as it is on disk."""
    findings = lint(agent, folder)
    if has_errors(findings):
        _print_findings([f for f in findings if f.severity == "error"], "text")
        _err("\nnot testing: fix the lint errors above")
        return 1
    key = os.environ.get("VK_ENGINE_KEY") or None
    with EngineClient(url, agent, key, live_tools=args.live_tools, transport=getattr(args, "_transport", None)) as c:
        try:
            info = c.health()
        except ApiError as exc:
            _err(f"error: {exc.message}")
            return 1
        if args.format == "text":
            cfg = info.get("config") or {}
            _out(f"engine {url}: {cfg.get('llm')} {cfg.get('model')}\n")
        results = []
        for sc in scenarios:
            try:
                result = scenario.run(c, sc, agent.context_variables)
            except ApiError as exc:
                result = scenario.Result(sc, scenario.Transcript(), [f"engine error: {exc}"], 0.0)
            results.append(result)
            if args.format == "text":
                _print_result(result, args.verbose)
    return _report(args, results)


# --- parser ----------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vk", description="Build, check and test voice agent folders from the terminal.")
    p.add_argument("--version", action="version", version=f"vk {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("lint", help="check agent folders offline")
    s.add_argument("dirs", nargs="+")
    s.add_argument("--format", choices=["text", "json"], default="text")
    s.add_argument("--strict", action="store_true", help="fail on warnings too")
    s.set_defaults(func=cmd_lint)

    s = sub.add_parser("test", help="run the folder's tests/*.yaml conversations on the engine")
    s.add_argument("dir")
    s.add_argument("-k", help="only run tests whose name or file contains this text")
    s.add_argument("--url", "--engine", dest="url", metavar="URL",
                   help="the engine (default: VK_ENGINE_URL or http://localhost:7860); the folder runs as it is on disk")
    s.add_argument("--format", choices=["text", "json"], default="text")
    s.add_argument("-v", "--verbose", action="store_true", help="print transcripts of passing tests too")
    s.add_argument("--live-tools", action="store_true",
                   help="call tools' real services instead of their mock responses")
    s.set_defaults(func=cmd_test)

    templates = sub.add_parser("templates", help="starter agents")
    tsub = templates.add_subparsers(dest="templates_command", required=True)
    s = tsub.add_parser("list", help="list bundled starter agents")
    s.set_defaults(func=cmd_templates_list)
    s = tsub.add_parser("install", help="copy a starter agent into a folder")
    s.add_argument("name")
    s.add_argument("--dir", help="target folder (default: agents/<template>)")
    s.add_argument("--name", dest="name_as", metavar="AGENT_NAME", help="name for the new agent")
    s.add_argument("--force", action="store_true", help="overwrite a non-empty folder")
    s.set_defaults(func=cmd_templates_install)

    s = sub.add_parser("import", help="convert a Dialogflow CX or CX Agent Studio export into an agent folder (offline)")
    s.add_argument("source", choices=["dfcx", "ces", "auto"], help="dfcx = Dialogflow CX JSON package, ces = CX Agent Studio app export")
    s.add_argument("export", help="the exported .zip, or the unzipped folder")
    s.add_argument("--dir", help="target folder (default: agents/<agent-name>)")
    s.add_argument("--name", help="name for the new agent")
    s.add_argument("--force", action="store_true", help="overwrite a non-empty folder")
    s.set_defaults(func=cmd_import)

    s = sub.add_parser("export", help="write an agent folder as a Dialogflow CX or CX Agent Studio import (offline)")
    s.add_argument("target", choices=["dfcx", "ces"], help="dfcx = Dialogflow CX JSON package (playbooks), ces = CX Agent Studio app")
    s.add_argument("dir", help="the agent folder")
    s.add_argument("--out", help="the .zip to write (default: <folder>-<target>.zip)")
    s.add_argument("--folder", action="store_true", help="write the files into a folder instead of a zip")
    s.add_argument("--time-zone", default="America/New_York", help="Dialogflow CX: the agent's time zone")
    s.add_argument("--language", default="en", help="Dialogflow CX: the agent's default language")
    s.add_argument("--force", action="store_true", help="overwrite")
    s.set_defaults(func=cmd_export)

    skills = sub.add_parser("skills", help="coding-agent skills")
    ssub = skills.add_subparsers(dest="skills_command", required=True)
    s = ssub.add_parser("install", help="copy vk's skills into this project")
    s.add_argument("--target", choices=sorted(SKILL_TARGETS), default="claude")
    s.add_argument("--dest", help="custom destination folder")
    s.set_defaults(func=cmd_skills_install)
    return p


def main(argv: list[str] | None = None, *, transport=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args._transport = transport          # tests: a fake HTTP transport for the engine client
    try:
        return args.func(args)
    except agentfs.FolderError as exc:
        _err(f"error: {exc}")
        return 1
    except ApiError as exc:
        _err(f"error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
