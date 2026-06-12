"""Provider resolution for the model factory (no network calls)."""

import pytest

from talentflow import llm
from talentflow.config import get_settings


@pytest.fixture(autouse=True)
def clean_caches(monkeypatch):
    get_settings.cache_clear()
    llm.reset_model_cache()
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TALENTFLOW_PROVIDER", raising=False)
    monkeypatch.delenv("TALENTFLOW_MODEL", raising=False)
    yield
    get_settings.cache_clear()
    llm.reset_model_cache()


def test_auto_prefers_gemini_when_google_key_present(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert llm.resolve_provider() == "gemini"


def test_auto_falls_back_to_anthropic(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    assert llm.resolve_provider() == "anthropic"


def test_explicit_provider_overrides_keys(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    monkeypatch.setenv("TALENTFLOW_PROVIDER", "anthropic")
    assert llm.resolve_provider() == "anthropic"


def test_no_credentials_raises_helpful_error():
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        llm.resolve_provider()


def test_builds_gemini_model_with_default_name(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test-key")
    model = llm.get_chat_model()
    assert type(model).__name__ == "ChatGoogleGenerativeAI"
    assert llm.DEFAULT_MODELS["gemini"] in model.model


def test_builds_anthropic_model_with_custom_name(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setenv("TALENTFLOW_MODEL", "claude-sonnet-4-6")
    model = llm.get_chat_model()
    assert type(model).__name__ == "ChatAnthropic"
    assert model.model == "claude-sonnet-4-6"
