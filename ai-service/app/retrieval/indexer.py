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


def index_all() -> int:
    store = get_vector_store()
    store.clear()
    docs = []

    apps = application_repository.list_all(limit=1000)
    for app in apps:
        docs.append(documents.application_document(app))

    clusters = cluster_repository.list_all(limit=1000)
    cluster_by_id = {c.ClusterId: c for c in clusters}
    for cluster in clusters:
        snapshot = capacity.compute_cluster_capacity(cluster)
        docs.append(
            documents.cluster_document(
                cluster,
                current_cpu_percent=float(snapshot.current_cpu_utilization_percent),
                current_memory_percent=float(snapshot.current_memory_utilization_percent),
            )
        )
        for node in node_repository.get_by_cluster(cluster.ClusterId):
            docs.append(documents.node_document(node, cluster.ClusterCode))

    app_by_id = {a.ApplicationId: a for a in apps}
    for app in apps:
        from app.repositories import hosting_repository

        for hosting in hosting_repository.get_all_for_application(app.ApplicationId):
            cluster = cluster_by_id.get(hosting.ClusterId)
            if cluster:
                docs.append(documents.hosting_document(hosting, app.ApplicationCode, cluster.ClusterCode))

        for dep in dependency_repository.get_outbound(app.ApplicationId):
            if dep.TargetApplicationId:
                target = app_by_id.get(dep.TargetApplicationId)
                target_desc = f"application {target.ApplicationCode}" if target else "an application"
            elif dep.TargetClusterId:
                target_cluster = cluster_by_id.get(dep.TargetClusterId)
                target_desc = f"cluster {target_cluster.ClusterCode}" if target_cluster else "a cluster"
            else:
                target_desc = "an unspecified target"
            docs.append(documents.dependency_document(dep, app.ApplicationCode, target_desc))

        for incident in incident_repository.get_recent_for_application(app.ApplicationId, days=180, limit=50):
            docs.append(documents.incident_document(incident, f"application {app.ApplicationCode}"))

    for cluster in clusters:
        for incident in incident_repository.get_recent_for_cluster(cluster.ClusterId, days=180, limit=50):
            docs.append(documents.incident_document(incident, f"cluster {cluster.ClusterCode}"))

    for doc_id, title, text in STANDARDS:
        docs.append(documents.standard_document(doc_id, title, text))

    store.upsert(docs)
    logger.info("retrieval.index_all", document_count=len(docs))
    return len(docs)


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
