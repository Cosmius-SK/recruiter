"""Single factory for the chat model used by every agent.

The whole system talks to the model through LangChain's common interface
(``bind_tools`` + ``with_structured_output``), so the provider is a pure
configuration concern:

- **Gemini** (``GOOGLE_API_KEY``) — default for the testing phase; the free
  tier makes full lifecycle runs effectively free.
- **Claude** (``ANTHROPIC_API_KEY``) — production target.

Resolution order: explicit ``TALENTFLOW_PROVIDER`` env var, otherwise
whichever API key is present (Google wins if both are set). Keys are read
from the environment or a local ``.env`` file (gitignored) — never from code
or the repo.

Tests monkeypatch :func:`get_chat_model` to substitute a scripted model.
"""

from __future__ import annotations

import os

from dotenv import load_dotenv
from langchain_core.language_models.chat_models import BaseChatModel

from talentflow.config import get_settings

load_dotenv()  # pick up GOOGLE_API_KEY / ANTHROPIC_API_KEY from a local .env

DEFAULT_MODELS = {
    "gemini": "gemini-2.5-flash",
    "anthropic": "claude-opus-4-8",
}

_model: BaseChatModel | None = None


def resolve_provider() -> str:
    provider = get_settings().provider
    if provider != "auto":
        return provider
    if os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    raise RuntimeError(
        "No LLM credentials found. Set GOOGLE_API_KEY (Gemini, free tier — get one at "
        "https://aistudio.google.com/apikey) or ANTHROPIC_API_KEY (Claude), either in the "
        "environment or in a .env file. To force a provider, set TALENTFLOW_PROVIDER."
    )


def _build_model(provider: str) -> BaseChatModel:
    settings = get_settings()
    model_name = settings.model or DEFAULT_MODELS[provider]
    if provider == "gemini":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=model_name, max_output_tokens=settings.max_tokens)
    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        # Opus 4.7+ rejects sampling parameters (temperature/top_p/top_k),
        # so none are set here.
        return ChatAnthropic(model=model_name, max_tokens=settings.max_tokens)
    raise ValueError(f"Unknown provider '{provider}'")


def get_chat_model() -> BaseChatModel:
    global _model
    if _model is None:
        _model = _build_model(resolve_provider())
    return _model


def reset_model_cache() -> None:
    """Drop the cached model (used by tests and provider switches)."""
    global _model
    _model = None
