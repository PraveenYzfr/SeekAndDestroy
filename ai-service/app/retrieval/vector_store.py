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
    suffix = hashlib.blake2b(fingerprint.encode("utf-8"), digest_size=4).hexdigest()
    return f"{base_collection}__{suffix}"


class QdrantVectorStore:
    def __init__(
        self, embedder: Embeddings, url: str, api_key: str, collection: str, dimensions: int, fingerprint: str = ""
    ):
        from qdrant_client import QdrantClient
        from qdrant_client.models import Distance, VectorParams

        self._embedder = embedder
        self._collection = _fingerprinted_collection_name(collection, fingerprint) if fingerprint else collection
        self._client = QdrantClient(url=url, api_key=api_key or None)
        existing = [c.name for c in self._client.get_collections().collections]
        if self._collection not in existing:
            stale = [c for c in existing if c == collection or c.startswith(f"{collection}__")]
            if stale:
                logger.warning(
                    "vector_store.qdrant_fingerprint_changed_new_collection",
                    old_collections=stale, new_collection=self._collection,
                )
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=dimensions, distance=Distance.COSINE),
            )

    @staticmethod
    def _point_id(doc_id: str) -> int:
        import hashlib

        return int(hashlib.blake2b(doc_id.encode("utf-8"), digest_size=8).hexdigest(), 16) % (2**63)

    def upsert(self, documents: list[RetrievalDocument]) -> None:
        from qdrant_client.models import PointStruct

        if not documents:
            return
        vectors = self._embedder.embed_documents([d.text for d in documents])
        points = [
            PointStruct(
                id=self._point_id(doc.id),
                vector=vec,
                payload={"doc_id": doc.id, "text": doc.text, **doc.metadata()},
            )
            for doc, vec in zip(documents, vectors)
        ]
        self._client.upsert(collection_name=self._collection, points=points)

    def delete(self, ids: list[str]) -> None:
        self._client.delete(collection_name=self._collection, points_selector=[self._point_id(i) for i in ids])

    def search(self, query: str, *, top_k: int = 8, filters: dict | None = None) -> list[SearchResult]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        query_vec = self._embedder.embed_query(query)
        qdrant_filter = None
        if filters:
            conditions = [
                FieldCondition(key=k, match=MatchValue(value=v)) for k, v in filters.items() if v is not None
            ]
            if conditions:
                qdrant_filter = Filter(must=conditions)
        # query_points, not the older search(): qdrant-client removed
        # QdrantClient.search/search_batch, so on the pinned 1.18 client the
        # old call raised AttributeError. That failed *silently* in practice -
        # graph.retrieve_related_context catches retrieval errors and degrades
        # to no context - so the symptom was worse answers, never an error.
        hits = self._client.query_points(
            collection_name=self._collection, query=query_vec, limit=top_k, query_filter=qdrant_filter,
            with_payload=True,
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
        self._client.delete_collection(self._collection)

    def count(self) -> int:
        return self._client.count(self._collection).count


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
