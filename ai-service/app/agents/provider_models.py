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
LISTABLE = ("mock", "deepseek", "groq", "anthropic", "openai", "gemini", "ollama")

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


def _fetch(provider: str) -> dict:
    settings = get_settings().llm

    if provider == "mock":
        return {"provider": "mock", "available": True, "models": ["seek-and-destroy-mock"], "error": None}

    try:
        if provider == "gemini":
            if not settings.api_key:
                return _unavailable(provider, "SAD_LLM__API_KEY is not set")
            base = (settings.base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
            data = _get(f"{base}/models?key={settings.api_key}")
            names = [
                m["name"].split("/", 1)[-1]
                for m in data.get("models", [])
                # Gemini lists every model with the methods it supports; a model
                # that cannot generateContent cannot narrate, whatever its name
                # suggests.
                if "generateContent" in (m.get("supportedGenerationMethods") or [])
            ]
        elif provider == "anthropic":
            # GET /v1/models, but authenticated the Anthropic way - x-api-key and
            # the version header, not a Bearer token. Listing rather than typing
            # matters most here: Claude ids carry dates and are retired on a
            # published schedule, so a name that worked last quarter is exactly
            # the kind of plausible-looking guess that has already bitten us.
            key = settings.key_for("anthropic")
            if not key:
                return _unavailable(provider, "No credential: set SAD_LLM__PROVIDER_KEYS__ANTHROPIC")
            base = (settings.base_url or "https://api.anthropic.com/v1").rstrip("/")
            data = _get(
                f"{base}/models",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            )
            names = [m["id"] for m in data.get("data", [])]
        elif provider == "ollama":
            base = settings.ollama_base_url.rstrip("/")
            data = _get(f"{base}/v1/models", headers={"Authorization": "Bearer ollama"})
            names = [m["id"] for m in data.get("data", [])]
        else:
            # OpenAI-compatible: OpenAI itself, DeepSeek, and anything else
            # serving /v1/models.
            default_base = {
                "openai": "https://api.openai.com/v1",
                "deepseek": "https://api.deepseek.com/v1",
                "groq": "https://api.groq.com/openai/v1",
            }[provider]
            key = settings.key_for(provider)
            if not key:
                return _unavailable(
                    provider,
                    f"No credential: set SAD_LLM__PROVIDER_KEYS__{provider.upper()}",
                )
            data = _get(f"{(settings.base_url or default_base).rstrip('/')}/models",
                        headers={"Authorization": f"Bearer {settings.api_key}"})
            names = [m["id"] for m in data.get("data", [])]
    except Exception as exc:  # noqa: BLE001
        # Reported, not raised. One provider being down must not empty the whole
        # screen - the operator can still see and change the others.
        logger.warning("provider_models.list_failed", provider=provider, error=str(exc))
        return _unavailable(provider, str(exc)[:200])

    chat = sorted({n for n in names if _is_chat_model(n)})
    return {"provider": provider, "available": True, "models": chat, "error": None}


def _unavailable(provider: str, reason: str) -> dict:
    return {"provider": provider, "available": False, "models": [], "error": reason}


def _get(url: str, headers: dict | None = None) -> dict[str, Any]:
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
    result = _fetch(provider)
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
