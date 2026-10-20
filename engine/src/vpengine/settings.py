"""Integration settings saved from the UI.

Stored as JSON outside the repository (default ~/.config/vpengine/settings.json,
or VPE_SETTINGS_FILE), readable only by you (mode 600). Keys are never sent
back to the browser: the UI only learns whether a key is set and its last four
characters.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx

from vk.tools import SECRET_NAME

from .services import LLM_BY_NAME, LLM_PROVIDERS, SPEC_BY_NAME, SPECS, Config, ConfigError, make_llm
from .tools import env_secrets


def default_path() -> Path:
    custom = os.environ.get("VPE_SETTINGS_FILE", "").strip()
    if custom:
        return Path(custom).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME", "").strip() or str(Path.home() / ".config")
    return Path(base) / "vpengine" / "settings.json"


class SettingsStore:
    def __init__(self, path: Path | None = None, env: dict | None = None):
        self.path = path or default_path()
        self.env = env

    def _raw(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            raise ConfigError(f"cannot read {self.path}: {exc}") from exc
        return data if isinstance(data, dict) else {}

    def read(self) -> dict[str, str]:
        return {k: str(v) for k, v in self._raw().items() if k in SPEC_BY_NAME and v not in (None, "")}

    # -- tool secrets ({{secrets.NAME}} in an agent's tools) ----------------------

    def saved_secrets(self) -> dict[str, str]:
        raw = self._raw().get("secrets") or {}
        return {str(k): str(v) for k, v in raw.items() if SECRET_NAME.match(str(k)) and v} if isinstance(raw, dict) else {}

    def secrets(self) -> dict[str, str]:
        """Environment (VPE_SECRET_NAME) first, then the ones saved here, which win."""
        return {**env_secrets(self.env), **self.saved_secrets()}

    def secrets_view(self) -> list[dict]:
        env, saved = env_secrets(self.env), self.saved_secrets()
        out = []
        for name in sorted(set(env) | set(saved)):
            value = saved.get(name, env.get(name, ""))
            out.append({"name": name, "source": "saved" if name in saved else "env",
                        "hint": f"…{value[-4:]}" if len(value) >= 12 else "set"})
        return out

    def set_secret(self, name: str, value: str | None) -> None:
        if not SECRET_NAME.match(name or ""):
            raise ConfigError("secret names use capital letters, digits and underscores, like SCHEDULING_KEY")
        value = (value or "").strip()
        if len(value) > 4000 or "\n" in value or "\r" in value:
            raise ConfigError("a secret is one line, up to 4000 characters")
        secrets = self.saved_secrets()
        if value:
            secrets[name] = value
        else:
            secrets.pop(name, None)
        self._write({**self.read(), **({"secrets": secrets} if secrets else {})})

    def config(self) -> Config:
        return Config.load(self.env, self.read())

    def update(self, changes: dict[str, Any]) -> Config:
        """Apply changes: a value sets it, "" or None removes the saved value (env/default applies again)."""
        unknown = set(changes) - set(SPEC_BY_NAME)
        if unknown:
            raise ConfigError(f"unknown setting(s): {', '.join(sorted(unknown))}")
        saved = self.read()
        before = Config.load(self.env, saved).llm
        if changes.get("llm") and changes["llm"] != before and "model" not in changes:
            saved.pop("model", None)            # a new provider starts on its own default model
        for name, value in changes.items():
            value = "" if value is None else str(value).strip()
            if len(value) > 2000:
                raise ConfigError(f"{SPEC_BY_NAME[name].label} is too long")
            if value:
                saved[name] = value
            else:
                saved.pop(name, None)
        cfg = Config.load(self.env, saved)          # validate before writing
        secrets = self.saved_secrets()
        self._write({**saved, **({"secrets": secrets} if secrets else {})})
        return cfg

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o700)
        except OSError:
            pass
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def public_view(cfg: Config, path: Path) -> dict:
    """What the Integrations page may see: values, but never keys."""
    out: dict[str, Any] = {"file": str(path), "fields": {}, "status": cfg.describe(),
                           "llm_providers": [{"name": p.name, "label": p.label, "tag": p.tag, "blurb": p.blurb,
                                              "model": p.model, "key": p.key, "fields": list(p.fields),
                                              "key_hint": p.key_hint} for p in LLM_PROVIDERS]}
    values = cfg.values()
    for spec in SPECS:
        value = values.get(spec.name)
        entry: dict[str, Any] = {"label": spec.label, "source": cfg.sources.get(spec.name, "default"),
                                 "env": spec.env}
        if spec.choices:
            entry["choices"] = list(spec.choices)
        if spec.secret:
            entry["set"] = bool(value)
            entry["hint"] = f"…{value[-4:]}" if value and len(value) >= 12 else ("set" if value else "")
        else:
            entry["value"] = "" if value is None else value
        out["fields"][spec.name] = entry
    return out


# -- "Test connection" ------------------------------------------------------------

async def check(service: str, cfg: Config, timeout: float = 10.0) -> dict:
    """Make one cheap, read-only call to see whether a service accepts our settings.

    service: "llm", "stt" or "tts" (whichever provider is selected), or a provider name.
    """
    target = {"llm": "llm", "stt": cfg.stt, "tts": cfg.tts}.get(service, service)
    try:
        async with httpx.AsyncClient(timeout=timeout) as http:
            if target == "llm":
                checked = await _check_llm(cfg, http)
                if isinstance(checked, dict):
                    return checked
                r = checked
            elif target == "deepgram":
                if not cfg.deepgram_key:
                    return {"ok": False, "message": "no API key saved"}
                r = await http.get("https://api.deepgram.com/v1/projects",
                                   headers={"Authorization": f"Token {cfg.deepgram_key}"})
            elif target == "fish":      # same key for listening and speaking
                if not cfg.fish_key:
                    return {"ok": False, "message": "no API key saved"}
                r = await http.get("https://api.fish.audio/model", params={"page_size": 1, "self": "true"},
                                   headers={"Authorization": f"Bearer {cfg.fish_key}"})
            elif target == "whisper":
                from .builtin_speech import whisper_ready
                ok = whisper_ready(cfg.local_whisper_model)
                return {"ok": ok, "message": "model downloaded" if ok else "download the models first"}
            elif target == "moonshine":
                from . import moonshine_stt
                if not moonshine_stt.installed():
                    return {"ok": False, "message": "not installed on this computer (needs Linux or an Apple-silicon Mac)"}
                ok = moonshine_stt.ready(cfg.moonshine_model)
                return {"ok": ok, "message": "model downloaded" if ok else "download the model first"}
            elif target == "kokoro":
                from .builtin_speech import kokoro_ready
                ok = kokoro_ready()
                return {"ok": ok, "message": "model downloaded" if ok else "download the models first"}
            elif target == "whisper-server":
                r = await http.get(cfg.whisper_url.rstrip("/") + "/models")
            elif target == "kokoro-server":
                r = await http.get(cfg.kokoro_url.rstrip("/") + "/audio/voices")
            else:
                return {"ok": False, "message": f"nothing to test for {service!r}"}
    except httpx.HTTPError as exc:
        return {"ok": False, "message": f"could not connect ({exc.__class__.__name__})"}
    if r.status_code in (401, 403):
        return {"ok": False, "message": "the key was rejected"}
    if r.status_code >= 400:
        return {"ok": False, "message": f"the service answered {r.status_code}"}
    return {"ok": True, "message": "connected"}


async def _check_llm(cfg: Config, http: httpx.AsyncClient):
    """One read-only request to the model provider: a response, or a result dict when there's nothing to send."""
    p = LLM_BY_NAME[cfg.llm]
    if cfg.llm == "anthropic":
        if not cfg.anthropic_key:
            return {"ok": False, "message": "no API key saved"}
        return await http.get("https://api.anthropic.com/v1/models",
                              headers={"x-api-key": cfg.anthropic_key, "anthropic-version": "2023-06-01"})
    if cfg.llm == "google":
        if not cfg.gemini_key:
            return {"ok": False, "message": "no API key saved"}
        return await http.get("https://generativelanguage.googleapis.com/v1beta/models",
                              params={"pageSize": 1}, headers={"x-goog-api-key": cfg.gemini_key})
    if cfg.llm == "azure":
        if not (cfg.azure_key and cfg.azure_endpoint):
            return {"ok": False, "message": "save the API key and endpoint first"}
        return await http.get(cfg.azure_endpoint.rstrip("/") + "/openai/v1/models", headers={"api-key": cfg.azure_key})
    if cfg.llm in ("vertex", "bedrock"):
        try:
            make_llm(cfg)
        except Exception as exc:  # noqa: BLE001 - shown in the UI
            return {"ok": False, "message": str(exc)}
        return {"ok": True, "message": "settings accepted; place a test chat to be sure the model answers"}
    if cfg.llm == "openai":
        base = (cfg.base_url or "https://api.openai.com/v1").rstrip("/")
        key = cfg.openai_key
    elif cfg.llm == "ollama":
        base, key = cfg.ollama_url.rstrip("/"), ""
    else:
        base, key = p.base_url.rstrip("/"), getattr(cfg, p.key)
        if not key:
            return {"ok": False, "message": "no API key saved"}
    return await http.get(f"{base}/models", headers={"Authorization": f"Bearer {key}"} if key else {})


async def local_speech_problem(cfg: Config, timeout: float = 1.5) -> str | None:
    """Before a call: if listening or speaking runs on a local server, make sure it's up."""
    from .builtin_speech import kokoro_ready, whisper_ready
    if cfg.stt == "whisper" and not whisper_ready(cfg.local_whisper_model) or cfg.tts == "kokoro" and not kokoro_ready():
        return "The built-in speech models aren't downloaded yet. Press \"Download models\" on the Integrations page."
    if cfg.stt == "moonshine":
        from . import moonshine_stt
        if not moonshine_stt.ready(cfg.moonshine_model):
            return "The Moonshine model isn't downloaded yet. Open Integrations → Moonshine and press Download."
    servers = []
    if cfg.stt == "whisper-server":
        servers.append(("Whisper", cfg.whisper_url.rstrip("/") + "/models"))
    if cfg.tts == "kokoro-server":
        servers.append(("Kokoro", cfg.kokoro_url.rstrip("/") + "/audio/voices"))
    async with httpx.AsyncClient(timeout=timeout) as http:
        for name, url in servers:
            try:
                await http.get(url)
            except httpx.HTTPError:
                return (f"{name} isn't running at {url.rsplit('/', 2)[0]}. Start it with "
                        f"\"Start local speech\" on the Integrations page, or pick another provider there.")
    return None
