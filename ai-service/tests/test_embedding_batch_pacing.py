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
