"""Daily call-count budget for real LLM/embedding providers
(SAD_LLM__DAILY_CALL_BUDGET, SAD_RETRIEVAL__EMBEDDING_DAILY_CALL_BUDGET) -
app.services.spend_budget, and its wiring into HttpChatModel/HttpEmbedder/
GeminiEmbedder (mock/hash providers never touch this at all).
"""

from __future__ import annotations

import httpx
import pytest

from app.agents.http_chat_model import HttpChatModel
from app.cache.store import InMemoryCacheStore, get_cache_store, reset_cache_store_cache
from app.retrieval.embedder import HttpEmbedder
from app.retrieval.gemini_embedder import GeminiEmbedder
from app.services.spend_budget import BudgetExceededError, check_and_increment


@pytest.fixture(autouse=True)
def _fresh_cache_store():
    reset_cache_store_cache()
    yield
    reset_cache_store_cache()


# ---------------------------------------------------------------------------
# check_and_increment
# ---------------------------------------------------------------------------


def test_zero_limit_means_unlimited_and_never_touches_the_store():
    assert check_and_increment("test-ns", 0) == 0
    assert get_cache_store().count() == 0  # no key was ever written


def test_increments_up_to_the_limit_then_raises():
    for expected in (1, 2, 3):
        assert check_and_increment("test-ns", 3) == expected
    with pytest.raises(BudgetExceededError) as exc_info:
        check_and_increment("test-ns", 3)
    assert exc_info.value.namespace == "test-ns"
    assert exc_info.value.limit == 3
    assert exc_info.value.used == 3


def test_different_namespaces_have_independent_budgets():
    assert check_and_increment("llm_chat", 1) == 1
    assert check_and_increment("embedding", 1) == 1  # unaffected by llm_chat's count
    with pytest.raises(BudgetExceededError):
        check_and_increment("llm_chat", 1)


def test_uses_atomic_incr_not_get_then_set(monkeypatch):
    # A get()-then-set() implementation would silently under-count under
    # concurrency; incr() must be what's actually called.
    store = InMemoryCacheStore()
    calls = []
    original_incr = store.incr

    def spy_incr(key, **kwargs):
        calls.append(key)
        return original_incr(key, **kwargs)

    monkeypatch.setattr(store, "incr", spy_incr)
    import app.services.spend_budget as budget_module

    monkeypatch.setattr(budget_module, "get_cache_store", lambda: store)
    check_and_increment("test-ns", 5)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Wiring: HttpChatModel, HttpEmbedder, GeminiEmbedder
# ---------------------------------------------------------------------------


def _ok_chat_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})


def test_http_chat_model_respects_daily_call_budget(monkeypatch):
    from app.config import get_settings

    settings = get_settings().llm
    original = settings.daily_call_budget
    settings.daily_call_budget = 1
    try:
        from langchain_core.messages import HumanMessage

        model = HttpChatModel(base_url="https://api.openai.com/v1", model="m", transport=httpx.MockTransport(_ok_chat_response))
        model.invoke([HumanMessage(content="hi")])  # 1st call: within budget
        with pytest.raises(BudgetExceededError):
            model.invoke([HumanMessage(content="hi again")])  # 2nd call: over budget
    finally:
        settings.daily_call_budget = original


def test_http_embedder_respects_daily_call_budget():
    from app.config import get_settings

    settings = get_settings().retrieval
    original = settings.embedding_daily_call_budget
    settings.embedding_daily_call_budget = 1
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0]}]})

        embedder = HttpEmbedder(base_url="https://api.openai.com/v1", model="m", transport=httpx.MockTransport(handler))
        embedder.embed_query("first")  # within budget
        with pytest.raises(BudgetExceededError):
            embedder.embed_query("second")  # over budget
    finally:
        settings.embedding_daily_call_budget = original


def test_gemini_embedder_respects_daily_call_budget():
    from app.config import get_settings

    settings = get_settings().retrieval
    original = settings.embedding_daily_call_budget
    settings.embedding_daily_call_budget = 1
    try:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"embeddings": [{"values": [0.0]}]})

        embedder = GeminiEmbedder(api_key="k", transport=httpx.MockTransport(handler))
        embedder.embed_query("first")  # within budget
        with pytest.raises(BudgetExceededError):
            embedder.embed_query("second")  # over budget
    finally:
        settings.embedding_daily_call_budget = original


def test_default_budget_is_unlimited_for_real_providers():
    # The shipped default (0) must never block a real provider that hasn't
    # explicitly opted into a budget.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0]}]})

    embedder = HttpEmbedder(base_url="https://api.openai.com/v1", model="m", transport=httpx.MockTransport(handler))
    for _ in range(5):
        embedder.embed_query("hi")  # never raises
