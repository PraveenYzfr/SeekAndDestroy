"""embedding_batch_delay_seconds paces consecutive batch calls within one
embed_documents() run - discovered necessary after a live reindex against a
real (free-tier) Gemini key hit a sustained requests-per-minute quota that
per-request retry-with-backoff alone couldn't recover from.
"""

from __future__ import annotations

import httpx

from app.retrieval.embedder import HttpEmbedder
from app.retrieval.gemini_embedder import GeminiEmbedder


def test_http_embedder_sleeps_between_batches_not_before_the_first(monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.retrieval.embedder.time.sleep", sleeps.append)

    def handler(request: httpx.Request) -> httpx.Response:
        n = len(__import__("json").loads(request.content)["input"])
        return httpx.Response(200, json={"data": [{"index": i, "embedding": [0.0]} for i in range(n)]})

    embedder = HttpEmbedder(
        base_url="https://api.openai.com/v1", model="m", batch_size=1,
        batch_delay_seconds=3.0, transport=httpx.MockTransport(handler),
    )
    embedder.embed_documents(["a", "b", "c"])  # 3 batches of 1 -> 2 gaps
    assert sleeps == [3.0, 3.0]


def test_http_embedder_no_delay_by_default():
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.0]}]})

    embedder = HttpEmbedder(base_url="https://api.openai.com/v1", model="m", batch_size=1, transport=httpx.MockTransport(handler))
    embedder.embed_documents(["a", "b", "c"])  # must not hang or raise - default delay is 0
    assert calls["count"] == 3


def test_gemini_embedder_sleeps_between_batches_not_before_the_first(monkeypatch):
    sleeps = []
    monkeypatch.setattr("app.retrieval.gemini_embedder.time.sleep", sleeps.append)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"embeddings": [{"values": [0.0]}]})

    embedder = GeminiEmbedder(
        api_key="k", batch_size=1, batch_delay_seconds=2.5, transport=httpx.MockTransport(handler)
    )
    embedder.embed_documents(["a", "b"])  # 2 batches of 1 -> 1 gap
    assert sleeps == [2.5]


def test_a_transient_429_is_survived_rather_than_failing_the_run():
    """Six attempts, not three.

    The 429s seen against Gemini name
    `global_embed_content_requests_per_minute_per_base_model` - a pool shared
    across the base model, not this project's allowance. Measured 2026-09-01:
    the project peaked at 1.79K of 3K RPM with unlimited requests per day, and a
    batch was refused seconds before a larger one went through untouched.

    So a refusal means the pool was busy, and the only useful response is to
    wait and ask again. Three attempts was not enough and cost a full index run;
    this test exists so that number cannot quietly go back down.
    """
    import httpx

    from app.retrieval.gemini_embedder import GeminiEmbedder

    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # Refused four times - more than the old limit of three - then served.
        if calls["n"] <= 4:
            return httpx.Response(
                429,
                json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                                "message": "Quota exceeded for "
                                           "global_embed_content_requests_per_minute_per_base_model",
                                "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                             "retryDelay": "0s"}]}},
            )
        return httpx.Response(200, json={"embeddings": [{"values": [0.1, 0.2, 0.3]}]})

    embedder = GeminiEmbedder(
        api_key="test-key", max_attempts=6,
        transport=httpx.MockTransport(handler),
    )
    vectors = embedder.embed_documents(["one document"])

    assert vectors == [[0.1, 0.2, 0.3]]
    assert calls["n"] == 5, f"expected 4 refusals then a success, got {calls['n']} calls"


def test_three_attempts_would_not_have_survived_it():
    """The counterpart: the old limit fails on the same sequence.

    Without this, the test above passes whether max_attempts is 6 or 60 and
    says nothing about why the number changed.
    """
    import httpx
    import pytest

    from app.retrieval.gemini_embedder import GeminiEmbedder

    def always_429(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "busy",
                            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo",
                                         "retryDelay": "0s"}]}},
        )

    embedder = GeminiEmbedder(
        api_key="test-key", max_attempts=3, transport=httpx.MockTransport(always_429)
    )
    with pytest.raises(httpx.HTTPStatusError):
        embedder.embed_documents(["one document"])
