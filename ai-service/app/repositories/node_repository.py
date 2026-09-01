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


def changed_since(since, last_id: int = 0, limit: int = 500) -> list[ClusterNode]:
    """One page of rows at or after the cursor ``(since, last_id)``.

    Keyset pagination on ``(UpdatedAt, NodeId)``, not OFFSET and not a bare
    timestamp comparison. Both alternatives are wrong here:

    * ``WHERE UpdatedAt > :since`` alone loses rows whenever a page boundary
      falls inside a group sharing one timestamp - the next query excludes the
      whole group, so those rows are skipped permanently rather than late.
    * OFFSET re-scans everything it has already skipped, so the last page of a
      large corpus costs the most exactly when the run is most likely to be
      interrupted.

    The cursor is exact, which is what lets the caller persist it after every
    batch and resume from it rather than restarting.

    ``since=None`` means "never indexed": the first page starts at the beginning.
    """
    if since is None:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('ClusterNode')} ORDER BY UpdatedAt, NodeId",
            {"limit": limit},
            max_rows=limit,
        )
    else:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('ClusterNode')} "
            f"WHERE UpdatedAt > :since OR (UpdatedAt = :since AND NodeId > :last_id) "
            f"ORDER BY UpdatedAt, NodeId",
            {"since": since, "last_id": last_id or 0, "limit": limit},
            max_rows=limit,
        )
    return [ClusterNode(**r) for r in rows]
