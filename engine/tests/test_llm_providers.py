"""Every model provider builds through Pipecat, and switching providers switches models."""

import pytest

from vpengine.services import LLM_PROVIDERS, SPEC_BY_NAME, Config, ConfigError, make_llm
from vpengine.settings import SettingsStore, check, public_view

KEYS = {"ANTHROPIC_API_KEY": "sk-ant-test", "OPENAI_API_KEY": "sk-test", "GEMINI_API_KEY": "g-test",
        "AZURE_OPENAI_API_KEY": "az-test", "AZURE_OPENAI_ENDPOINT": "https://r.openai.azure.com",
        "AWS_ACCESS_KEY_ID": "AKIATEST", "AWS_SECRET_ACCESS_KEY": "aws-test", "GROQ_API_KEY": "x",
        "MISTRAL_API_KEY": "x", "DEEPSEEK_API_KEY": "x", "XAI_API_KEY": "x", "TOGETHER_API_KEY": "x",
        "FIREWORKS_API_KEY": "x", "OPENROUTER_API_KEY": "x", "CEREBRAS_API_KEY": "x", "NVIDIA_API_KEY": "x",
        "DASHSCOPE_API_KEY": "x", "SAMBANOVA_API_KEY": "x", "NEBIUS_API_KEY": "x", "NOVITA_API_KEY": "x"}


@pytest.mark.parametrize("provider", [p for p in LLM_PROVIDERS if p.name != "vertex"], ids=lambda p: p.name)
def test_every_provider_builds_with_its_default_model(provider):
    cfg = Config.load({**KEYS, "VPE_LLM": provider.name})
    assert cfg.model == provider.model and cfg.llm_ready
    service = make_llm(cfg)
    assert service is not None
    if provider.cls:
        assert type(service).__name__ == provider.cls


def test_each_provider_names_real_settings():
    for p in LLM_PROVIDERS:
        for name in ([p.key] if p.key else []) + list(p.fields):
            assert name in SPEC_BY_NAME, (p.name, name)
        if p.key:
            assert SPEC_BY_NAME[p.key].secret, p.key


def test_missing_key_is_explained():
    with pytest.raises(ConfigError, match="no Groq API key"):
        make_llm(Config.load({"VPE_LLM": "groq"}))
    assert not Config.load({"VPE_LLM": "mistral"}).llm_ready


def test_vertex_needs_a_project_and_explains_credentials():
    assert not Config.load({"VPE_LLM": "vertex"}).llm_ready
    with pytest.raises(ConfigError, match="project"):
        make_llm(Config.load({"VPE_LLM": "vertex"}))
    with pytest.raises(ConfigError, match="service account"):
        Config.load({"VPE_LLM": "vertex", "VPE_VERTEX_CREDENTIALS": '{"type": "user"}'})


def test_bad_addresses_are_refused():
    with pytest.raises(ConfigError, match="Ollama address"):
        Config.load({"VPE_LLM": "ollama", "OLLAMA_BASE_URL": "localhost:11434"})
    with pytest.raises(ConfigError, match="Endpoint"):
        Config.load({"AZURE_OPENAI_ENDPOINT": "my-resource"})


def test_switching_provider_drops_the_old_model(tmp_path):
    store = SettingsStore(tmp_path / "settings.json", env={})
    store.update({"llm": "anthropic", "model": "claude-sonnet-5-5"})
    cfg = store.update({"llm": "groq"})
    assert cfg.model == "openai/gpt-oss-120b"
    cfg = store.update({"llm": "groq", "model": "llama-3.3-70b-versatile"})
    assert cfg.model == "llama-3.3-70b-versatile"
    assert store.update({"temperature": "0.3"}).model == "llama-3.3-70b-versatile"   # same provider keeps it


def test_settings_page_lists_providers_without_keys(tmp_path):
    cfg = Config.load({**KEYS, "VPE_LLM": "groq"})
    view = public_view(cfg, tmp_path / "s.json")
    names = [p["name"] for p in view["llm_providers"]]
    assert names[:2] == ["anthropic", "openai"] and "groq" in names and "bedrock" in names
    assert view["status"]["llm_label"] == "Groq"
    assert view["fields"]["groq_key"]["set"] and "value" not in view["fields"]["groq_key"]


async def test_check_asks_the_providers_model_list(monkeypatch):
    import httpx

    seen = []

    async def fake_get(self, url, **kw):
        seen.append((url, kw.get("headers")))
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    assert (await check("llm", Config.load({**KEYS, "VPE_LLM": "groq"})))["ok"]
    assert seen[-1] == ("https://api.groq.com/openai/v1/models", {"Authorization": "Bearer x"})
    assert (await check("llm", Config.load({**KEYS, "VPE_LLM": "google"})))["ok"]
    assert seen[-1][0].startswith("https://generativelanguage.googleapis.com/") and seen[-1][1] == {"x-goog-api-key": "g-test"}
    assert (await check("llm", Config.load({"VPE_LLM": "ollama"})))["ok"]
    assert seen[-1] == ("http://localhost:11434/v1/models", {})
    assert not (await check("llm", Config.load({"VPE_LLM": "cerebras"})))["ok"]
