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


def changed_since(since, last_id: int = 0, limit: int = 500) -> list[Incident]:
    """One page of rows at or after the cursor ``(since, last_id)``.

    Keyset pagination on ``(OpenedAt, IncidentId)``, not OFFSET and not a bare
    timestamp comparison. Both alternatives are wrong here:

    * ``WHERE OpenedAt > :since`` alone loses rows whenever a page boundary
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
            f"SELECT TOP (:limit) * FROM {T('Incident')} ORDER BY OpenedAt, IncidentId",
            {"limit": limit},
            max_rows=limit,
        )
    else:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('Incident')} "
            f"WHERE OpenedAt > :since OR (OpenedAt = :since AND IncidentId > :last_id) "
            f"ORDER BY OpenedAt, IncidentId",
            {"since": since, "last_id": last_id or 0, "limit": limit},
            max_rows=limit,
        )
    return [Incident(**r) for r in rows]


def closed_since(since, last_id: int = 0, limit: int = 500) -> list[Incident]:
    """One page of incidents *closed* at or after the cursor.

    A second cursor rather than an OR bolted onto changed_since(). Closing an
    incident rewrites its document - incident_document() renders both Status and
    ClosedAt - so a closure has to be indexed, but ClosedAt moves independently
    of OpenedAt and cannot share one keyset ordering with it. Two cursors is the
    honest shape: two orderings, two watermarks, advancing separately.

    An incident opened and closed between the same two runs is returned by both
    cursors. That is harmless: the document id is derived from IncidentId, so the
    second write replaces the first rather than duplicating it, and the cost is
    one extra embedding.

    STILL INVISIBLE: a Status change that closes nothing - Open to InProgress -
    touches neither cursor, because sad.Incident has no UpdatedAt. That is the
    schema limitation named in refresh_index(), not something this query can fix.
    """
    if since is None:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('Incident')} "
            f"WHERE ClosedAt IS NOT NULL ORDER BY ClosedAt, IncidentId",
            {"limit": limit},
            max_rows=limit,
        )
    else:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('Incident')} "
            f"WHERE ClosedAt IS NOT NULL "
            f"AND (ClosedAt > :since OR (ClosedAt = :since AND IncidentId > :last_id)) "
            f"ORDER BY ClosedAt, IncidentId",
            {"since": since, "last_id": last_id or 0, "limit": limit},
            max_rows=limit,
        )
    return [Incident(**r) for r in rows]
