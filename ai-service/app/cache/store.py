"""Cache store abstraction: one interface, two backends - same pattern as
``app.retrieval.vector_store``. ``InMemoryCacheStore`` is the default
(no server assumed to be running); ``RedisCacheStore`` talks to a real Redis
instance when ``SAD_CACHE__BACKEND=redis``, so the cache survives process
restarts and is shared across multiple ai-service workers.

This only ever caches LLM *narration* output (see app.agents.structured) -
never the deterministic capacity/eligibility/scoring numbers, which are
recomputed fresh on every call per the trust-boundary rule enforced
throughout app.agents.
"""

from __future__ import annotations

import time
from functools import lru_cache
from typing import Protocol

import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)


class CacheStore(Protocol):
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def clear(self) -> None: ...
    def count(self) -> int: ...


class InMemoryCacheStore:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}
        self._expires_at: dict[str, float] = {}

    def _evict_if_expired(self, key: str) -> None:
        expires_at = self._expires_at.get(key)
        if expires_at is not None and expires_at <= time.time():
            self._values.pop(key, None)
            self._expires_at.pop(key, None)

    def get(self, key: str) -> str | None:
        self._evict_if_expired(key)
        return self._values.get(key)

    def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        self._values[key] = value
        if ttl_seconds is not None:
            self._expires_at[key] = time.time() + ttl_seconds
        else:
            self._expires_at.pop(key, None)

    def delete(self, key: str) -> None:
        self._values.pop(key, None)
        self._expires_at.pop(key, None)

    def clear(self) -> None:
        self._values.clear()
        self._expires_at.clear()

    def count(self) -> int:
        return len(self._values)


class RedisCacheStore:
    def __init__(self, url: str):
        import redis

        self._client = redis.Redis.from_url(url, decode_responses=True)
        self._client.ping()

    def get(self, key: str) -> str | None:
        return self._client.get(key)

    def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        self._client.set(key, value, ex=ttl_seconds)

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def clear(self) -> None:
        self._client.flushdb()

    def count(self) -> int:
        return self._client.dbsize()


def build_cache_store() -> CacheStore:
    settings = get_settings().cache
    if settings.backend == "redis":
        try:
            return RedisCacheStore(settings.redis_url)
        except Exception as exc:
            # Redis unreachable (e.g. no server running locally) - fall back
            # to the in-memory backend rather than crashing the whole service.
            logger.warning("cache.redis_unavailable_falling_back_to_memory", error=str(exc))
    return InMemoryCacheStore()


@lru_cache(maxsize=1)
def get_cache_store() -> CacheStore:
    return build_cache_store()


def reset_cache_store_cache() -> None:
    get_cache_store.cache_clear()
