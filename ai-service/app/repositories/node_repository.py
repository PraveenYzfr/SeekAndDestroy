from __future__ import annotations

from app.models.entities import ClusterNode
from app.repositories.base import T, fetch_all, fetch_one


def get_by_id(node_id: int) -> ClusterNode | None:
    row = fetch_one(f"SELECT * FROM {T('ClusterNode')} WHERE NodeId = :id", {"id": node_id})
    return ClusterNode(**row) if row else None


def get_by_cluster(cluster_id: int, limit: int = 200) -> list[ClusterNode]:
    rows = fetch_all(
        f"SELECT TOP (:limit) * FROM {T('ClusterNode')} WHERE ClusterId = :cluster_id ORDER BY HostName",
        {"cluster_id": cluster_id, "limit": limit},
    )
    return [ClusterNode(**r) for r in rows]


def get_active_by_cluster(cluster_id: int, limit: int = 200) -> list[ClusterNode]:
    rows = fetch_all(
        f"SELECT TOP (:limit) * FROM {T('ClusterNode')} "
        f"WHERE ClusterId = :cluster_id AND LifecycleStatus = 'Active' ORDER BY HostName",
        {"cluster_id": cluster_id, "limit": limit},
    )
    return [ClusterNode(**r) for r in rows]


def count_active_by_cluster(cluster_id: int) -> int:
    row = fetch_one(
        f"SELECT COUNT(*) AS Cnt FROM {T('ClusterNode')} "
        f"WHERE ClusterId = :cluster_id AND LifecycleStatus = 'Active'",
        {"cluster_id": cluster_id},
    )
    return int(row["Cnt"]) if row else 0
