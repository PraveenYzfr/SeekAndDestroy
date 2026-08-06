"""Cache abstraction (memory | redis, SAD_CACHE__BACKEND) and its use for
caching LLM structured-output narration in app.agents.structured.
"""

from __future__ import annotations

import time

from app.agents.structured import run_structured
from app.cache.store import InMemoryCacheStore, RedisCacheStore, build_cache_store
from app.config import get_settings
from app.models.agent_contracts import GroundedAnswer


def test_in_memory_cache_store_roundtrips():
    store = InMemoryCacheStore()
    assert store.get("k") is None
    store.set("k", "v")
    assert store.get("k") == "v"
    assert store.count() == 1
    store.delete("k")
    assert store.get("k") is None


def test_in_memory_cache_store_respects_ttl():
    store = InMemoryCacheStore()
    store.set("k", "v", ttl_seconds=0)
    time.sleep(0.01)
    assert store.get("k") is None


def test_in_memory_cache_store_clear():
    store = InMemoryCacheStore()
    store.set("a", "1")
    store.set("b", "2")
    store.clear()
    assert store.count() == 0


def test_build_cache_store_falls_back_to_memory_when_redis_unreachable(monkeypatch):
    settings = get_settings().cache
    original_backend, original_url = settings.backend, settings.redis_url
    try:
        settings.backend = "redis"
        settings.redis_url = "redis://localhost:1/0"  # nothing listening there
        store = build_cache_store()
        assert isinstance(store, InMemoryCacheStore)
    finally:
        settings.backend, settings.redis_url = original_backend, original_url


def test_run_structured_caches_identical_prompts():
    from app.agents.mock_llm import MockChatModel

    llm = MockChatModel()
    calls = {"count": 0}
    original_invoke = MockChatModel._generate

    def counting_generate(self, *args, **kwargs):
        calls["count"] += 1
        return original_invoke(self, *args, **kwargs)

    import app.agents.mock_llm as mock_llm_module

    mock_llm_module.MockChatModel._generate = counting_generate
    try:
        first = run_structured(llm, "system prompt", "human prompt unique to this test", GroundedAnswer)
        second = run_structured(llm, "system prompt", "human prompt unique to this test", GroundedAnswer)
        assert first == second
        assert calls["count"] == 1  # second call was served from cache, LLM not invoked again
    finally:
        mock_llm_module.MockChatModel._generate = original_invoke
