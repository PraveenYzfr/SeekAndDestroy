"""Real embedding provider (SAD_RETRIEVAL__EMBEDDING_PROVIDER=api): HttpEmbedder's
wire format, the startup-probe fallback/dimension-validation logic in
app.retrieval.embedder, and the fingerprint guard in app.retrieval.vector_store
that keeps a persisted index from being queried by a different embedder than
built it.
"""

from __future__ import annotations

import json

import httpx
import pytest

import app.retrieval.embedder as embedder_module
from app.config import get_settings
from app.models.retrieval import RetrievalDocument
from app.retrieval.embedder import DeterministicHashEmbedder, HttpEmbedder
from app.retrieval.vector_store import InMemoryVectorStore

# ---------------------------------------------------------------------------
# HttpEmbedder wire format (httpx.MockTransport - no real network)
# ---------------------------------------------------------------------------


def test_embed_documents_sends_correct_payload_and_respects_batch_size():
    received = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        received.append(payload)
        n = len(payload["input"])
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [float(i)] * 3} for i in range(n)]})

    embedder = HttpEmbedder(
        base_url="https://api.openai.com/v1", model="text-embedding-3-small",
        api_key="sk-test", batch_size=2, transport=httpx.MockTransport(handler),
    )
    vectors = embedder.embed_documents(["a", "b", "c"])

    assert len(vectors) == 3
    assert len(received) == 2  # batch_size=2 over 3 texts -> batches of [a,b] then [c]
    assert received[0]["input"] == ["a", "b"]
    assert received[1]["input"] == ["c"]
    assert received[0]["model"] == "text-embedding-3-small"


def test_embed_documents_empty_list_makes_no_request():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json={"data": []})

    embedder = HttpEmbedder(base_url="https://api.openai.com/v1", model="m", transport=httpx.MockTransport(handler))
    assert embedder.embed_documents([]) == []
    assert calls["count"] == 0


def test_embed_query_returns_single_vector_sorted_by_index():
    def handler(request: httpx.Request) -> httpx.Response:
        # Response deliberately out of order - the client must sort by index.
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0, 2.0]}]})

    embedder = HttpEmbedder(base_url="https://api.openai.com/v1", model="m", transport=httpx.MockTransport(handler))
    assert embedder.embed_query("hi") == [1.0, 2.0]


def test_openai_style_uses_authorization_bearer_header():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0]}]})

    embedder = HttpEmbedder(
        base_url="https://api.openai.com/v1", model="m", api_key="sk-test", transport=httpx.MockTransport(handler)
    )
    embedder.embed_query("hi")

    assert captured["headers"]["authorization"] == "Bearer sk-test"
    assert "api-key" not in captured["headers"]
    assert captured["url"] == "https://api.openai.com/v1/embeddings"


def test_azure_style_uses_api_key_header_and_api_version_query_param():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0]}]})

    embedder = HttpEmbedder(
        base_url="https://my-resource.openai.azure.com/openai/deployments/mydeploy",
        model="text-embedding-3-small", api_key="azure-key", api_version="2024-02-01",
        extra_headers={"api-key": "azure-key"}, transport=httpx.MockTransport(handler),
    )
    embedder.embed_query("hi")

    assert captured["headers"]["api-key"] == "azure-key"
    assert "authorization" not in captured["headers"]
    assert "api-version=2024-02-01" in captured["url"]


def test_retries_once_on_5xx_then_succeeds():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [1.0]}]})

    embedder = HttpEmbedder(base_url="https://api.openai.com/v1", model="m", transport=httpx.MockTransport(handler))
    assert embedder.embed_query("hi") == [1.0]
    assert calls["count"] == 2


def test_does_not_retry_on_4xx():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(401, json={"error": "bad key"})

    embedder = HttpEmbedder(base_url="https://api.openai.com/v1", model="m", transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        embedder.embed_query("hi")
    assert calls["count"] == 1


# ---------------------------------------------------------------------------
# build_embedder(): startup probe, fallback, dimension validation
# ---------------------------------------------------------------------------


class _StubEmbedder:
    """Stands in for HttpEmbedder so the factory's fallback/validation branching
    can be tested without any real HTTP mechanics (those are covered above)."""

    def __init__(self, *, result=None, error: Exception | None = None, **_kwargs):
        self._result = result
        self._error = error

    def embed_query(self, text: str) -> list[float]:
        if self._error is not None:
            raise self._error
        return self._result


def _set_retrieval(**overrides):
    settings = get_settings().retrieval
    original = {k: getattr(settings, k) for k in overrides}
    for k, v in overrides.items():
        setattr(settings, k, v)
    return original


def _restore_retrieval(original: dict):
    settings = get_settings().retrieval
    for k, v in original.items():
        setattr(settings, k, v)
    embedder_module.reset_embedder_cache()


def test_build_embedder_falls_back_to_hash_when_api_unreachable(monkeypatch):
    monkeypatch.setattr(
        embedder_module, "HttpEmbedder",
        lambda **kwargs: _StubEmbedder(error=ConnectionError("simulated network failure")),
    )
    original = _set_retrieval(embedding_provider="api", embedding_base_url="https://fake.example/v1", embedding_dimensions=384)
    embedder_module.reset_embedder_cache()
    try:
        result = embedder_module.get_embedder()
        assert isinstance(result, DeterministicHashEmbedder)
        assert embedder_module.get_embedder_fingerprint() == "hash:384"
    finally:
        _restore_retrieval(original)


def test_build_embedder_raises_clear_error_on_dimension_mismatch(monkeypatch):
    monkeypatch.setattr(
        embedder_module, "HttpEmbedder",
        lambda **kwargs: _StubEmbedder(result=[0.0] * 999),  # wrong dimension
    )
    original = _set_retrieval(
        embedding_provider="api", embedding_base_url="https://fake.example/v1",
        embedding_dimensions=384, embedding_model="text-embedding-3-small",
    )
    embedder_module.reset_embedder_cache()
    try:
        with pytest.raises(ValueError, match="does not match"):
            embedder_module.get_embedder()
    finally:
        _restore_retrieval(original)


def test_build_embedder_succeeds_when_dimensions_match(monkeypatch):
    monkeypatch.setattr(embedder_module, "HttpEmbedder", lambda **kwargs: _StubEmbedder(result=[0.1] * 384))
    original = _set_retrieval(embedding_provider="api", embedding_base_url="https://fake.example/v1", embedding_dimensions=384)
    embedder_module.reset_embedder_cache()
    try:
        result = embedder_module.get_embedder()
        assert isinstance(result, _StubEmbedder)
        assert embedder_module.get_embedder_fingerprint().startswith("api:")
    finally:
        _restore_retrieval(original)


def test_hash_provider_unchanged_default_behavior():
    # The default (SAD_RETRIEVAL__EMBEDDING_PROVIDER=hash) must be untouched by this feature.
    original = _set_retrieval(embedding_provider="hash", embedding_dimensions=384)
    embedder_module.reset_embedder_cache()
    try:
        result = embedder_module.get_embedder()
        assert isinstance(result, DeterministicHashEmbedder)
        assert embedder_module.get_embedder_fingerprint() == "hash:384"
    finally:
        _restore_retrieval(original)


# ---------------------------------------------------------------------------
# Fingerprint guard on persisted InMemoryVectorStore indexes
# ---------------------------------------------------------------------------


def _doc(doc_id: str) -> RetrievalDocument:
    return RetrievalDocument(id=doc_id, text="hello world", entity_type="application", entity_id=1)


def test_matching_fingerprint_reuses_persisted_index(tmp_path):
    path = tmp_path / "vectors.json"
    embedder = DeterministicHashEmbedder(dimensions=8)

    store1 = InMemoryVectorStore(embedder, persist_path=path, fingerprint="hash:8")
    store1.upsert([_doc("doc:1")])
    assert store1.count() == 1

    store2 = InMemoryVectorStore(embedder, persist_path=path, fingerprint="hash:8")
    assert store2.count() == 1


def test_fingerprint_change_discards_persisted_index_instead_of_reusing_it(tmp_path):
    path = tmp_path / "vectors.json"
    embedder = DeterministicHashEmbedder(dimensions=8)

    store1 = InMemoryVectorStore(embedder, persist_path=path, fingerprint="hash:8")
    store1.upsert([_doc("doc:1")])
    assert store1.count() == 1

    # Simulates switching SAD_RETRIEVAL__EMBEDDING_PROVIDER=hash -> api: a different
    # fingerprint must never inherit vectors from the old embedding space.
    store2 = InMemoryVectorStore(embedder, persist_path=path, fingerprint="api:text-embedding-3-small:1536")
    assert store2.count() == 0


def test_pre_fingerprint_file_format_is_discarded_not_crashed_on(tmp_path):
    path = tmp_path / "vectors.json"
    # The old format was a bare JSON array with no fingerprint header.
    path.write_text(
        json.dumps([{"document": _doc("doc:1").model_dump(mode="json"), "vector": [0.1] * 8}]),
        encoding="utf-8",
    )
    embedder = DeterministicHashEmbedder(dimensions=8)
    store = InMemoryVectorStore(embedder, persist_path=path, fingerprint="hash:8")
    assert store.count() == 0


# ---------------------------------------------------------------------------
# Retrieval degrades gracefully on a runtime embedding failure
# ---------------------------------------------------------------------------


def test_retrieve_related_context_degrades_to_empty_on_search_failure(monkeypatch):
    from app.graph import nodes

    class _BoomStore:
        def search(self, query, *, top_k=8, filters=None):
            raise RuntimeError("embedding API down mid-run")

    monkeypatch.setattr(nodes, "get_vector_store", lambda: _BoomStore())
    result = nodes.retrieve_related_context({"user_query": "why was nyc-03 rejected?"})
    assert result == {"retrieved_context": []}


def test_investigation_completes_despite_retrieval_failure(monkeypatch):
    from app.graph import nodes
    from app.graph.graph import run_investigation

    class _BoomStore:
        def search(self, query, *, top_k=8, filters=None):
            raise RuntimeError("embedding API down mid-run")

    monkeypatch.setattr(nodes, "get_vector_store", lambda: _BoomStore())
    result = run_investigation(query="Find the best clusters for hosting APP-ONBOARDING.", created_by=1)
    assert result["status"] in ("AwaitingReview", "Completed")
