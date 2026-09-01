from __future__ import annotations

from app.models.entities import ApplicationHosting
from app.models.enums import ACTIVE_HOSTING_STATES
from app.repositories.base import T, fetch_all


def get_active_for_application(application_id: int) -> list[ApplicationHosting]:
    rows = fetch_all(
        f"SELECT * FROM {T('ApplicationHosting')} "
        f"WHERE ApplicationId = :id AND HostingStatus IN ('Active','Migrating') "
        f"ORDER BY IsPrimary DESC, HostedSince",
        {"id": application_id},
    )
    return [ApplicationHosting(**r) for r in rows]


def get_all_for_application(application_id: int) -> list[ApplicationHosting]:
    rows = fetch_all(
        f"SELECT * FROM {T('ApplicationHosting')} WHERE ApplicationId = :id ORDER BY HostedSince",
        {"id": application_id},
    )
    return [ApplicationHosting(**r) for r in rows]


def get_active_for_cluster(cluster_id: int, limit: int = 500) -> list[ApplicationHosting]:
    rows = fetch_all(
        f"SELECT TOP (:limit) * FROM {T('ApplicationHosting')} "
        f"WHERE ClusterId = :cluster_id AND HostingStatus IN ('Active','Migrating') "
        f"ORDER BY HostedSince",
        {"cluster_id": cluster_id, "limit": limit},
    )
    return [ApplicationHosting(**r) for r in rows]


def get_active_for_node(node_id: int, limit: int = 200) -> list[ApplicationHosting]:
    rows = fetch_all(
        f"SELECT TOP (:limit) * FROM {T('ApplicationHosting')} "
        f"WHERE NodeId = :node_id AND HostingStatus IN ('Active','Migrating')",
        {"node_id": node_id, "limit": limit},
    )
    return [ApplicationHosting(**r) for r in rows]


assert ACTIVE_HOSTING_STATES == frozenset({"Active", "Migrating"})


def changed_since(since, limit: int = 20000) -> list[ApplicationHosting]:
    """Hosting rows updated after ``since``; all of them when ``since`` is None."""
    if since is None:
        rows = fetch_all(f"SELECT TOP (:limit) * FROM {T('ApplicationHosting')} ORDER BY UpdatedAt", {"limit": limit}, max_rows=limit)
    else:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('ApplicationHosting')} WHERE UpdatedAt > :since ORDER BY UpdatedAt",
            {"since": since, "limit": limit},
            max_rows=limit,
        )
    return [ApplicationHosting(**r) for r in rows]
