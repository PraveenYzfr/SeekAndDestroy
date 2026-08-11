"""Qdrant backend tests that run against a *live* Qdrant.

These exist because the Qdrant path was broken for real and nothing caught it:
``QdrantClient.search()`` was removed in qdrant-client 1.x, so every search
raised ``AttributeError`` on the pinned 1.18 client. It never surfaced because
``SAD_RETRIEVAL__BACKEND`` defaults to ``memory``, no test exercised the
backend against a server, and ``graph.retrieve_related_context`` catches
retrieval failures and degrades to no context - so the only symptom in
production would have been quietly worse narration.

Mocking the client would not have caught it either: a mock has whatever
methods you give it. That is why these talk to a real server, and skip
cleanly when one is not running:

    docker compose -f docker/docker-compose.yml up -d qdrant
"""

from __future__ import annotations

import os

import pytest

QDRANT_URL = os.environ.get("SAD_RETRIEVAL__QDRANT_URL", "http://localhost:6333")


def _qdrant_available() -> bool:
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"{QDRANT_URL}/healthz", timeout=2) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _qdrant_available(), reason=f"no Qdrant at {QDRANT_URL} - `docker compose up -d qdrant`"
)


@pytest.fixture
def embedder():
    """The hash embedder, constructed directly rather than via get_embedder().

    Deliberate: the process-wide embedder cache must not be touched here. When
    a real provider is configured (this repo runs Gemini), clearing that cache
    makes the next caller rebuild and re-probe the live API - and any later
    test that triggers an index rebuild would re-embed the whole ~2,400-doc
    corpus at real cost. What is under test is the store, not the embedder.
    """
    from app.retrieval.embedder import DeterministicHashEmbedder

    return DeterministicHashEmbedder(dimensions=384)


@pytest.fixture
def store(embedder):
    """A throwaway collection, dropped after each test."""
    from app.retrieval.vector_store import QdrantVectorStore

    store = QdrantVectorStore(
        embedder=embedder, url=QDRANT_URL, api_key="",
        collection="sad_pytest", dimensions=384, fingerprint="pytest",
    )
    yield store
    store.clear()


def _docs():
    from app.retrieval.documents import standard_document

    return [
        standard_document("t1", "Tier-1 resiliency", "Tier-1 clusters require at least three active nodes."),
        standard_document("t2", "Headroom policy", "Projected CPU utilization must stay below 75 percent."),
        standard_document("t3", "Node placement", "Hosts are ranked by remaining headroom inside a cluster."),
    ]


def test_upsert_then_search_returns_results(store):
    """The regression itself: this raised AttributeError before the fix."""
    store.upsert(_docs())
    hits = store.search("how many nodes does Tier-1 need", top_k=2)
    assert hits, "a live Qdrant with 3 indexed documents must return matches"
    assert all(0.0 <= h.score <= 1.0 or h.score > 0 for h in hits)


def test_search_returns_the_real_document_id_not_the_point_id(store):
    """Qdrant needs a numeric point id, so the document id lives in the
    payload. Returning hit.id would hand callers an opaque integer and make
    this backend inconsistent with InMemoryVectorStore.
    """
    store.upsert(_docs())
    hits = store.search("Tier-1 resiliency", top_k=1)
    assert hits[0].document.id.startswith("standard:"), hits[0].document.id
    assert not hits[0].document.id.isdigit()


def test_documents_survive_a_new_client(store, embedder):
    """Persistence: a fresh client against the same server must still see the
    points, proving they came off Qdrant's disk and not process memory.
    """
    from app.retrieval.vector_store import QdrantVectorStore

    store.upsert(_docs())
    reconnected = QdrantVectorStore(
        embedder=embedder, url=QDRANT_URL, api_key="",
        collection="sad_pytest", dimensions=384, fingerprint="pytest",
    )
    assert reconnected.count() == 3
    assert reconnected.search("headroom", top_k=1)


def test_search_honours_metadata_filters(store):
    store.upsert(_docs())
    unfiltered = store.search("cluster", top_k=5)
    matching = store.search("cluster", top_k=5, filters={"entity_type": "standard"})
    assert matching, "every seeded document is a standard, so the filter must match them"
    assert len(matching) <= len(unfiltered)
    assert all(h.document.entity_type == "standard" for h in matching)

    # And a filter that matches nothing must return nothing, not silently
    # fall back to an unfiltered search.
    assert store.search("cluster", top_k=5, filters={"entity_type": "cluster"}) == []


def test_a_different_fingerprint_gets_a_different_collection(store):
    """Switching embedder must not silently query old vectors with a new
    embedder - the fingerprint is folded into the collection name.
    """
    from app.retrieval.vector_store import _fingerprinted_collection_name

    assert _fingerprinted_collection_name("c", "gemini:3072") != _fingerprinted_collection_name("c", "hash:384")
