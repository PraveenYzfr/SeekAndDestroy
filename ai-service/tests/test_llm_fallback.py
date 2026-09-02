"""Multi-LLM support: SAD_LLM__FALLBACK_PROVIDERS is an ordered list of
backup providers tried in sequence if the primary raises. See
app.agents.llm_factory.FallbackChatModel.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.messages import AIMessage

from app.agents.llm_factory import FallbackChatModel, build_chat_model
from app.agents.mock_llm import MockChatModel
from app.config import get_settings


class _BoomModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "boom"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        raise RuntimeError("simulated provider outage")


class _EchoModel(BaseChatModel):
    @property
    def _llm_type(self) -> str:
        return "echo"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="ok"))])


def test_fallback_model_uses_primary_when_it_succeeds():
    fb = FallbackChatModel(members=[("primary", _EchoModel()), ("secondary", _BoomModel())])
    result = fb.invoke([HumanMessage(content="hi")])
    assert result.content == "ok"


def test_fallback_model_cascades_to_secondary_on_primary_failure():
    fb = FallbackChatModel(members=[("primary", _BoomModel()), ("secondary", MockChatModel())])
    result = fb.invoke([HumanMessage(content="hi")])
    assert result.content  # mock's deterministic narration text, non-empty


def test_fallback_model_raises_once_every_provider_is_exhausted():
    fb = FallbackChatModel(members=[("primary", _BoomModel()), ("secondary", _BoomModel())])
    with pytest.raises(RuntimeError, match="all LLM providers failed"):
        fb.invoke([HumanMessage(content="hi")])


def test_build_chat_model_returns_plain_model_with_no_fallback_configured(monkeypatch):
    # Order matters, and it only started mattering when the default stopped
    # being empty: cache_clear() discards the object that was just patched and
    # rebuilds it from configuration, so patching first undid the patch. Clear,
    # then patch the instance the factory will actually read.
    get_settings.cache_clear()
    monkeypatch.setenv("SAD_LLM__FALLBACK_PROVIDERS", "")
    monkeypatch.setattr(get_settings().llm, "fallback_providers", "")
    model = build_chat_model()
    assert not isinstance(model, FallbackChatModel)
    get_settings.cache_clear()


def test_fallback_provider_list_parses_comma_separated_env_value():
    settings = get_settings().llm
    original = settings.fallback_providers
    try:
        settings.fallback_providers = " azure-openai , ollama ,, "
        assert settings.fallback_provider_list == ["azure-openai", "ollama"]
    finally:
        settings.fallback_providers = original
