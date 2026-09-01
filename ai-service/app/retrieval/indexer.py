"""Builds and maintains the Qdrant/in-memory index over CMDB + capacity data."""

from __future__ import annotations

import structlog

from app.repositories import (
    application_repository,
    cluster_repository,
    dependency_repository,
    incident_repository,
    node_repository,
)
from app.retrieval import documents
from app.retrieval.vector_store import get_vector_store
from app.services import capacity

logger = structlog.get_logger(__name__)

STANDARDS = [
    (
        "environment-separation",
        "Environment separation standard",
        "Production workloads must run only on Production-tier infrastructure. Non-production "
        "environments (Staging, Test, Development) must match exactly; cross-tier placement is never "
        "permitted regardless of available capacity.",
    ),
    (
        "data-classification",
        "Data classification and hosting standard",
        "Infrastructure must be certified at or above the data classification of any workload it hosts: "
        "Public < Internal < Confidential < Restricted. Restricted-classification workloads with a "
        "preferred location must remain in that location.",
    ),
    (
        "resiliency-tiering",
        "Resiliency tiering standard",
        "Critical-criticality applications require Tier-1 infrastructure with at least 3 active nodes. "
        "High-criticality applications require Tier-1 or Tier-2 infrastructure with at least 2 active "
        "nodes, preserving N-1 failure tolerance at all times.",
    ),
    (
        "headroom-thresholds",
        "Capacity headroom thresholds",
        "After any placement, projected utilization must remain below 75% CPU, 80% memory and 85% "
        "storage by default. These thresholds are configurable via policy settings.",
    ),
]


def reindex_application(application_id: int) -> None:
    app = application_repository.get_by_id(application_id)
    if app is None:
        return
    store = get_vector_store()
    store.upsert([documents.application_document(app)])


def reindex_cluster(cluster_id: int) -> None:
    cluster = cluster_repository.get_by_id(cluster_id)
    if cluster is None:
        return
    snapshot = capacity.compute_cluster_capacity(cluster)
    store = get_vector_store()
    store.upsert(
        [
            documents.cluster_document(
                cluster, current_cpu_percent=float(snapshot.current_cpu_utilization_percent),
                current_memory_percent=float(snapshot.current_memory_utilization_percent),
            )
        ]
    )


def delete_document(document_id: str) -> None:
    get_vector_store().delete([document_id])


def index_all() -> int:
    """Full rebuild: clear the collection and index everything.

    Now a thin wrapper over the same batched, checkpointing pipeline a refresh
    uses. It is not a separate implementation any more, which is the point - the
    previous version built the whole corpus in memory and wrote it in one call,
    so the two paths could drift on what a document contained or which rows were
    in scope, and only one of them was resumable.

    Kept synchronous for tests and for `docker exec`. The API enqueues instead:
    see app/retrieval/worker.py.
    """
    from app.retrieval import pipeline

    result = pipeline.execute("rebuild")
    logger.info("retrieval.index_all", document_count=result["documents_indexed"])
    return result["documents_indexed"]


def refresh_index() -> dict:
    """Differential index: only what changed since each source's watermark.

    Synchronous, for tests and `docker exec`. In production the API enqueues a
    run and the worker executes it, so a caller never holds an HTTP connection
    open for the length of an index.
    """
    from app.retrieval import pipeline

    return pipeline.execute("refresh")
