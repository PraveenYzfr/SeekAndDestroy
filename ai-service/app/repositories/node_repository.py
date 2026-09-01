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


def changed_since(since, limit: int = 20000) -> list[ClusterNode]:
    """Nodes updated after ``since``; all of them when ``since`` is None.

    The limit is higher than the other sources on purpose: nodes outnumber
    everything else roughly ten to one, and a truncated node set would silently
    leave the index disagreeing with the CMDB about which hosts exist.
    """
    if since is None:
        rows = fetch_all(f"SELECT TOP (:limit) * FROM {T('ClusterNode')} ORDER BY UpdatedAt", {"limit": limit}, max_rows=limit)
    else:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('ClusterNode')} WHERE UpdatedAt > :since ORDER BY UpdatedAt",
            {"since": since, "limit": limit},
            max_rows=limit,
        )
    return [ClusterNode(**r) for r in rows]
