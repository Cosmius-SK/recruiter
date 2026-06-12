"""Single factory for the Claude chat model used by every agent.

Centralised so the whole system can be pointed at a different model via
configuration, and so tests can monkeypatch one function to substitute a
scripted stand-in for the LLM.
"""

from langchain_anthropic import ChatAnthropic

from talentflow.config import get_settings

_model: ChatAnthropic | None = None


def get_chat_model() -> ChatAnthropic:
    global _model
    if _model is None:
        settings = get_settings()
        # Opus 4.7+ rejects sampling parameters (temperature/top_p/top_k),
        # so none are set here.
        _model = ChatAnthropic(model=settings.model, max_tokens=settings.max_tokens)
    return _model
