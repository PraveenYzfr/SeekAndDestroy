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


def changed_since(since, last_id: int = 0, limit: int = 500) -> list[ApplicationHosting]:
    """One page of rows at or after the cursor ``(since, last_id)``.

    Keyset pagination on ``(UpdatedAt, HostingId)``, not OFFSET and not a bare
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
            f"SELECT TOP (:limit) * FROM {T('ApplicationHosting')} ORDER BY UpdatedAt, HostingId",
            {"limit": limit},
            max_rows=limit,
        )
    else:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('ApplicationHosting')} "
            f"WHERE UpdatedAt > :since OR (UpdatedAt = :since AND HostingId > :last_id) "
            f"ORDER BY UpdatedAt, HostingId",
            {"since": since, "last_id": last_id or 0, "limit": limit},
            max_rows=limit,
        )
    return [ApplicationHosting(**r) for r in rows]
