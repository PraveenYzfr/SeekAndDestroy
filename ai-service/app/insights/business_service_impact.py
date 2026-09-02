"""Business-service-level incident impact: volume, severity-weighted impact,
and the "does the leader change" comparison that is usually the real insight.

WHY THIS IS ITS OWN MODULE, NOT JUST A GROUP_BY OPTION
--------------------------------------------------------
"Which business service has the most incidents" is answered by
query_builder.run_query(entity="incident", group_by=["business_service"]) - a
plain, whitelisted count. Two things this module does are not that:

  1. Folding in Criticality (Platinum/Gold/Silver/Bronze) as a WEIGHT. A
     weighted number must never be presented as if it were a count - e7's
     warning: "a weighted number that looks like a count is the kind of
     thing that gets quoted in a meeting and cannot be reconciled
     afterwards." So every weighted figure here carries the raw count and
     the weight used alongside it, never just the product.

  2. Comparing two rollups (overall vs. one severity) to state whether the
     busiest service and the worst-hit one are the same. This is the same
     shape as e7's own finding at the root-cause level - Capacity dominates
     the estate overall (2,418 incidents) while Network dominates Sev1 (194)
     - computed here rather than left for a narrator to notice by eye.

THE FAN-OUT CAVEAT (see app.insights.whitelist's business_service join)
--------------------------------------------------------------------------
An application can map to more than one business service. Grouping by
service therefore counts an incident once per service its application maps
to - the sum across services can exceed the ungrouped incident total. That
is a real property of this data, not a bug, and is why totals here are never
compared against Incident's own row count as a correctness check.
"""

from __future__ import annotations

from app.repositories.base import T, fetch_all

MAX_ROWS = 500

#: A judgement call, stated as one rather than hidden - linear 4/3/2/1 rather
#: than something exponential, specifically because a round, easily
#: recomputed-by-hand number survives being quoted in a meeting days later.
#: Override explicitly if a different weighting is ever wanted; never change
#: this default silently, since anyone who saw a prior weighted figure would
#: have no way to know the basis moved.
CRITICALITY_WEIGHTS: dict[str, float] = {"Platinum": 4.0, "Gold": 3.0, "Silver": 2.0, "Bronze": 1.0}


def _service_incident_counts(severity: str | None = None) -> list[dict]:
    """Incidents grouped by the business service their CI maps to (see
    whitelist.py's business_service join for the graph direction). Includes
    only incidents that DO resolve to a service - the NULL group (821 of
    1,200 applications today have no service edge at all) is out of scope
    for a leaderboard, though it is preserved in the whitelist-driven
    business_service dimension for anyone who wants to see it.
    """
    where = "WHERE i.Severity = :severity" if severity else ""
    params = {"severity": severity} if severity else {}
    sql = (
        f"SELECT bs.Name AS ServiceName, COUNT(*) AS IncidentCount "
        f"FROM {T('Incident')} i "
        f"JOIN {T('CiRelationship')} bsrel ON bsrel.ChildCiId = i.CmdbCiId AND bsrel.TypeId = 4 "
        f"JOIN {T('ConfigurationItem')} bs ON bs.CiId = bsrel.ParentCiId AND bs.ClassName = 'cmdb_ci_service' "
        f"{where} GROUP BY bs.Name ORDER BY IncidentCount DESC"
    )
    return fetch_all(sql, params, max_rows=MAX_ROWS)


def severity_weighted_impact(severity: str | None = None, weights: dict[str, float] | None = None) -> list[dict]:
    """Per business service: the raw incident count AND a criticality-weighted
    score (count * weight) - both always returned together, so a reader (or
    a narrator) can never present the weighted figure as if it were a count.

    A service with a Criticality value this function does not recognise
    (should not happen against the four seeded tiers, but the CMDB has
    gained new CI classes three times in one night) gets weight 0.0 and
    weight_unknown=True, rather than being silently dropped or given a
    default that pretends to be measured data.
    """
    active_weights = weights or CRITICALITY_WEIGHTS
    sql = (
        f"SELECT bs.Name AS ServiceName, bsvc.Criticality, COUNT(*) AS IncidentCount "
        f"FROM {T('Incident')} i "
        f"JOIN {T('CiRelationship')} bsrel ON bsrel.ChildCiId = i.CmdbCiId AND bsrel.TypeId = 4 "
        f"JOIN {T('ConfigurationItem')} bs ON bs.CiId = bsrel.ParentCiId AND bs.ClassName = 'cmdb_ci_service' "
        f"JOIN {T('CiBusinessService')} bsvc ON bsvc.CiId = bs.CiId "
        + ("WHERE i.Severity = :severity " if severity else "")
        + "GROUP BY bs.Name, bsvc.Criticality ORDER BY IncidentCount DESC"
    )
    params = {"severity": severity} if severity else {}
    rows = fetch_all(sql, params, max_rows=MAX_ROWS)

    result = []
    for row in rows:
        criticality = row["Criticality"]
        weight = active_weights.get(criticality)
        count = row["IncidentCount"]
        result.append({
            "business_service": row["ServiceName"],
            "criticality": criticality,
            "incident_count": count,
            "criticality_weight": weight if weight is not None else 0.0,
            "weighted_impact": count * (weight if weight is not None else 0.0),
            "weight_unknown": weight is None,
        })
    return result


def _leader(rows: list[dict]) -> dict | None:
    if not rows:
        return None
    return {"business_service": rows[0]["ServiceName"], "incident_count": rows[0]["IncidentCount"]}


def compare_leaders(overall_leader: dict | None, filtered_leader: dict | None) -> dict:
    """Pure comparison, no SQL - so "does the leader change" is testable
    deterministically against constructed inputs, independent of whatever
    shape tonight's corpus happens to be in. See tests for both a
    must-fire case (leaders differ) and a must-NOT-fire control (leaders
    the same, or one side empty) - a function that only ever reports "yes,
    inverted" has not been shown to work, only to run.
    """
    changes = (
        overall_leader is not None
        and filtered_leader is not None
        and overall_leader["business_service"] != filtered_leader["business_service"]
    )
    return {
        "overall_leader": overall_leader["business_service"] if overall_leader else None,
        "overall_leader_count": overall_leader["incident_count"] if overall_leader else 0,
        "filtered_leader": filtered_leader["business_service"] if filtered_leader else None,
        "filtered_leader_count": filtered_leader["incident_count"] if filtered_leader else 0,
        "leader_changes": changes,
    }


def volume_vs_severity_leader(top_severity: str = "Sev1") -> dict:
    """Which business service has the most incidents overall, versus which
    has the most at ``top_severity`` - and whether that is the same service.
    The comparison itself, computed here rather than left for a narrator to
    notice by eye, is usually the actual finding (see module docstring).
    """
    overall = _leader(_service_incident_counts())
    filtered = _leader(_service_incident_counts(severity=top_severity))
    result = compare_leaders(overall, filtered)
    result["top_severity"] = top_severity
    return result
