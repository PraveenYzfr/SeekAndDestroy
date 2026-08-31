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


# --------------------------------------------------------------------------
# Hybrid retrieval: dense + BM25 sparse, fused with Reciprocal Rank Fusion.
#
# These need a live server more than the tests above do. The fusion query has a
# shape - prefetch with `using`, an outer FusionQuery - that a mock would accept
# in any form, and a vector-less statistics point is exactly the kind of thing a
# fake client would allow and a real one might not.
# --------------------------------------------------------------------------


@pytest.fixture
def search_mode(monkeypatch):
    """Set SAD_RETRIEVAL__SEARCH_MODE for one test and restore the cache after.

    get_settings is lru_cached, so the env var alone changes nothing - and
    leaving a cleared cache behind would leak this mode into later tests.
    """
    from app.config import get_settings

    def _set(mode: str):
        monkeypatch.setenv("SAD_RETRIEVAL__SEARCH_MODE", mode)
        get_settings.cache_clear()

    yield _set
    monkeypatch.undo()
    get_settings.cache_clear()


def _incident_docs():
    """Three documents that read almost identically and differ only in the
    hostname - the case the sparse half exists for."""
    from app.retrieval.documents import standard_document

    return [
        standard_document("i1", "Memory exhaustion", "Memory exhaustion on cmh-p212 after failover, INC1005432."),
        standard_document("i2", "Memory exhaustion", "Memory exhaustion on cmh-p999 after failover, INC1005610."),
        standard_document("i3", "Memory exhaustion", "Memory exhaustion on lhr-p104 after failover, INC1005788."),
    ]


def test_count_excludes_the_statistics_point(store):
    """The BM25 statistics live in the collection as a reserved point. Counting
    it would make this backend disagree with InMemoryVectorStore by one."""
    store.upsert(_docs())
    assert store.count() == 3


def test_statistics_are_persisted_in_the_collection_not_the_process(store, embedder):
    """They must survive a redeploy. A file inside the container would not, and
    the cache expires - either way the sparse half would silently stop working
    until someone reindexed."""
    from app.retrieval.vector_store import QdrantVectorStore

    store.upsert(_incident_docs())
    reconnected = QdrantVectorStore(
        embedder=embedder, url=QDRANT_URL, api_key="",
        collection="sad_pytest", dimensions=384, fingerprint="pytest",
    )
    stats = reconnected._load_stats()
    assert stats.document_count == 3
    assert stats.average_length > 0


def test_clear_resets_the_statistics(store):
    """clear() destroys the collection, so the stats point goes with it. If the
    cached copy survived, the next upsert would merge a fresh corpus into
    statistics describing one that no longer exists."""
    store.upsert(_incident_docs())
    store.clear()
    assert store.count() == 0
    assert store._load_stats().document_count == 0


def test_sparse_mode_finds_the_exact_host_dense_cannot(store, search_mode):
    """The motivating case. The three documents are near-identical prose, so a
    dense embedder has little to separate them; the hostname is the signal, and
    exact-token matching is what BM25 is for."""
    search_mode("sparse")
    store.upsert(_incident_docs())
    hits = store.search("memory exhaustion on cmh-p212", top_k=3)
    assert hits, "sparse search must return the document containing the host"
    assert hits[0].document.id.endswith("i1"), [h.document.id for h in hits]


def test_sparse_mode_finds_an_incident_number(store, search_mode):
    search_mode("sparse")
    store.upsert(_incident_docs())
    hits = store.search("INC1005788", top_k=3)
    assert hits[0].document.id.endswith("i3"), [h.document.id for h in hits]


def test_hybrid_returns_results(store, search_mode):
    """Exercises the fusion query itself - prefetch over two named vectors with
    an outer RRF. A malformed prefetch is a server-side error, not a silent
    one, so simply getting results back is the assertion that matters."""
    search_mode("hybrid")
    store.upsert(_incident_docs())
    hits = store.search("memory exhaustion cmh-p212", top_k=3)
    assert hits
    assert all(h.document.id.startswith("standard:") for h in hits)


def test_hybrid_honours_filters_on_both_halves(store, search_mode):
    """Filtering only the outer query would let one half fill its prefetch with
    documents the filter then discards."""
    search_mode("hybrid")
    store.upsert(_incident_docs())
    assert store.search("memory", top_k=5, filters={"entity_type": "standard"})
    assert store.search("memory", top_k=5, filters={"entity_type": "cluster"}) == []


def test_dense_mode_is_unchanged(store, search_mode):
    """The pre-hybrid path has to keep working - it is the fallback whenever
    the sparse half has nothing to offer."""
    search_mode("dense")
    store.upsert(_docs())
    assert store.search("Tier-1 resiliency", top_k=1)


def test_hybrid_degrades_to_dense_when_no_statistics_exist(store, search_mode):
    """A collection indexed before this schema has no stats point. Hybrid must
    fall back to dense rather than returning nothing - the failure mode this
    guards against is an empty result set with no error anywhere."""
    search_mode("hybrid")
    store.upsert(_docs())
    store._bm25 = None
    store._client.delete(collection_name=store._collection, points_selector=[0])
    store._bm25 = None
    assert store.search("Tier-1 resiliency", top_k=2), "must still answer from the dense half"
