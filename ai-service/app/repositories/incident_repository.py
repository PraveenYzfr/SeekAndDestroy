from __future__ import annotations

from app.models.entities import Incident, IncidentComment
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


def get_by_number(number: str) -> Incident | None:
    """One incident by its ticket number, or None.

    WHY THIS DID NOT EXIST UNTIL NOW, which is the interesting part.

    "Explain INC1008138" had no engine behind it. There was no way to fetch an
    incident by the identifier a person actually types, so the question fell
    through to retrieval - which found the incident's indexed text, handed it to
    a model, and got a fluent paragraph back. That paragraph was correct. The
    report built around it was not: with no structured evidence, the reporting
    chain was still asked for a recommendation shape and filled
    top_recommendation, risks and next_steps with plausible invention
    ("continue to monitor the link", "update documentation with the hardware
    replacement details") for a ticket closed months earlier.

    This is the shape the plan calls out: a question type with no engine behind
    it degrades to retrieval, and retrieval answers confidently. The fix is not
    a better prompt - it is having a record to answer from.

    Matched case-insensitively and trimmed, because the number arrives from a
    person's typing. Not a LIKE: a number is exact, and a prefix match would
    make INC100 return whatever INC1008138 happened to sort first.
    """
    cleaned = (number or "").strip().upper()
    if not cleaned:
        return None
    rows = fetch_all(
        f"SELECT TOP (2) * FROM {T('Incident')} WHERE UPPER(Number) = :n",
        {"n": cleaned},
    )
    #  Exactly one, or nothing. Number has no unique constraint in the schema,
    #  and answering "here are the facts of INC1008138" from an arbitrary one of
    #  two rows would be worse than declining - the reader cannot tell.
    if len(rows) != 1:
        return None
    return Incident(**rows[0])


def comments_for(incident_id: int, limit: int = 50) -> list[IncidentComment]:
    """The work-note timeline for one incident, oldest first.

    ATTACKER-WRITABLE TEXT. Anyone who can touch a ticket can write these, and
    this platform's whole grounding rule is that a figure in prose is not
    evidence. So these are shown as QUOTED TIMELINE ENTRIES attributed to their
    author, never parsed for facts and never used to derive a field. The
    structured columns on the incident are the record; this is testimony about
    it.
    """
    rows = fetch_all(
        f"""
        SELECT TOP (:limit) * FROM {T('IncidentComment')}
        WHERE IncidentId = :id ORDER BY Sequence ASC
        """,
        {"id": incident_id, "limit": limit},
    )
    return [IncidentComment(**r) for r in rows]
