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
    from datetime import datetime, timezone

    from app.repositories import index_watermark_repository

    store = get_vector_store()
    # Captured before anything is read, not after. A row updated while the
    # rebuild is running would otherwise fall between the data this run saw and
    # the watermark it wrote, and never be indexed by any later refresh. Taking
    # the start time means such a row is re-indexed next refresh: duplicated
    # work, which is cheap, rather than a missed document, which is silent.
    started_at = datetime.now(timezone.utc).replace(tzinfo=None)
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

    # A full rebuild has just indexed everything, so every watermark now means
    # "current as of the moment this run started". Without this the first
    # refresh after a rebuild would see no watermark, treat it as a first run,
    # and re-embed the entire corpus it had just embedded.
    max_dependency_id = max((d.DependencyId for d in dependency_repository.created_after_id(None)), default=None)
    for source in ("application", "node", "cluster", "hosting", "incident"):
        index_watermark_repository.save(
            source, last_seen_at=started_at, last_seen_id=None,
            documents_indexed=len(docs), run_at=started_at,
        )
    index_watermark_repository.save(
        "dependency", last_seen_at=None, last_seen_id=max_dependency_id,
        documents_indexed=len(docs), run_at=started_at,
    )

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


#: How far back incident documents are indexed. index_all() uses the same window
#: (see the get_recent_for_* calls above); a refresh using a different one would
#: add documents that the next full rebuild then silently removed.
_INCIDENT_WINDOW_DAYS = 180


def _newest(values: list):
    real = [v for v in values if v is not None]
    return max(real) if real else None


def refresh_index() -> dict:
    """Index only what changed since the last run. Manual trigger, no schedule.

    index_all() remains the correctness backstop: it clears the collection and
    re-embeds everything, which is what a schema change or a corrupted index
    requires. It is the wrong tool for "three nodes were decommissioned this
    morning", where every unchanged document is re-sent to the embedding
    provider to produce a vector identical to the one already stored - roughly
    2,400 documents at 3072 dimensions to capture three edits.

    This reads each source's watermark, asks that source only for rows newer
    than it, and writes back how far it got. Sources advance independently, so a
    source that fails does not rewind the ones that succeeded.

    WHAT THIS CANNOT SEE, stated here rather than discovered later:

    * Deletes. No source has a soft-delete flag and the schema has no change
      tracking, so a decommissioned node keeps its document and keeps being
      retrieved. Only index_all() removes it, because only index_all() clears.
    * Incident edits. sad.Incident has no UpdatedAt, so a Severity or Status
      change leaving OpenedAt and ClosedAt untouched is invisible.
    * Dependency edits. sad.ApplicationDependency has no timestamp at all and is
      followed by IDENTITY, so inserts are seen and edits never are.

    Those are schema limitations rather than design choices, and they are why a
    periodic full rebuild stays necessary rather than merely available.
    """
    from datetime import datetime, timedelta, timezone

    from app.repositories import hosting_repository, index_watermark_repository

    store = get_vector_store()
    run_at = datetime.now(timezone.utc).replace(tzinfo=None)

    def watermark(source: str):
        row = index_watermark_repository.get(source)
        return (row["LastSeenAt"], row["LastSeenId"]) if row else (None, None)

    # Codes for the entities that documents refer to by name. Both sets are
    # small - a few hundred clusters, a few dozen applications - so loading them
    # whole costs two queries and avoids a lookup per changed row.
    clusters = {c.ClusterId: c for c in cluster_repository.list_all(limit=1000)}
    apps = {a.ApplicationId: a for a in application_repository.list_all(limit=1000)}

    docs: list = []
    counts: dict[str, int] = {}
    dirty_clusters: set[int] = set()

    # --- applications ------------------------------------------------------
    since, _ = watermark("application")
    changed_apps = application_repository.changed_since(since)
    docs += [documents.application_document(a) for a in changed_apps]
    counts["application"] = len(changed_apps)
    app_mark = _newest([a.UpdatedAt for a in changed_apps])

    # --- nodes -------------------------------------------------------------
    # Before clusters, because a node change makes its cluster document stale
    # too: cluster_document embeds live utilisation computed from the nodes, and
    # InfrastructureCluster.UpdatedAt does not move when a node changes. Without
    # this fan-out a decommissioned host leaves the cluster document advertising
    # capacity that no longer exists.
    since, _ = watermark("node")
    changed_nodes = node_repository.changed_since(since)
    for node in changed_nodes:
        cluster = clusters.get(node.ClusterId)
        if cluster:
            docs.append(documents.node_document(node, cluster.ClusterCode))
            dirty_clusters.add(node.ClusterId)
    counts["node"] = len(changed_nodes)
    node_mark = _newest([n.UpdatedAt for n in changed_nodes])

    # --- clusters ----------------------------------------------------------
    since, _ = watermark("cluster")
    changed_clusters = cluster_repository.changed_since(since)
    cluster_mark = _newest([c.UpdatedAt for c in changed_clusters])
    changed_cluster_ids = {c.ClusterId for c in changed_clusters}
    for cluster_id in changed_cluster_ids | dirty_clusters:
        cluster = clusters.get(cluster_id)
        if not cluster:
            continue
        snapshot = capacity.compute_cluster_capacity(cluster)
        docs.append(
            documents.cluster_document(
                cluster,
                current_cpu_percent=float(snapshot.current_cpu_utilization_percent),
                current_memory_percent=float(snapshot.current_memory_utilization_percent),
            )
        )
    counts["cluster"] = len(changed_clusters)
    counts["cluster_via_node_change"] = len(dirty_clusters - changed_cluster_ids)

    # --- hosting -----------------------------------------------------------
    since, _ = watermark("hosting")
    changed_hosting = hosting_repository.changed_since(since)
    for hosting in changed_hosting:
        app = apps.get(hosting.ApplicationId)
        cluster = clusters.get(hosting.ClusterId)
        if app and cluster:
            docs.append(documents.hosting_document(hosting, app.ApplicationCode, cluster.ClusterCode))
    counts["hosting"] = len(changed_hosting)
    hosting_mark = _newest([h.UpdatedAt for h in changed_hosting])

    # --- incidents ---------------------------------------------------------
    since, _ = watermark("incident")
    cutoff = run_at - timedelta(days=_INCIDENT_WINDOW_DAYS)
    changed_incidents = incident_repository.changed_since(since)
    indexed_incidents = 0
    for incident in changed_incidents:
        if incident.OpenedAt < cutoff:
            continue
        if incident.ApplicationId and incident.ApplicationId in apps:
            subject = "application " + apps[incident.ApplicationId].ApplicationCode
        elif incident.ClusterId and incident.ClusterId in clusters:
            subject = "cluster " + clusters[incident.ClusterId].ClusterCode
        else:
            # index_all() only reaches incidents through an application or a
            # cluster. Indexing a node-only incident here would add a document
            # that the next full rebuild then removed.
            continue
        docs.append(documents.incident_document(incident, subject))
        indexed_incidents += 1
    counts["incident"] = indexed_incidents
    incident_mark = _newest(
        [i.OpenedAt for i in changed_incidents] + [i.ClosedAt for i in changed_incidents]
    )

    # --- dependencies ------------------------------------------------------
    _, last_id = watermark("dependency")
    new_deps = dependency_repository.created_after_id(last_id)
    for dep in new_deps:
        source_app = apps.get(dep.SourceApplicationId)
        if not source_app:
            continue
        if dep.TargetApplicationId:
            target = apps.get(dep.TargetApplicationId)
            target_desc = "application " + target.ApplicationCode if target else "an application"
        elif dep.TargetClusterId:
            target_cluster = clusters.get(dep.TargetClusterId)
            target_desc = "cluster " + target_cluster.ClusterCode if target_cluster else "a cluster"
        else:
            target_desc = "an unspecified target"
        docs.append(documents.dependency_document(dep, source_app.ApplicationCode, target_desc))
    counts["dependency"] = len(new_deps)
    dep_mark = max((d.DependencyId for d in new_deps), default=None)

    # --- write -------------------------------------------------------------
    # One upsert for everything, so the embedding provider sees a single batched
    # run rather than six, and a document touched by two sources in the same
    # refresh is embedded once.
    if docs:
        store.upsert(docs)

    # Watermarks last, and only once the upsert has succeeded. Advancing them
    # first would mean a failed embed silently skipped those rows forever - the
    # class of bug where the job reports success and the index falls behind.
    marks = {
        "application": (app_mark, None),
        "node": (node_mark, None),
        "cluster": (cluster_mark, None),
        "hosting": (hosting_mark, None),
        "incident": (incident_mark, None),
        "dependency": (None, dep_mark),
    }
    for source, (at, ident) in marks.items():
        index_watermark_repository.save(
            source,
            last_seen_at=at,
            last_seen_id=ident,
            documents_indexed=counts.get(source, 0),
            run_at=run_at,
        )

    logger.info("retrieval.refresh_index", document_count=len(docs), **counts)
    return {"documents_indexed": len(docs), "by_source": counts}
