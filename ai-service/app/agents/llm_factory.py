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
from app.agents.http_chat_model import EmptyCompletionError, HttpChatModel
from app.agents.mock_llm import MockChatModel
from app.config import get_settings

logger = structlog.get_logger(__name__)

from app.agents.roles import fallback_role_name


def build_chat_model_for_provider(provider: str, model_override: str | None = None) -> BaseChatModel:
    """Build a client for ``provider``, via its registered adapter.

    ``model_override`` names a specific model, for per-role selection. Without it
    the configured SAD_LLM__MODEL is used, which is the behaviour every existing
    caller gets - the parameter is additive, so a role with no override resolves
    to exactly what this returned before roles existed.

    This was an eight-branch if-chain, and a second four-branch chain in
    provider_models had to agree with it. Both are now derived from
    agents.providers.REGISTRY, so a provider cannot be constructible and
    unlistable, and adding one changes no existing function.
    """
    from app.agents.providers import adapter_for

    settings = get_settings().llm
    # What metrics call this. Defaults to the provider name; SAD_LLM__PROVIDER_LABEL
    # distinguishes the OpenAI-compatible endpoints that all share provider="openai".
    label = settings.provider_label or provider
    return adapter_for(provider).build(settings, model_override or settings.model, label)


def build_chat_model() -> BaseChatModel:
    """The primary provider, with backups behind it.

    Each fallback is built with ITS OWN model. That was the bug: every member of
    the chain was built with settings.model, so an OpenAI fallback behind a
    DeepSeek primary requested "deepseek-v4-flash" and 404d. The backup was
    guaranteed to fail at the exact moment it was needed, and nothing caught it
    because the chain shipped empty.

    A fallback that cannot be built is SKIPPED rather than fatal - no credential
    for the backup provider must not take the primary offline too. A chain with
    nothing constructible degrades to the primary alone, which is where it
    started.
    """
    settings = get_settings().llm
    primary = build_chat_model_for_provider(settings.provider)

    # The Model Settings screen wins over configuration, exactly as it does for
    # every other role. Without this the fallback would be VISIBLE on that screen
    # and unchangeable by it, which is worse than not showing it at all.
    # Each leg gets ITS OWN model. A single fallback_model across a chain of
    # different providers is the same defect that made the first OpenAI backup
    # ask for "deepseek-v4-flash": a backup that 404s at the moment it is needed.
    chain = [
        (name, settings.fallback_model_for(name) or None)
        for name in settings.fallback_provider_list
    ]

    members = [(settings.provider, primary)]
    for name, model in chain:
        if name == settings.provider:
            continue  # a provider is not its own backup
        try:
            members.append((name, build_chat_model_for_provider(name, model)))
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_factory.fallback_unavailable", provider=name, error=str(exc))
    if len(members) == 1:
        return primary
    return FallbackChatModel(members=members)


#: Appended to the SYSTEM prompt when a model exhausts its output budget on
#: reasoning. The question was fine; what overran is the model's own thinking, so
#: rewriting the human turn would change what was asked.
_BREVITY_NUDGE = (
    "@@NL@@@@NL@@IMPORTANT: a previous attempt ran out of output budget while reasoning "
    "and returned nothing. Reason briefly. Do not restate the evidence, do not "
    "enumerate alternatives you have rejected, and produce the required output "
    "directly."
).replace("@@NL@@", chr(10))


def _with_brevity(messages: list[BaseMessage]) -> list[BaseMessage]:
    """The same messages, with the brevity instruction on the system turn.

    Falls back to prepending one if there is no system message, rather than
    silently dropping the instruction - a retry that changes nothing is a second
    identical failure and a doubled bill.
    """
    from langchain_core.messages import SystemMessage

    if messages and isinstance(messages[0], SystemMessage):
        return [SystemMessage(content=messages[0].content + _BREVITY_NUDGE), *messages[1:]]
    return [SystemMessage(content=_BREVITY_NUDGE.strip()), *messages]


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
            except EmptyCompletionError as exc:
                # RUNNING OUT OF BUDGET IS NOT A PROVIDER BEING UNAVAILABLE, and
                # treating it as one is what made this expensive.
                #
                # A reasoning model that spends its whole allowance thinking
                # returns 200 with empty content. This loop caught that like any
                # other failure and moved to the next provider - so one overflow
                # cost four sequential calls across four providers, and the
                # cheapest possible fix, asking THIS model to be brief, was never
                # tried. Measured on the 100-case golden run: 16 overflows,
                # 23,881 to 37,842 characters of reasoning against an 8,192 token
                # budget, every one falling through the entire chain.
                #
                # There IS a brevity retry in app.agents.structured. It never
                # fired once, because this loop is INSIDE the model that
                # structured calls: by the time anything escaped to it, the chain
                # had already exhausted every provider and raised a RuntimeError,
                # which is not the exception that retry watches for.
                #
                # So the retry belongs here, before the fall-through, and only for
                # this failure. One attempt, same provider, then continue.
                logger.warning("llm_factory.fallback.length_retry", provider=name, error=str(exc)[:200])
                try:
                    return model._generate(
                        _with_brevity(messages), stop=stop, run_manager=run_manager, **kwargs
                    )
                except Exception as retry_exc:  # noqa: BLE001
                    logger.warning(
                        "llm_factory.fallback.provider_failed",
                        provider=name, error=str(retry_exc)[:200],
                    )
                    if i < len(self.members) - 1:
                        llm_fallback_total.labels(from_provider=name).inc()
                    last_exc = retry_exc
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


def _is_judge_role(role_name: str) -> bool:
    """The judge role, and its fallback.

    Both, because a fallback that lands back on the primary model would restore
    exactly the self-judging this exists to prevent - and it would do it only
    when the judge's own provider was down, which is the moment nobody is
    looking at where verdicts came from.
    """
    from app.agents.roles import primary_role_name

    return primary_role_name(role_name) == "judge"


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

    # 3. The judge's own default, so it is not the author of what it grades.
    #
    #    BELOW the override on purpose: the admin screen still wins, because an
    #    operator who deliberately points the judge somewhere has made a choice
    #    and this must not quietly undo it. ABOVE the tier and base config,
    #    because those are what made every verdict self-judged - the judge
    #    inherited the same model as every other role and its output was
    #    discarded, silently, on every request.
    #
    #    Credentials are checked here rather than at call time. A judge pointed
    #    at a provider with no key would raise inside the grading path, and
    #    grading must never be able to break a delivered answer - so an
    #    unusable default falls through to the tier/config answer with a warning
    #    rather than producing a model nobody can call.
    if _is_judge_role(role_name) and settings.judge_provider and settings.judge_model:
        if settings.key_for(settings.judge_provider):
            return tiers.Resolution(
                role=role_name, tier=tier, provider=settings.judge_provider,
                model=settings.judge_model, source="judge_default",
            ).as_dict()
        logger.warning(
            "llm_factory.judge_default_unusable",
            provider=settings.judge_provider,
            detail="no credential for the configured judge provider; falling back. "
                   "The judge may now be the author of what it grades, and its "
                   "verdicts will be excluded as self-judged.",
        )

    # 4. Tier slot. Both halves must be set - a provider with no model would
    #    silently pair a new provider with the previous provider's model name.
    slot_provider = settings.cheap_provider if tier == tiers.CHEAP else settings.costly_provider
    slot_model = settings.cheap_model if tier == tiers.CHEAP else settings.costly_model
    if slot_provider and slot_model:
        return tiers.Resolution(
            role=role_name, tier=tier, provider=slot_provider, model=slot_model, source="tier",
        ).as_dict()

    # 5. Base configuration.
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


def _with_configured_fallbacks(primary_provider: str, primary: BaseChatModel) -> BaseChatModel:
    """Put the configured fallback chain behind an already-built primary.

    A leg that cannot be built is SKIPPED rather than fatal - no credential for
    a backup provider must not take the primary offline too. A chain with
    nothing constructible degrades to the primary alone, which is where it
    started.
    """
    settings = get_settings().llm
    members = [(primary_provider, primary)]
    for name in settings.fallback_provider_list:
        if name == primary_provider:
            continue  # a provider is not its own backup
        try:
            members.append(
                (name, build_chat_model_for_provider(name, settings.fallback_model_for(name)))
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("llm_factory.role_fallback_unavailable", provider=name, error=str(exc))
    if len(members) == 1:
        return primary
    return FallbackChatModel(members=members)


def get_chat_model_for_role(role_name: str) -> BaseChatModel:
    """The model configured for ``role_name``, with that role's own backup behind it.

    Falls back to the process-wide model whenever the role has no override, so an
    unconfigured platform behaves exactly as it did before roles existed - and
    that path already carries the estate-wide chain from
    SAD_LLM__FALLBACK_PROVIDERS.

    An OVERRIDDEN role used to have no backup at all. The reasoning was that the
    operator named one model and answering from another would make the audit log
    disagree with the screen. That is right about silence and wrong about the
    consequence: it meant configuring a role removed its resilience, so the more
    deliberately the platform was set up, the more fragile it became.

    Now the screen carries a second choice per role, and answering from it is
    something an operator picked rather than something the platform did quietly.
    The audit log records the provider that actually answered, so the two still
    agree.
    """
    resolved = resolve_role(role_name)
    if resolved["source"] in ("config", "tier"):
        return get_chat_model()

    primary = _model_for(resolved["provider"], resolved["model"])
    backup = resolve_role(fallback_role_name(role_name))
    if backup.get("source") != "override":
        # NO EXPLICIT BACKUP FOR THIS ROLE - fall back to the CONFIGURED CHAIN
        # rather than to nothing.
        #
        # This used to return `primary` alone, on the reasoning that a fallback
        # nobody selected is a model nobody evaluated. That was right about
        # silence and wrong about what it cost: choosing a model on the Model
        # Settings screen REMOVED that role's resilience, so the more
        # deliberately the platform was configured the more fragile it became -
        # and nothing on the screen said so.
        #
        # The chain is not unevaluated. SAD_LLM__FALLBACK_PROVIDERS is a
        # deliberate configuration - gemini flash-lite, then openai mini - and
        # every other role already answers from it. Giving an overridden role
        # the same chain is consistent rather than surprising.
        #
        # An explicit per-role fallback still wins, and the audit log records
        # the provider that actually answered, so the screen and the record
        # continue to agree.
        return _with_configured_fallbacks(resolved["provider"], primary)
    try:
        secondary = _model_for(backup["provider"], backup["model"])
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "llm_factory.role_fallback_unavailable", role=role_name, error=str(exc)
        )
        return primary
    return FallbackChatModel(members=[
        (resolved["provider"], primary), (backup["provider"], secondary),
    ])


def reset_role_model_cache() -> None:
    _model_for.cache_clear()
