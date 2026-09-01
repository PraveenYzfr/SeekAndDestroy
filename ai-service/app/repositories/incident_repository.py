from __future__ import annotations

from app.models.entities import Incident
from app.repositories.base import T, fetch_all


def get_recent_for_application(application_id: int, days: int = 90, limit: int = 200) -> list[Incident]:
    rows = fetch_all(
        f"""
        SELECT TOP (:limit) * FROM {T('Incident')}
        WHERE ApplicationId = :id
          AND OpenedAt >= (
              SELECT DATEADD(day, -:days, MAX(OpenedAt)) FROM {T('Incident')}
          )
        ORDER BY OpenedAt DESC
        """,
        {"id": application_id, "days": days, "limit": limit},
    )
    return [Incident(**r) for r in rows]


def get_recent_for_cluster(cluster_id: int, days: int = 90, limit: int = 200) -> list[Incident]:
    rows = fetch_all(
        f"""
        SELECT TOP (:limit) * FROM {T('Incident')}
        WHERE ClusterId = :id
          AND OpenedAt >= (
              SELECT DATEADD(day, -:days, MAX(OpenedAt)) FROM {T('Incident')}
          )
        ORDER BY OpenedAt DESC
        """,
        {"id": cluster_id, "days": days, "limit": limit},
    )
    return [Incident(**r) for r in rows]


def get_recent_for_node(node_id: int, days: int = 90, limit: int = 200) -> list[Incident]:
    rows = fetch_all(
        f"""
        SELECT TOP (:limit) * FROM {T('Incident')}
        WHERE NodeId = :id
          AND OpenedAt >= (
              SELECT DATEADD(day, -:days, MAX(OpenedAt)) FROM {T('Incident')}
          )
        ORDER BY OpenedAt DESC
        """,
        {"id": node_id, "days": days, "limit": limit},
    )
    return [Incident(**r) for r in rows]


def get_open_severe_for_node(node_id: int) -> list[Incident]:
    rows = fetch_all(
        f"SELECT * FROM {T('Incident')} WHERE NodeId = :id "
        f"AND Status IN ('Open','InProgress') AND Severity IN ('Sev1','Sev2')",
        {"id": node_id},
    )
    return [Incident(**r) for r in rows]


def get_open_severe_for_cluster(cluster_id: int) -> list[Incident]:
    rows = fetch_all(
        f"SELECT * FROM {T('Incident')} WHERE ClusterId = :id "
        f"AND Status IN ('Open','InProgress') AND Severity IN ('Sev1','Sev2')",
        {"id": cluster_id},
    )
    return [Incident(**r) for r in rows]


def changed_since(since, limit: int = 20000) -> list[Incident]:
    """Incidents opened or closed after ``since``; all of them when None.

    KNOWN BLIND SPOT: sad.Incident has no UpdatedAt. An edit that changes
    Severity, Status or RootCauseCategory without touching OpenedAt or ClosedAt
    is invisible here and will not be re-indexed until the next full rebuild.
    Adding UpdatedAt to the table is the fix; until then this is a limitation of
    the schema being worked around, not a complete change feed.
    """
    if since is None:
        rows = fetch_all(f"SELECT TOP (:limit) * FROM {T('Incident')} ORDER BY OpenedAt", {"limit": limit}, max_rows=limit)
    else:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('Incident')} "
            f"WHERE OpenedAt > :since OR (ClosedAt IS NOT NULL AND ClosedAt > :since) ORDER BY OpenedAt",
            {"since": since, "limit": limit},
            max_rows=limit,
        )
    return [Incident(**r) for r in rows]
