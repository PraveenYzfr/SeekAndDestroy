"""LLM provider abstraction. Selection is entirely config-driven
(``SAD_LLM__PROVIDER`` plus an optional ``SAD_LLM__FALLBACK_PROVIDERS`` ordered
list); every chain in app/agents talks to ``BaseChatModel`` and never imports a
provider-specific class directly.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

import structlog
from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.outputs import ChatResult

from app.agents.gemini_chat_model import (
    DEFAULT_BASE_URL as GEMINI_DEFAULT_BASE_URL,
    DEFAULT_MODEL as GEMINI_DEFAULT_MODEL,
    GeminiChatModel,
)
from app.agents.http_chat_model import HttpChatModel
from app.agents.mock_llm import MockChatModel
from app.config import get_settings

logger = structlog.get_logger(__name__)


def build_chat_model_for_provider(provider: str) -> BaseChatModel:
    settings = get_settings().llm
    if provider == "mock":
        return MockChatModel()
    if provider == "openai":
        return HttpChatModel(
            base_url=settings.base_url or "https://api.openai.com/v1",
            model=settings.model, api_key=settings.api_key,
            temperature=settings.temperature, max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds, provider_name=provider,
        )
    if provider == "azure-openai":
        if not settings.azure_endpoint or not settings.azure_deployment:
            raise ValueError("SAD_LLM__AZURE_ENDPOINT and SAD_LLM__AZURE_DEPLOYMENT are required for azure-openai")
        base_url = (
            f"{settings.azure_endpoint.rstrip('/')}/openai/deployments/{settings.azure_deployment}"
        )
        return HttpChatModel(
            base_url=base_url, model=settings.model, api_key=settings.api_key,
            temperature=settings.temperature, max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds,
            extra_headers={"api-key": settings.api_key} if settings.api_key else {}, provider_name=provider,
        )
    if provider == "gemini":
        if not settings.api_key:
            raise ValueError("SAD_LLM__API_KEY is required for the gemini provider")
        return GeminiChatModel(
            api_key=settings.api_key,
            # Falls back to a Gemini chat model rather than settings.model's
            # default, which names the mock. Pointing the Gemini endpoint at
            # "seek-and-destroy-mock" would 404 in a way that reads like an
            # auth problem.
            model=settings.model if settings.model != "seek-and-destroy-mock" else GEMINI_DEFAULT_MODEL,
            base_url=settings.base_url or GEMINI_DEFAULT_BASE_URL,
            temperature=settings.temperature, max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds, provider_name=provider,
        )
    if provider == "deepseek":
        # No new client needed - DeepSeek serves the OpenAI chat-completions
        # wire format, so HttpChatModel works unchanged. Same reason `ollama`
        # is a base_url variant rather than its own class, and the opposite of
        # Gemini, which needed one because generateContent is a different shape.
        #
        # Roughly an order of magnitude cheaper than frontier models. The
        # trade-off worth knowing: no server-side schema enforcement, so
        # structured output falls back to prompt instructions plus a repair
        # retry (app/agents/structured.py) rather than the guarantee Gemini's
        # responseSchema gives.
        if not settings.api_key:
            raise ValueError("SAD_LLM__API_KEY is required for the deepseek provider")
        return HttpChatModel(
            base_url=settings.base_url or "https://api.deepseek.com/v1",
            model=settings.model if settings.model != "seek-and-destroy-mock" else "deepseek-chat",
            api_key=settings.api_key,
            temperature=settings.temperature, max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds, provider_name=provider,
        )
    if provider == "ollama":
        return HttpChatModel(
            base_url=settings.ollama_base_url.rstrip("/") + "/v1", model=settings.model,
            api_key="ollama", temperature=settings.temperature, max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds, provider_name=provider,
        )
    raise ValueError(f"unknown SAD_LLM__PROVIDER: {provider}")


def build_chat_model() -> BaseChatModel:
    settings = get_settings().llm
    fallbacks = settings.fallback_provider_list
    if not fallbacks:
        return build_chat_model_for_provider(settings.provider)
    providers = [settings.provider, *fallbacks]
    models = [(p, build_chat_model_for_provider(p)) for p in providers]
    return FallbackChatModel(members=models)


class FallbackChatModel(BaseChatModel):
    """Tries each ``(provider_name, BaseChatModel)`` pair in order, moving to
    the next on any exception. This is what ``SAD_LLM__FALLBACK_PROVIDERS``
    wires up - a primary provider plus an ordered list of backups, so a
    single down API doesn't take the whole platform's narration offline.
    """

    members: list[tuple[str, BaseChatModel]]

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "seekanddestroy-fallback"

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        from app.observability.metrics import llm_fallback_total

        last_exc: Exception | None = None
        for i, (name, model) in enumerate(self.members):
            try:
                return model._generate(messages, stop=stop, run_manager=run_manager, **kwargs)
            except Exception as exc:  # noqa: BLE001 - must try the next provider regardless of failure cause
                logger.warning("llm_factory.fallback.provider_failed", provider=name, error=str(exc))
                if i < len(self.members) - 1:
                    llm_fallback_total.labels(from_provider=name).inc()
                last_exc = exc
        raise RuntimeError(
            f"all LLM providers failed: {[n for n, _ in self.members]}"
        ) from last_exc


@lru_cache(maxsize=1)
def get_chat_model() -> BaseChatModel:
    return build_chat_model()


def reset_chat_model_cache() -> None:
    get_chat_model.cache_clear()
