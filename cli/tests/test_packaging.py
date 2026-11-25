"""The root pyproject.toml is a shim so pre-commit's `language: python` can `pip
install .` from the repo root, while the real package lives in cli/. Its
dependencies and package-data must stay in step with cli/pyproject.toml."""

import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path) -> dict:
    return tomllib.loads(path.read_text())


def _package_data(project: dict) -> dict:
    return {k: sorted(v) for k, v in project["tool"]["setuptools"]["package-data"].items()}


def test_root_pyproject_matches_cli():
    root = _load(ROOT / "pyproject.toml")
    cli = _load(ROOT / "cli" / "pyproject.toml")
    assert sorted(root["project"]["dependencies"]) == sorted(cli["project"]["dependencies"])
    assert _package_data(root) == _package_data(cli)


def test_pre_commit_hook_is_strict_by_default():
    hooks = yaml.safe_load((ROOT / ".pre-commit-hooks.yaml").read_text())
    [hook] = [h for h in hooks if h["id"] == "vk-lint"]
    assert hook["args"] == ["agents", "--strict"]
