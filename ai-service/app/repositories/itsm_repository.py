"""Changes, problems, and the comments that hang off tickets.

COMMENTS ARE FETCHED PER PAGE, NOT PER TICKET
---------------------------------------------
The obvious shape - for each incident, fetch its comments - is 10,000 round
trips per index run, each returning nine rows. At roughly 8ms per query that is
eighty seconds of latency doing nothing but waiting, and it scales with the
corpus rather than with what changed.

Instead the pipeline reads a page of incidents and asks for every comment
belonging to that page in one query. 500 incidents, one round trip, ~4,500
rows. The IN list is bounded by the page size, which is bounded by BATCH_SIZE,
so the query plan stays stable no matter how large the corpus grows.
"""

from __future__ import annotations

from app.models.entities import Change, IncidentComment, Problem
from app.repositories.base import T, fetch_all


# =============================================================================
# Comments
# =============================================================================
def incident_comments_for(incident_ids: list[int]) -> dict[int, list]:
    """Every comment for a page of incidents, grouped by incident id.

    Ordered by Sequence, not CreatedAt: two notes can share a timestamp to the
    millisecond, and a chunk's context prefix says "note 7/11", which needs a
    stable order rather than a plausible one.
    """
    if not incident_ids:
        return {}
    # Parameter names are generated, not interpolated values - the ids are bound
    # individually. SQL Server caps a statement at 2,100 parameters, and a page
    # is 500, so this stays well inside it.
    names = ", ".join(f":i{n}" for n in range(len(incident_ids)))
    params = {f"i{n}": v for n, v in enumerate(incident_ids)}
    rows = fetch_all(
        f"SELECT IncidentId, Sequence, CreatedAt, CreatedBy, Type, Text "
        f"FROM {T('IncidentComment')} WHERE IncidentId IN ({names}) "
        f"ORDER BY IncidentId, Sequence",
        params,
        max_rows=len(incident_ids) * 40,
    )
    grouped: dict[int, list] = {}
    for r in rows:
        grouped.setdefault(r["IncidentId"], []).append(r)
    return grouped


def change_comments_for(change_ids: list[int]) -> dict[int, list]:
    if not change_ids:
        return {}
    names = ", ".join(f":c{n}" for n in range(len(change_ids)))
    params = {f"c{n}": v for n, v in enumerate(change_ids)}
    rows = fetch_all(
        f"SELECT ChangeId, Sequence, CreatedAt, CreatedBy, Type, Text "
        f"FROM {T('ChangeComment')} WHERE ChangeId IN ({names}) "
        f"ORDER BY ChangeId, Sequence",
        params,
        max_rows=len(change_ids) * 20,
    )
    grouped: dict[int, list] = {}
    for r in rows:
        grouped.setdefault(r["ChangeId"], []).append(r)
    return grouped


# =============================================================================
# Changes
# =============================================================================
def changes_changed_since(since, last_id: int = 0, limit: int = 500) -> list[Change]:
    """One page of changes at or after the cursor, keyset on (UpdatedAt, ChangeId)."""
    if since is None:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('Change')} ORDER BY UpdatedAt, ChangeId",
            {"limit": limit}, max_rows=limit)
    else:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('Change')} "
            f"WHERE UpdatedAt > :since OR (UpdatedAt = :since AND ChangeId > :last_id) "
            f"ORDER BY UpdatedAt, ChangeId",
            {"since": since, "last_id": last_id or 0, "limit": limit}, max_rows=limit)
    return [Change(**r) for r in rows]


def problems_changed_since(since, last_id: int = 0, limit: int = 500) -> list[Problem]:
    if since is None:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('Problem')} ORDER BY UpdatedAt, ProblemId",
            {"limit": limit}, max_rows=limit)
    else:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('Problem')} "
            f"WHERE UpdatedAt > :since OR (UpdatedAt = :since AND ProblemId > :last_id) "
            f"ORDER BY UpdatedAt, ProblemId",
            {"since": since, "last_id": last_id or 0, "limit": limit}, max_rows=limit)
    return [Problem(**r) for r in rows]


# =============================================================================
# Change risk - the placement signals
# =============================================================================
def change_risk_for_clusters(cluster_ids: list[int], *, upcoming_days: int = 14,
                             history_days: int = 90) -> dict[int, dict]:
    """Upcoming changes, recent volume and failure rate, per cluster.

    One query for every candidate rather than one per candidate: a placement run
    scores up to 256 clusters, and this is read once per candidate per run.

    ``upcoming_days`` is the window in which a scheduled change disqualifies a
    cluster. Fourteen days is a judgement, not a fact: too short and a workload
    lands days before its new home is taken down for maintenance; too long and
    an estate with hundreds of planned changes has nothing eligible left.
    """
    if not cluster_ids:
        return {}
    names = ", ".join(f":k{n}" for n in range(len(cluster_ids)))
    params = {f"k{n}": v for n, v in enumerate(cluster_ids)}
    params.update({"upcoming_days": upcoming_days, "history_days": history_days})
    rows = fetch_all(
        f"""
        SELECT c.ClusterId,
               SUM(CASE WHEN c.State = 'Scheduled'
                         AND c.PlannedStart >= SYSUTCDATETIME()
                         AND c.PlannedStart <= DATEADD(day, :upcoming_days, SYSUTCDATETIME())
                        THEN 1 ELSE 0 END)                                   AS UpcomingChanges,
               SUM(CASE WHEN c.ActualEnd >= DATEADD(day, -:history_days, SYSUTCDATETIME())
                        THEN 1 ELSE 0 END)                                   AS RecentChanges,
               SUM(CASE WHEN c.ActualEnd >= DATEADD(day, -:history_days, SYSUTCDATETIME())
                         AND c.CloseCode IN ('Failed', 'BackedOut')
                        THEN 1 ELSE 0 END)                                   AS RecentFailures,
               MAX(CASE WHEN c.FreezeUntil > SYSUTCDATETIME()
                        THEN c.FreezeUntil END)                              AS FreezeUntil
          FROM {T('Change')} AS c
         WHERE c.ClusterId IN ({names})
         GROUP BY c.ClusterId
        """,
        params,
        max_rows=len(cluster_ids) + 1,
    )
    result = {}
    for r in rows:
        recent = int(r["RecentChanges"] or 0)
        failures = int(r["RecentFailures"] or 0)
        result[r["ClusterId"]] = {
            "upcoming_changes": int(r["UpcomingChanges"] or 0),
            "recent_changes": recent,
            "recent_failures": failures,
            # Rate, not count. A cluster with 40 changes and 4 failures is
            # healthier than one with 5 changes and 4 failures, and a raw count
            # would rank them the other way round - punishing the cluster that
            # is simply worked on more often.
            "failure_rate": (failures / recent) if recent else 0.0,
            "freeze_until": r["FreezeUntil"],
        }
    for cid in cluster_ids:
        result.setdefault(cid, {"upcoming_changes": 0, "recent_changes": 0,
                                "recent_failures": 0, "failure_rate": 0.0,
                                "freeze_until": None})
    return result
