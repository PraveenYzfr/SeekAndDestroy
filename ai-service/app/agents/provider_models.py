"""What each provider will actually serve, asked at runtime.

NEVER HARDCODE A MODEL LIST
---------------------------
Model names in this estate have gone stale five times, and every failure looked
like something else:

    gemini-2.0-flash          404
    gemini-2.5-flash          "no longer available to new users" - reads exactly
                              like a bad API key
    deepseek-chat             retired, still in the vendor's own docs
    llama-3.1-8b-instant      retired by Groq within 5 days of being benchmarked
    llama-3.3-70b-versatile   retired in the same window

A dropdown of stale names is worse than no dropdown: the operator picks
something, the next investigation fails with a 404, and it reads as a broken
screen rather than a retired model.

So the list comes from the provider, and a provider that cannot be reached is
reported as unavailable *with its reason* rather than silently replaced by a
remembered list.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)

#: Long enough that opening the screen twice does not re-ask every provider,
#: short enough that a model retired this morning is gone by lunchtime.
_CACHE_TTL_SECONDS = 600

_cache: dict[str, tuple[float, dict]] = {}

#: Providers this platform can build a client for. `mock` is included on purpose
#: - it is a legitimate choice for a role while comparing behaviour without
#: spending anything, and leaving it out would mean the screen could not express
#: a configuration the factory supports.
def _listable() -> tuple[str, ...]:
    """Derived from the registry rather than restated.

    This was a hand-maintained tuple beside an if-chain that also named
    providers. Nothing kept the two in agreement, so a provider could be
    listable-but-unbuildable or the reverse, and neither the type system nor a
    test would notice.
    """
    from app.agents.providers import listable_providers

    return listable_providers()


LISTABLE = _listable()

#: Substrings that mean "not a chat model". Provider listings mix in embedding,
#: speech and moderation models that would 404 or return nonsense as a narration
#: model, and the operator has no way to tell from the name alone.
_NOT_CHAT = (
    "embed", "embedding", "whisper", "tts", "speech", "audio", "moderation",
    "guard", "rerank", "vision-encoder", "image", "dall-e", "sora",
)


def _is_chat_model(name: str) -> bool:
    lowered = name.lower()
    return not any(token in lowered for token in _NOT_CHAT)


def _fetch(provider: str, *, refresh: bool = False) -> dict:
    """Live model ids for one provider, or an entry saying why not.

    The per-provider knowledge - endpoint, auth header, how to read the response
    - moved to agents.providers, so this is now only the part that is the same
    for everyone: call it, and turn a failure into a reportable state rather
    than an exception. A provider that is down must show as unavailable WITH ITS
    REASON, because "no models" and "could not reach it" send an operator to
    different places.
    """
    from app.agents.providers import adapter_for

    settings = get_settings().llm
    try:
        adapter = adapter_for(provider)
    except ValueError as exc:
        return _unavailable(provider, str(exc))
    try:
        names = adapter.list_models(settings)
    except NotImplementedError as exc:
        return _unavailable(provider, str(exc))
    except Exception as exc:  # noqa: BLE001 - any failure is a reportable state
        return _unavailable(provider, f"{type(exc).__name__}: {exc}")
    # PROBED, NOT TRUSTED. A catalogue entry is not permission to call it: on the
    # live Gemini key, GET /models/gemini-2.5-pro returns 200 with
    # generateContent among its supportedGenerationMethods, and POST
    # ...:generateContent returns 404. The capability filter screened on that
    # flag and confidently offered a model that cannot be called - which is how
    # the judge role and its fallback were both set to 404s and produced zero
    # verdicts across four evaluation runs.
    #
    # usable_chat_models stays as the CHEAP pre-filter: it removes whisper, tts,
    # embeddings and retired families by name, so a real API call is not spent
    # probing a text-to-speech model. The probe then answers the only question
    # the catalogue cannot.
    from app.agents.providers import callable_models

    models = sorted(names)
    try:
        models, filtered = callable_models(adapter, settings, models, refresh=refresh)
    except Exception as exc:  # noqa: BLE001
        # A probe failure must not blank the dropdown. Fall back to the
        # catalogue and SAY SO, rather than presenting an unverified list as a
        # verified one.
        return {
            "provider": provider, "available": True, "models": models,
            "filtered_uncallable": 0, "callability_verified": False,
            "error": f"model list not verified: {type(exc).__name__}: {exc}",
        }
    return {
        "provider": provider, "available": True, "models": models,
        # Reported so a short list reads as a filtered one rather than an
        # outage. 1 of 29 with no explanation is indistinguishable from broken.
        "filtered_uncallable": filtered,
        "callability_verified": True,
        "error": None,
    }


def _unavailable(provider: str, reason: str) -> dict:
    return {"provider": provider, "available": False, "models": [], "error": reason}


def http_get(url: str, headers: dict | None = None) -> dict[str, Any]:
    with httpx.Client(timeout=15.0) as client:
        response = client.get(url, headers=headers or {})
        response.raise_for_status()
        return response.json()


def list_models(provider: str, *, refresh: bool = False) -> dict:
    now = time.time()
    if not refresh:
        cached = _cache.get(provider)
        if cached and now - cached[0] < _CACHE_TTL_SECONDS:
            return cached[1]
    result = _fetch(provider, refresh=refresh)
    # Only successes are cached. Caching a failure would keep a provider dark
    # for ten minutes after a transient blip or after the operator fixed the key.
    if result["available"]:
        _cache[provider] = (now, result)
    return result


def list_all(*, refresh: bool = False) -> list[dict]:
    return [list_models(p, refresh=refresh) for p in LISTABLE]


def is_known_model(provider: str, model: str) -> bool:
    """Whether the provider currently serves this model.

    Used to validate a write. Storing a name the provider does not serve turns
    a bad save into a failed investigation later, in a place with no obvious
    connection to the dropdown that caused it.
    """
    listing = list_models(provider)
    if not listing["available"]:
        # Cannot verify. Refusing here would make the screen unusable whenever a
        # provider is briefly unreachable, so this defers to the caller, which
        # says so in its response rather than pretending the name was checked.
        return True
    return model in listing["models"]


def reset_cache() -> None:
    _cache.clear()
