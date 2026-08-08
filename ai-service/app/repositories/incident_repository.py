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
