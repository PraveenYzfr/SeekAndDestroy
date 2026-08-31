"""Vector store abstraction: one interface, two backends.

``InMemoryVectorStore`` is the default (Docker/Qdrant is not assumed to be
running) and is functionally complete - indexing, updates, deletes, metadata
filtering, cosine similarity, top-k, and optional JSON-file persistence so
data survives a process restart. ``QdrantVectorStore`` implements the same
protocol against a real Qdrant server when ``SAD_RETRIEVAL__BACKEND=qdrant``.

Both backends are fingerprint-guarded (see app.retrieval.embedder): vectors
persisted by one embedder (provider + model + dimensions) must never be
queried by another, since a hash-embedder vector and a real API-embedder
vector don't live in a comparable similarity space. Switching
``SAD_RETRIEVAL__EMBEDDING_PROVIDER`` is safe in both directions with zero
manual steps - a fingerprint mismatch just means starting from an empty
index rather than silently returning nonsense similarity scores.
"""

from __future__ import annotations

import hashlib
import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Protocol

import structlog
from langchain_core.embeddings import Embeddings

from app.config import get_settings
from app.models.retrieval import RetrievalDocument, SearchResult
from app.retrieval import sparse
from app.retrieval.embedder import get_embedder, get_embedder_fingerprint

logger = structlog.get_logger(__name__)


class VectorStore(Protocol):
    def upsert(self, documents: list[RetrievalDocument]) -> None: ...
    def delete(self, ids: list[str]) -> None: ...
    def search(self, query: str, *, top_k: int = 8, filters: dict | None = None) -> list[SearchResult]: ...
    def clear(self) -> None: ...
    def count(self) -> int: ...


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _matches_filters(doc: RetrievalDocument, filters: dict | None) -> bool:
    if not filters:
        return True
    meta = doc.metadata()
    for key, value in filters.items():
        if value is None:
            continue
        if meta.get(key) != value:
            return False
    return True


class InMemoryVectorStore:
    def __init__(self, embedder: Embeddings, persist_path: Path | None = None, fingerprint: str = ""):
        self._embedder = embedder
        self._persist_path = persist_path
        self._fingerprint = fingerprint
        self._vectors: dict[str, list[float]] = {}
        self._documents: dict[str, RetrievalDocument] = {}
        self._load()

    def _load(self) -> None:
        if not self._persist_path or not self._persist_path.exists():
            return
        raw = json.loads(self._persist_path.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            # Pre-fingerprint file format - provenance of these vectors is unknown,
            # so discard rather than risk mixing embedding spaces.
            logger.warning("vector_store.persisted_index_missing_fingerprint_discarding", path=str(self._persist_path))
            return
        stored_fingerprint = raw.get("fingerprint", "")
        if stored_fingerprint != self._fingerprint:
            logger.warning(
                "vector_store.fingerprint_changed_discarding_index",
                stored=stored_fingerprint, active=self._fingerprint, path=str(self._persist_path),
            )
            return
        for entry in raw.get("documents", []):
            doc = RetrievalDocument(**entry["document"])
            self._documents[doc.id] = doc
            self._vectors[doc.id] = entry["vector"]

    def _save(self) -> None:
        if not self._persist_path:
            return
        payload = {
            "fingerprint": self._fingerprint,
            "documents": [
                {"document": self._documents[doc_id].model_dump(mode="json"), "vector": vector}
                for doc_id, vector in self._vectors.items()
            ],
        }
        self._persist_path.write_text(json.dumps(payload), encoding="utf-8")

    def upsert(self, documents: list[RetrievalDocument]) -> None:
        if not documents:
            return
        vectors = self._embedder.embed_documents([d.text for d in documents])
        for doc, vec in zip(documents, vectors):
            self._documents[doc.id] = doc
            self._vectors[doc.id] = vec
        self._save()

    def delete(self, ids: list[str]) -> None:
        for doc_id in ids:
            self._documents.pop(doc_id, None)
            self._vectors.pop(doc_id, None)
        self._save()

    def search(self, query: str, *, top_k: int = 8, filters: dict | None = None) -> list[SearchResult]:
        query_vec = self._embedder.embed_query(query)
        scored = []
        for doc_id, doc in self._documents.items():
            if not _matches_filters(doc, filters):
                continue
            score = _cosine(query_vec, self._vectors[doc_id])
            scored.append(SearchResult(document=doc, score=score))
        scored.sort(key=lambda r: r.score, reverse=True)
        return scored[:top_k]

    def clear(self) -> None:
        self._documents.clear()
        self._vectors.clear()
        self._save()

    def count(self) -> int:
        return len(self._documents)


def _fingerprinted_collection_name(base_collection: str, fingerprint: str) -> str:
    """Qdrant has no first-class "collection metadata" API, but it does
    natively isolate by collection name - so the fingerprint is folded into
    the name itself. A fingerprint change (switching embedder) then means
    talking to a different, empty collection automatically; the old one is
    simply left orphaned rather than queried with an incompatible embedder.
    """
    suffix = hashlib.blake2b(
        f"{fingerprint}|schema=v{_COLLECTION_SCHEMA}".encode("utf-8"), digest_size=4
    ).hexdigest()
    return f"{base_collection}__{suffix}"


#: Bumped when the collection's *shape* changes, independently of the embedder.
#: v2 renamed the dense vector from unnamed to "dense" and added a named
#: "sparse" vector for BM25. A collection created under v1 has neither, so
#: upserting into it fails - folding this into the name means an existing
#: deployment quietly builds a new collection on next index instead of erroring,
#: exactly as it already does when the embedder changes.
_COLLECTION_SCHEMA = 2


#: Points per Qdrant upsert call. 256 at 3072 dimensions is ~9 MB, well
#: inside Qdrant 32 MiB request cap.
_UPSERT_BATCH = 256

#: Named vectors. Qdrant allows one unnamed dense vector, but mixing an unnamed
#: dense with a named sparse makes the Prefetch syntax for fusion awkward and
#: easy to get subtly wrong. Naming both keeps every query symmetric.
DENSE = "dense"
SPARSE = "sparse"

#: Reserved point holding the BM25 corpus statistics. They live in the
#: collection they describe rather than in a file or the cache: a file inside
#: the container is lost on redeploy, and the cache expires - either way the
#: sparse half would silently return nothing until someone reindexed. Here the
#: stats are created, replaced and destroyed with the collection itself.
_STATS_POINT_ID = 0


class QdrantVectorStore:
    def __init__(
        self, embedder: Embeddings, url: str, api_key: str, collection: str, dimensions: int, fingerprint: str = ""
    ):
        from qdrant_client import QdrantClient

        self._embedder = embedder
        self._collection = _fingerprinted_collection_name(collection, fingerprint) if fingerprint else collection
        self._client = QdrantClient(url=url, api_key=api_key or None)
        self._dimensions = dimensions
        #: Corpus statistics, read lazily from the collection on first use. None
        #: means "not yet read", which is distinct from BM25Stats() meaning "read,
        #: and the corpus is empty" - the difference decides fit-versus-merge.
        self._bm25: sparse.BM25Stats | None = None
        existing = [c.name for c in self._client.get_collections().collections]
        if self._collection not in existing:
            stale = [c for c in existing if c == collection or c.startswith(f"{collection}__")]
            if stale:
                logger.warning(
                    "vector_store.qdrant_fingerprint_changed_new_collection",
                    old_collections=stale, new_collection=self._collection,
                )
            self._ensure_collection()

    def _ensure_collection(self) -> None:
        from qdrant_client.models import (
            Distance, SparseIndexParams, SparseVectorParams, VectorParams,
        )

        # Both vectors are always created and always written, regardless of
        # search_mode. The mode is a query-time choice: baking it into the
        # index would mean re-embedding the entire corpus to compare dense
        # against hybrid, which is how that comparison ends up never happening.
        self._client.create_collection(
            collection_name=self._collection,
            vectors_config={DENSE: VectorParams(size=self._dimensions, distance=Distance.COSINE)},
            sparse_vectors_config={SPARSE: SparseVectorParams(index=SparseIndexParams())},
        )

    def _load_stats(self) -> sparse.BM25Stats:
        """Corpus statistics for the sparse half, cached per store instance.

        Read from the collection rather than a file or the shared cache. A file
        inside the container does not survive a redeploy and the cache expires;
        either way the sparse half would keep scoring against statistics that no
        longer describe the corpus, and the failure mode is quietly worse
        ranking rather than an error anyone would notice.
        """
        if self._bm25 is not None:
            return self._bm25
        try:
            found = self._client.retrieve(
                collection_name=self._collection, ids=[_STATS_POINT_ID], with_payload=True
            )
        except Exception:
            # A collection written before this schema existed has no stats point.
            # Treating that as an empty corpus degrades hybrid to dense, which is
            # exactly the old behaviour, rather than failing the search.
            found = []
        raw = (found[0].payload or {}).get("bm25") if found else None
        self._bm25 = sparse.BM25Stats.from_dict(raw) if raw else sparse.BM25Stats()
        return self._bm25

    def _save_stats(self, stats: sparse.BM25Stats) -> None:
        from qdrant_client.models import PointStruct

        # No vectors on this point, only payload. Qdrant allows a point to carry
        # any subset of a collection's named vectors, and carrying none means it
        # can never be returned by a dense or a sparse query - so the statistics
        # live inside the collection they describe without ever polluting a
        # result set. count() excludes it explicitly.
        self._client.upsert(
            collection_name=self._collection,
            points=[PointStruct(id=_STATS_POINT_ID, vector={}, payload={"bm25": stats.to_dict()})],
        )
        self._bm25 = stats

    @staticmethod
    def _point_id(doc_id: str) -> int:
        import hashlib

        point_id = int(hashlib.blake2b(doc_id.encode("utf-8"), digest_size=8).hexdigest(), 16) % (2**63)
        # _STATS_POINT_ID is reserved. One document in 2**63 would hash onto it
        # and overwrite the corpus statistics with a document payload, breaking
        # the sparse half estate-wide until the next full reindex. The odds do
        # not justify thinking about it twice, and neither does the fix.
        return point_id or 1

    def upsert(self, documents: list[RetrievalDocument]) -> None:
        from qdrant_client.models import PointStruct, SparseVector

        if not documents:
            return
        texts = [d.text for d in documents]
        vectors = self._embedder.embed_documents(texts)

        # Fit or merge, decided by whether the collection already has statistics.
        # index_all() clears first, which destroys the stats point along with the
        # collection, so a full rebuild always lands on the exact fit() path with
        # no special-casing here. Anything arriving into a populated corpus is an
        # incremental reindex and merges - see sparse.merge on the drift that
        # implies.
        existing = self._load_stats()
        stats = sparse.fit(texts) if existing.document_count == 0 else sparse.merge(existing, texts)

        points = []
        for doc, vec in zip(documents, vectors):
            indices, values = sparse.encode_document(doc.text, stats)
            points.append(
                PointStruct(
                    id=self._point_id(doc.id),
                    # Named, not bare. _ensure_collection creates the collection with
                    # vectors_config={DENSE: ...}, and Qdrant rejects an unnamed vector
                    # against a named-vector collection with a 400 "Not existing vector
                    # name error" - at *index* time, before search is ever reached.
                    #
                    # A document whose text tokenises to nothing still gets a sparse
                    # entry, empty. Omitting the key would leave the point without the
                    # sparse vector at all, and it would then be invisible to the
                    # sparse half of a hybrid query for a reason no log would explain.
                    vector={DENSE: vec, SPARSE: SparseVector(indices=indices, values=values)},
                    payload={"doc_id": doc.id, "text": doc.text, **doc.metadata()},
                )
            )
        # Qdrant rejects any request body over 32 MiB, and a full reindex used
        # to send every point in one call. That is invisible with the default
        # `hash` embedder - 384 floats a point keeps the whole corpus well
        # under the limit - and fatal with real Gemini embeddings at 3072
        # dimensions, where the same corpus serialised to 96 MB and the rebuild
        # died with "JSON payload is larger than allowed". Worse, the failure
        # landed after the collection had been recreated, so the service was
        # left reporting not_ready with no collection at all.
        #
        # Chunking by point count rather than measured bytes keeps this simple
        # and predictable: 256 points is ~9 MB at 3072 dimensions, comfortably
        # inside the limit with room for larger payload text.
        for start in range(0, len(points), _UPSERT_BATCH):
            self._client.upsert(
                collection_name=self._collection,
                points=points[start : start + _UPSERT_BATCH],
            )
        # After the documents, never before: if a batch fails partway, the stored
        # statistics still describe the corpus as it was, and a retry re-derives
        # them. Writing them first would leave a corpus described by statistics
        # counting documents that were never indexed.
        self._save_stats(stats)

    def delete(self, ids: list[str]) -> None:
        self._client.delete(collection_name=self._collection, points_selector=[self._point_id(i) for i in ids])

    def search(self, query: str, *, top_k: int = 8, filters: dict | None = None) -> list[SearchResult]:
        from qdrant_client.models import (
            FieldCondition, Filter, Fusion, FusionQuery, MatchValue, Prefetch, SparseVector,
        )

        qdrant_filter = None
        if filters:
            conditions = [
                FieldCondition(key=k, match=MatchValue(value=v)) for k, v in filters.items() if v is not None
            ]
            if conditions:
                qdrant_filter = Filter(must=conditions)

        retrieval = get_settings().retrieval
        mode = retrieval.search_mode
        sparse_indices, sparse_values = sparse.encode_query(query, self._load_stats())
        # A query with no sparse representation - no statistics stored yet, or
        # every term stopworded - cannot use the sparse half. Falling back to
        # dense keeps the old behaviour instead of returning nothing, which is
        # what a pure-sparse query against an unfitted corpus would do.
        has_sparse = bool(sparse_indices)
        if mode == "sparse" and not has_sparse:
            mode = "dense"
        elif mode == "hybrid" and not has_sparse:
            mode = "dense"

        # query_points, not the older search(): qdrant-client removed
        # QdrantClient.search/search_batch, so on the pinned 1.18 client the
        # old call raised AttributeError. That failed *silently* in practice -
        # graph.retrieve_related_context catches retrieval errors and degrades
        # to no context - so the symptom was worse answers, never an error.
        if mode == "dense":
            hits = self._client.query_points(
                collection_name=self._collection, query=self._embedder.embed_query(query),
                using=DENSE, limit=top_k, query_filter=qdrant_filter, with_payload=True,
            ).points
        elif mode == "sparse":
            hits = self._client.query_points(
                collection_name=self._collection,
                query=SparseVector(indices=sparse_indices, values=sparse_values),
                using=SPARSE, limit=top_k, query_filter=qdrant_filter, with_payload=True,
            ).points
        else:
            # Reciprocal Rank Fusion over two independently ranked lists. RRF
            # combines *ranks*, not scores, which is the whole reason it is
            # usable here: a cosine similarity and a BM25 score have no shared
            # scale, and any weighted sum of the two would be tuning a constant
            # against a corpus rather than combining evidence.
            #
            # Both halves are filtered, not just the outer query. Filtering only
            # at the end would let one half fill its prefetch with documents the
            # filter then discards, so a filtered hybrid search would return
            # fewer results than an unfiltered one for no legitimate reason.
            prefetch_limit = max(retrieval.hybrid_prefetch, top_k)
            hits = self._client.query_points(
                collection_name=self._collection,
                prefetch=[
                    Prefetch(
                        query=self._embedder.embed_query(query), using=DENSE,
                        limit=prefetch_limit, filter=qdrant_filter,
                    ),
                    Prefetch(
                        query=SparseVector(indices=sparse_indices, values=sparse_values),
                        using=SPARSE, limit=prefetch_limit, filter=qdrant_filter,
                    ),
                ],
                query=FusionQuery(fusion=Fusion.RRF),
                limit=top_k, with_payload=True,
            ).points
        results = []
        for hit in hits:
            payload = dict(hit.payload or {})
            text = payload.pop("text", "")
            # The real document id ("cluster:42") is carried in the payload;
            # hit.id is the blake2b-derived numeric point id Qdrant requires.
            # Returning the numeric one would make this backend's results
            # inconsistent with InMemoryVectorStore's and break any caller
            # that maps a result back to its source entity.
            doc_id = payload.pop("doc_id", None) or str(hit.id)
            doc = RetrievalDocument(id=str(doc_id), text=text, **{k: v for k, v in payload.items() if k in RetrievalDocument.model_fields})
            results.append(SearchResult(document=doc, score=float(hit.score)))
        return results

    def clear(self) -> None:
        # Delete AND recreate. Deleting alone leaves this store pointing at a
        # collection that no longer exists, and the collection was only ever
        # created in __init__ - so every subsequent call 404s.
        #
        # index_all() clears before reindexing, which made a full rebuild
        # self-destructive: it destroyed the index, then failed to write the
        # new one, and left /api/ready reporting the vector store as an error
        # with no collection at all. The in-memory backend hides this, because
        # its clear() just empties two dicts and the store stays usable.
        self._client.delete_collection(self._collection)
        self._ensure_collection()
        # The stats point died with the collection. Dropping the cache is what
        # makes the next upsert take the fit() path rather than merging a fresh
        # corpus into statistics describing one that no longer exists.
        self._bm25 = None

    def count(self) -> int:
        from qdrant_client.models import Filter, HasIdCondition

        # The BM25 statistics live in this collection as a reserved point. It is
        # bookkeeping, not a document, and counting it would make this backend
        # disagree with InMemoryVectorStore by exactly one - the kind of
        # off-by-one that surfaces as a puzzling number on /api/ready.
        return self._client.count(
            self._collection,
            count_filter=Filter(must_not=[HasIdCondition(has_id=[_STATS_POINT_ID])]),
        ).count


def build_vector_store() -> VectorStore:
    settings = get_settings()
    embedder = get_embedder()
    fingerprint = get_embedder_fingerprint()
    if settings.retrieval.backend == "qdrant":
        try:
            return QdrantVectorStore(
                embedder, settings.retrieval.qdrant_url, settings.retrieval.qdrant_api_key,
                settings.retrieval.collection, settings.retrieval.embedding_dimensions, fingerprint=fingerprint,
            )
        except Exception:
            # Qdrant unreachable (e.g. Docker not running locally) - fall back
            # to the in-memory backend rather than crashing the whole service.
            pass
    return InMemoryVectorStore(embedder, get_settings().memory_store_file(), fingerprint=fingerprint)


@lru_cache(maxsize=1)
def get_vector_store() -> VectorStore:
    return build_vector_store()


def reset_vector_store_cache() -> None:
    get_vector_store.cache_clear()
