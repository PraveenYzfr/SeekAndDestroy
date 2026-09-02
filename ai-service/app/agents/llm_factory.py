"""LLM provider abstraction. Selection is entirely config-driven
(``SAD_LLM__PROVIDER`` plus an optional ``SAD_LLM__FALLBACK_PROVIDERS`` ordered
list); every chain in app/agents talks to ``BaseChatModel`` and never imports a
provider-specific class directly.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Optional

import structlog
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
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


def build_chat_model_for_provider(provider: str, model_override: str | None = None) -> BaseChatModel:
    """Build a client for ``provider``.

    ``model_override`` names a specific model, for per-role selection. Without
    it the configured SAD_LLM__MODEL is used, which is the behaviour every
    existing caller gets - the parameter is additive, so a role with no override
    resolves to exactly what this function returned before roles existed.
    """
    settings = get_settings().llm
    # What metrics call this. Defaults to the provider name; SAD_LLM__PROVIDER_LABEL
    # distinguishes the OpenAI-compatible endpoints that all share provider="openai".
    label = settings.provider_label or provider
    if provider == "mock":
        return MockChatModel()
    if provider == "openai":
        return HttpChatModel(
            base_url=settings.base_url or "https://api.openai.com/v1",
            model=model_override or settings.model, api_key=settings.api_key,
            temperature=settings.temperature, max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds, provider_name=label,
        )
    if provider == "azure-openai":
        if not settings.azure_endpoint or not settings.azure_deployment:
            raise ValueError("SAD_LLM__AZURE_ENDPOINT and SAD_LLM__AZURE_DEPLOYMENT are required for azure-openai")
        base_url = (
            f"{settings.azure_endpoint.rstrip('/')}/openai/deployments/{settings.azure_deployment}"
        )
        return HttpChatModel(
            base_url=base_url, model=model_override or settings.model, api_key=settings.api_key,
            temperature=settings.temperature, max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds,
            extra_headers={"api-key": settings.api_key} if settings.api_key else {}, provider_name=label,
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
            model=model_override or (settings.model if settings.model != "seek-and-destroy-mock" else GEMINI_DEFAULT_MODEL),
            base_url=settings.base_url or GEMINI_DEFAULT_BASE_URL,
            temperature=settings.temperature, max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds, provider_name=label,
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
        key = settings.key_for("deepseek")
        if not key:
            raise ValueError(
                "No DeepSeek credential. Set SAD_LLM__PROVIDER_KEYS__DEEPSEEK, or "
                "SAD_LLM__API_KEY if DeepSeek is the only provider in use."
            )
        return HttpChatModel(
            base_url=settings.base_url or "https://api.deepseek.com/v1",
            # Verified against GET /models, not assumed - "deepseek-chat" was a
            # plausible-looking guess that this account does not serve at all.
            model=model_override or (settings.model if settings.model != "seek-and-destroy-mock" else "deepseek-v4-flash"),
            api_key=settings.api_key,
            temperature=settings.temperature, max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds, provider_name=label,
        )
    if provider == "anthropic":
        # Its own client, not a base_url variant. Anthropic is the second
        # provider here that is not OpenAI-compatible on the wire - different
        # endpoint, x-api-key instead of Bearer, a required anthropic-version
        # header, system as a top-level field rather than a message, and content
        # returned as a list of typed blocks. Gemini needed a client for the same
        # reason; groq, deepseek and ollama did not.
        from app.agents.anthropic_chat_model import AnthropicChatModel

        key = settings.key_for("anthropic")
        if not key:
            raise ValueError(
                "No Anthropic credential. Set SAD_LLM__PROVIDER_KEYS__ANTHROPIC, or "
                "SAD_LLM__API_KEY if Anthropic is the only provider in use."
            )
        return AnthropicChatModel(
            api_key=key,
            # No default model. Claude model ids carry dates and are retired on a
            # published schedule, so a hardcoded one is a guess with an expiry -
            # the same mistake as "deepseek-chat", which came from a vendor's own
            # docs and is not served. The admin screen enumerates live ids.
            model=model_override or settings.model,
            base_url=settings.base_url or "https://api.anthropic.com/v1",
            temperature=settings.temperature,
            # Required by this API rather than optional, so the settings default
            # is load-bearing here in a way it is not for the others.
            max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds, provider_name=label,
        )
    if provider == "groq":
        # OpenAI-compatible, so no new client - the same reasoning as deepseek and
        # ollama. Groq's value here is latency rather than price: it serves small
        # models on custom silicon, which is what makes it a candidate for the
        # extraction and planning roles that are currently paying reasoning-model
        # time for schema-filling work.
        key = settings.key_for("groq")
        if not key:
            raise ValueError(
                "No Groq credential. Set SAD_LLM__PROVIDER_KEYS__GROQ, or "
                "SAD_LLM__API_KEY if Groq is the only provider in use."
            )
        return HttpChatModel(
            base_url=settings.base_url or "https://api.groq.com/openai/v1",
            # No default model name. Groq retires and renames models often, and
            # "deepseek-chat" - a plausible guess straight from a vendor's own
            # docs - turned out not to be served at all. The admin screen
            # enumerates live ids from GET /models; this raises rather than
            # guessing one.
            model=model_override or settings.model,
            api_key=key,
            temperature=settings.temperature, max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds, provider_name=label,
        )
    if provider == "ollama":
        return HttpChatModel(
            base_url=settings.ollama_base_url.rstrip("/") + "/v1", model=model_override or settings.model,
            api_key="ollama", temperature=settings.temperature, max_tokens=settings.max_output_tokens,
            timeout_seconds=settings.timeout_seconds, provider_name=label,
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

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: Optional[list[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        """Same ordered walk as _generate, awaiting each member.

        Note it calls _agenerate directly rather than ainvoke: ainvoke wraps
        the result in callbacks and would double-count this chain's members in
        the metrics, and a member without _agenerate would silently be run in
        a thread pool instead of failing over.
        """
        from app.observability.metrics import llm_fallback_total

        last_exc: Exception | None = None
        for i, (name, model) in enumerate(self.members):
            try:
                return await model._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)
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


# =============================================================================
# Per-role model selection
# =============================================================================


def resolve_role(role_name: str, overrides: dict | None = None) -> dict:
    """Which provider and model a role runs on, and which layer decided.

    Walks the four layers in app/agents/tiers.py, narrowest first: force-single,
    then the per-role override, then the tier slot, then base config. The layer
    that answered is returned as ``source`` - "which model" without "why" leaves
    an operator unable to tell an override from a default that happens to match.

    ``overrides`` lets several roles resolve against one snapshot of the table.
    Querying per role could straddle an edit and give one investigation two
    different configurations.
    """
    from app.agents import tiers
    from app.repositories import llm_role_repository

    settings = get_settings().llm
    tier = tiers.tier_for(role_name, settings.role_tier_map)

    # 1. Escape hatch. Outranks the admin screen on purpose: recovering from a
    #    provider outage must not require discovering who overrode what.
    if settings.force_single:
        return tiers.Resolution(
            role=role_name, tier=tier, provider=settings.force_single,
            model=settings.model, source="force_single",
        ).as_dict()

    # 2. Per-role override, from the admin screen.
    if overrides is None:
        try:
            overrides = llm_role_repository.as_map()
        except Exception as exc:  # noqa: BLE001
            # The overrides table being unreachable must not take narration
            # offline. The tier or config answer is still a correct one.
            logger.warning("llm_factory.role_overrides_unavailable", error=str(exc))
            overrides = {}
    row = overrides.get(role_name)
    if row:
        return tiers.Resolution(
            role=role_name, tier=tier, provider=row["Provider"], model=row["Model"],
            source="override", updated_by=row.get("UpdatedBy"), updated_at=row.get("UpdatedAt"),
        ).as_dict()

    # 3. Tier slot. Both halves must be set - a provider with no model would
    #    silently pair a new provider with the previous provider's model name.
    slot_provider = settings.cheap_provider if tier == tiers.CHEAP else settings.costly_provider
    slot_model = settings.cheap_model if tier == tiers.CHEAP else settings.costly_model
    if slot_provider and slot_model:
        return tiers.Resolution(
            role=role_name, tier=tier, provider=slot_provider, model=slot_model, source="tier",
        ).as_dict()

    # 4. Base configuration.
    return tiers.Resolution(
        role=role_name, tier=tier, provider=settings.provider, model=settings.model, source="config",
    ).as_dict()


def resolve_all_roles() -> list[dict]:
    """Every role's effective model, from one snapshot."""
    from app.agents.roles import ROLES
    from app.repositories import llm_role_repository

    try:
        overrides = llm_role_repository.as_map()
    except Exception as exc:  # noqa: BLE001
        logger.warning("llm_factory.role_overrides_unavailable", error=str(exc))
        overrides = {}
    return [resolve_role(role.name, overrides) for role in ROLES]


@lru_cache(maxsize=16)
def _model_for(provider: str, model: str) -> BaseChatModel:
    """Cached per (provider, model) rather than per role.

    Two roles pointed at the same model share one client, which is what the
    single-model default does today - so turning roles on costs nothing until
    somebody actually differentiates them. Bounded at 16 because the cache key
    is operator-controlled and an unbounded cache keyed on user input is a slow
    leak.
    """
    return build_chat_model_for_provider(provider, model_override=model)


def get_chat_model_for_role(role_name: str) -> BaseChatModel:
    """The model configured for ``role_name``.

    Falls back to the process-wide model whenever the role has no override, so
    an unconfigured platform behaves exactly as it did before roles existed.
    """
    resolved = resolve_role(role_name)
    if resolved["source"] == "config":
        # The configured default keeps the fallback chain from
        # SAD_LLM__FALLBACK_PROVIDERS. An overridden role deliberately does not:
        # the operator named one model, and silently answering from a different
        # provider would make the audit log disagree with the screen.
        return get_chat_model()
    return _model_for(resolved["provider"], resolved["model"])


def reset_role_model_cache() -> None:
    _model_for.cache_clear()
