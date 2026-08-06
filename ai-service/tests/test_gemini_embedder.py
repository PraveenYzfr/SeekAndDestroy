"""Gemini native embeddings provider (SAD_RETRIEVAL__EMBEDDING_PROVIDER=gemini):
GeminiEmbedder's wire format (a different shape from the OpenAI-compatible
HttpEmbedder - see app.retrieval.gemini_embedder's module docstring), plus the
same startup-probe/fallback/dimension-validation contract every real provider
gets via app.retrieval.embedder._probe_or_fallback.
"""

from __future__ import annotations

import json

import httpx
import pytest

import app.retrieval.embedder as embedder_module
from app.config import get_settings
from app.retrieval.embedder import DeterministicHashEmbedder
from app.retrieval.gemini_embedder import GeminiEmbedder


# ---------------------------------------------------------------------------
# GeminiEmbedder wire format (httpx.MockTransport - no real network)
# ---------------------------------------------------------------------------


def test_embed_documents_sends_gemini_batch_shape_and_respects_batch_size():
    received = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        received.append(payload)
        n = len(payload["requests"])
        return httpx.Response(200, json={"embeddings": [{"values": [float(i)] * 3} for i in range(n)]})

    embedder = GeminiEmbedder(
        api_key="gm-test", model="gemini-embedding-001", batch_size=2, transport=httpx.MockTransport(handler)
    )
    vectors = embedder.embed_documents(["a", "b", "c"])

    assert len(vectors) == 3
    assert len(received) == 2  # batch_size=2 over 3 texts -> batches of [a,b] then [c]
    assert received[0]["requests"][0]["content"]["parts"][0]["text"] == "a"
    assert received[0]["requests"][0]["model"] == "models/gemini-embedding-001"


def test_embed_documents_empty_list_makes_no_request():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json={"embeddings": []})

    embedder = GeminiEmbedder(api_key="gm-test", transport=httpx.MockTransport(handler))
    assert embedder.embed_documents([]) == []
    assert calls["count"] == 0


def test_embed_query_returns_single_vector():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [{"values": [1.0, 2.0]}]})

    embedder = GeminiEmbedder(api_key="gm-test", transport=httpx.MockTransport(handler))
    assert embedder.embed_query("hi") == [1.0, 2.0]


def test_uses_x_goog_api_key_header_not_authorization_bearer():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"embeddings": [{"values": [0.0]}]})

    embedder = GeminiEmbedder(
        api_key="gm-test", model="gemini-embedding-001", transport=httpx.MockTransport(handler)
    )
    embedder.embed_query("hi")

    assert captured["headers"]["x-goog-api-key"] == "gm-test"
    assert "authorization" not in captured["headers"]
    assert captured["url"].endswith("/models/gemini-embedding-001:batchEmbedContents")


def test_model_name_already_prefixed_with_models_is_not_doubled():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"embeddings": [{"values": [0.0]}]})

    embedder = GeminiEmbedder(api_key="k", model="models/gemini-embedding-001", transport=httpx.MockTransport(handler))
    embedder.embed_query("hi")
    assert "models/models/" not in captured["url"]


def test_retries_once_on_5xx_then_succeeds():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"embeddings": [{"values": [1.0]}]})

    embedder = GeminiEmbedder(api_key="k", transport=httpx.MockTransport(handler))
    assert embedder.embed_query("hi") == [1.0]
    assert calls["count"] == 2


def test_does_not_retry_on_4xx():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(403, json={"error": "bad key"})

    embedder = GeminiEmbedder(api_key="k", transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        embedder.embed_query("hi")
    assert calls["count"] == 1


# ---------------------------------------------------------------------------
# build_embedder(): same startup-probe/fallback/dimension-validation contract
# as every other real provider, exercised through the "gemini" branch
# ---------------------------------------------------------------------------


class _StubEmbedder:
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


def test_build_embedder_falls_back_to_hash_when_gemini_unreachable(monkeypatch):
    import app.retrieval.gemini_embedder as gemini_module

    monkeypatch.setattr(
        gemini_module, "GeminiEmbedder",
        lambda **kwargs: _StubEmbedder(error=ConnectionError("simulated network failure")),
    )
    original = _set_retrieval(embedding_provider="gemini", embedding_api_key="gm-test", embedding_dimensions=768)
    embedder_module.reset_embedder_cache()
    try:
        result = embedder_module.get_embedder()
        assert isinstance(result, DeterministicHashEmbedder)
        assert embedder_module.get_embedder_fingerprint() == "hash:768"
    finally:
        _restore_retrieval(original)


def test_build_embedder_raises_clear_error_on_gemini_dimension_mismatch(monkeypatch):
    import app.retrieval.gemini_embedder as gemini_module

    monkeypatch.setattr(gemini_module, "GeminiEmbedder", lambda **kwargs: _StubEmbedder(result=[0.0] * 999))
    original = _set_retrieval(
        embedding_provider="gemini", embedding_api_key="gm-test",
        embedding_dimensions=768, embedding_model="gemini-embedding-001",
    )
    embedder_module.reset_embedder_cache()
    try:
        with pytest.raises(ValueError, match="does not match"):
            embedder_module.get_embedder()
    finally:
        _restore_retrieval(original)


def test_build_embedder_succeeds_when_gemini_dimensions_match(monkeypatch):
    import app.retrieval.gemini_embedder as gemini_module

    monkeypatch.setattr(gemini_module, "GeminiEmbedder", lambda **kwargs: _StubEmbedder(result=[0.1] * 768))
    original = _set_retrieval(embedding_provider="gemini", embedding_api_key="gm-test", embedding_dimensions=768)
    embedder_module.reset_embedder_cache()
    try:
        result = embedder_module.get_embedder()
        assert isinstance(result, _StubEmbedder)
        assert embedder_module.get_embedder_fingerprint().startswith("gemini:")
    finally:
        _restore_retrieval(original)
