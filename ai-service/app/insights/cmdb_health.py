"""CMDB health: completeness, staleness, orphans, duplicates, coverage.

Every check here is scored per CI class and per support group, never as a
flat estate-wide percentage or a raw list of bad rows - "38% of authentication
servers are missing an owner" is a finding a support-group lead can act on;
"1,412 CIs have a problem" is not.

CLASS NAMES ARE DATA, NEVER HARDCODED
--------------------------------------
sad.ConfigurationItem.ClassName is a CHECK-constrained column, but the set of
values it accepts is still growing (migration 008 shipped nine classes;
migration 009 adds more the same night). A health report that iterates a
Python-side list of "the classes I know about" silently drops every class
added after that list was written - exactly the failure mode worth designing
against, since the whole point of a completeness report is to not miss
things. Every function below discovers classes from the data
(``GROUP BY ClassName`` / ``SELECT DISTINCT``), never from a literal.

GENERIC ON PURPOSE
-------------------
These checks are not shaped to find the specific defects anyone plants in the
seed for testing. A completeness check that only looks for the columns known
to be planted-empty would find those and miss a real gap it was never told
about. Real material already exists without any planting: the migration 008
backfill deliberately left OwnedById/ManagedById/RegulatoryScope null
wherever no existing column could prove a value, so these checks find real
findings about the estate from the first query, not just fixtures.
"""

from __future__ import annotations

from app.repositories.base import T, fetch_all

#: A GROUP BY ClassName (or ClassName x SupportGroup) over the CI estate
#: returns at most a few hundred rows even at tens of thousands of CIs -
#: bounded explicitly rather than left to settings.service.max_rows's
#: endpoint-wide default (see app.insights.query_builder for the same
#: reasoning).
MAX_HEALTH_ROWS = 2_000

#: Columns whose absence on a CI is a real completeness gap, independent of
#: class. Deliberately excludes columns that are legitimately optional for
#: most classes (RegulatoryScope only applies to CIs actually in scope for
#: SOX/PCI) - those are reported separately, not folded into a "completeness"
#: figure that would flag every non-regulated CI as defective.
COMPLETENESS_FIELDS: tuple[str, ...] = ("OwnedById", "ManagedById", "SupportGroupId", "Environment", "DataClassification")


def known_classes() -> list[str]:
    """Every ClassName actually present right now.

    Never hardcode this list elsewhere - see module docstring. Callers that
    need "which classes exist" (a UI dropdown, a report header) should call
    this, not maintain their own copy.
    """
    rows = fetch_all(f"SELECT DISTINCT ClassName FROM {T('ConfigurationItem')} ORDER BY ClassName", max_rows=200)
    return [r["ClassName"] for r in rows]


def completeness_by_class() -> list[dict]:
    """For each CI class: total CIs, and how many are missing each of
    COMPLETENESS_FIELDS. A class with zero CIs never appears - there is
    nothing to report completeness of."""
    case_columns = ", ".join(
        f"SUM(CASE WHEN {field} IS NULL THEN 1 ELSE 0 END) AS Missing{field}" for field in COMPLETENESS_FIELDS
    )
    sql = (
        f"SELECT ClassName, COUNT(*) AS TotalCis, {case_columns} "
        f"FROM {T('ConfigurationItem')} GROUP BY ClassName ORDER BY ClassName"
    )
    return fetch_all(sql, max_rows=MAX_HEALTH_ROWS)


def completeness_by_support_group() -> list[dict]:
    """Same fields, grouped by owning support group instead of class - which
    team's CIs are worst-documented, independent of what kind of CI they are.

    LEFT JOIN, not INNER: a CI with no SupportGroupId at all is itself a
    completeness gap and must appear (grouped under NULL), not be silently
    excluded by an inner join that requires the very field being measured.
    """
    fields = [f for f in COMPLETENESS_FIELDS if f != "SupportGroupId"]
    case_columns = ", ".join(f"SUM(CASE WHEN ci.{field} IS NULL THEN 1 ELSE 0 END) AS Missing{field}" for field in fields)
    sql = (
        f"SELECT sg.GroupName, COUNT(*) AS TotalCis, {case_columns} "
        f"FROM {T('ConfigurationItem')} ci LEFT JOIN {T('SupportGroup')} sg ON sg.SupportGroupId = ci.SupportGroupId "
        f"GROUP BY sg.GroupName ORDER BY TotalCis DESC"
    )
    return fetch_all(sql, max_rows=MAX_HEALTH_ROWS)


def staleness_by_class(stale_after_days: int = 90) -> list[dict]:
    """CIs whose LastDiscovered is older than stale_after_days, OR null
    (never discovered at all - the more severe case of the same problem, not
    a different one)."""
    sql = (
        f"SELECT ClassName, COUNT(*) AS TotalCis, "
        f"SUM(CASE WHEN LastDiscovered IS NULL "
        f"          OR LastDiscovered < DATEADD(day, -:stale_after_days, SYSUTCDATETIME()) "
        f"         THEN 1 ELSE 0 END) AS StaleCis "
        f"FROM {T('ConfigurationItem')} GROUP BY ClassName ORDER BY ClassName"
    )
    return fetch_all(sql, {"stale_after_days": stale_after_days}, max_rows=MAX_HEALTH_ROWS)


def orphans_by_class() -> list[dict]:
    """CIs that appear in sad.CiRelationship neither as a parent nor a child
    - infrastructure the graph does not know is connected to anything.

    A data centre or a stand-alone monitoring box with a genuinely flat
    topology could show up here without being a defect; that judgement is
    for the reader, which is why this reports a count, not a verdict.
    """
    sql = (
        f"SELECT ci.ClassName, COUNT(*) AS OrphanCis "
        f"FROM {T('ConfigurationItem')} ci "
        f"WHERE NOT EXISTS (SELECT 1 FROM {T('CiRelationship')} r WHERE r.ParentCiId = ci.CiId) "
        f"  AND NOT EXISTS (SELECT 1 FROM {T('CiRelationship')} r WHERE r.ChildCiId = ci.CiId) "
        f"GROUP BY ci.ClassName ORDER BY OrphanCis DESC"
    )
    return fetch_all(sql, max_rows=MAX_HEALTH_ROWS)


def unhosted_application_breakdown() -> dict:
    """Applications with no sad.ApplicationHosting row, split by whether they
    still carry a CiRelationship edge (typically a Depends-on link to or from
    another application) or none at all.

    "65 orphaned applications" reads as a defect. It usually is not one here:
    pack_applications() deliberately refuses to allocate past 97% of a
    cluster's physical cores rather than overcommit hardware, and Staging
    hits that ceiling soonest because non-production clusters are drawn
    smaller. The result - roughly 7% of applications left unhosted rather
    than a corpus quietly allocated past what the hardware has - is a real
    trade-off e7 made on purpose (see SCENARIOS["unhosted_applications"]),
    not a generation bug. A real bank's CMDB carries exactly this kind of
    thing too: decommissioned apps, shadow IT, or a registration made ahead
    of a build that never happened.

    The split matters for a reader: unhosted-and-unconnected is "nobody has
    mapped this to anything at all"; unhosted-but-dependency-linked is
    "we know what this talks to, it just was never placed" - a different,
    smaller finding.
    """
    total = fetch_all(
        f"SELECT COUNT(*) AS N FROM {T('CmdbApplication')} a "
        f"WHERE NOT EXISTS (SELECT 1 FROM {T('ApplicationHosting')} h WHERE h.ApplicationId = a.ApplicationId)"
    )[0]["N"]

    unconnected = fetch_all(
        f"SELECT COUNT(*) AS N FROM {T('CmdbApplication')} a "
        f"JOIN {T('ConfigurationItem')} ci ON ci.CiId = a.CiId "
        f"WHERE NOT EXISTS (SELECT 1 FROM {T('ApplicationHosting')} h WHERE h.ApplicationId = a.ApplicationId) "
        f"  AND NOT EXISTS (SELECT 1 FROM {T('CiRelationship')} r WHERE r.ParentCiId = ci.CiId) "
        f"  AND NOT EXISTS (SELECT 1 FROM {T('CiRelationship')} r WHERE r.ChildCiId = ci.CiId)"
    )[0]["N"]

    return {
        "total_unhosted": total,
        "unhosted_and_unconnected": unconnected,
        "unhosted_but_dependency_linked": total - unconnected,
    }


def duplicates_by_class() -> list[dict]:
    """Same ClassName + Name, different SysId - two CI rows the discovery
    process created for what should be one real thing.

    SysId is the actual identity key (see migration 008); a Name collision
    within a class is the observable symptom of that identity breaking, not
    a coincidence to explain away.
    """
    sql = (
        f"SELECT ClassName, Name, COUNT(*) AS DuplicateCount "
        f"FROM {T('ConfigurationItem')} GROUP BY ClassName, Name HAVING COUNT(*) > 1 "
        f"ORDER BY DuplicateCount DESC"
    )
    return fetch_all(sql, max_rows=MAX_HEALTH_ROWS)


def completeness_by_server_role() -> list[dict]:
    """Completeness for actual server hardware, broken down by
    CiServer.ServerRole rather than by class - every server-class CI shares
    one ClassName, so role (hypervisor, domain controller, storage,
    monitoring, ...) is what actually distinguishes one server population's
    ownership gaps from another's.

    JOINS TO sad.CiServer, NOT sad.ClusterNode - migration 011 split "a
    machine's membership in a cluster" (ClusterNode, class
    cmdb_ci_cluster_node) from "the machine" (CiServer, class
    cmdb_ci_server). A membership record having no owner is not a finding; a
    physical machine having none is. ClusterNode still carries a legacy
    ServerRole column from migration 009, predating that split - it is
    unused here on purpose, since a node CI's completeness is already
    covered generically under its own class in completeness_by_class().

    INNER JOIN is deliberate: this reports on servers, so a CI with no
    CiServer row at all is out of scope for this particular breakdown, not a
    gap within it.
    """
    case_columns = ", ".join(f"SUM(CASE WHEN ci.{field} IS NULL THEN 1 ELSE 0 END) AS Missing{field}" for field in COMPLETENESS_FIELDS)
    sql = (
        f"SELECT srv.ServerRole, COUNT(*) AS TotalCis, {case_columns} "
        f"FROM {T('ConfigurationItem')} ci JOIN {T('CiServer')} srv ON srv.CiId = ci.CiId "
        f"GROUP BY srv.ServerRole ORDER BY TotalCis DESC"
    )
    return fetch_all(sql, max_rows=MAX_HEALTH_ROWS)


def completeness_by_zone_type() -> list[dict]:
    """Completeness for zone-class CIs, broken down by Neighborhood.ZoneType
    (Compute/Storage/Core/Network/Management) - a network zone's gaps are a
    different finding from a compute zone's, even though both are
    'cmdb_ci_zone'.
    """
    case_columns = ", ".join(f"SUM(CASE WHEN ci.{field} IS NULL THEN 1 ELSE 0 END) AS Missing{field}" for field in COMPLETENESS_FIELDS)
    sql = (
        f"SELECT zone.ZoneType, COUNT(*) AS TotalCis, {case_columns} "
        f"FROM {T('ConfigurationItem')} ci JOIN {T('Neighborhood')} zone ON zone.CiId = ci.CiId "
        f"GROUP BY zone.ZoneType ORDER BY TotalCis DESC"
    )
    return fetch_all(sql, max_rows=MAX_HEALTH_ROWS)


def coverage_by_class() -> list[dict]:
    """Per class: total CIs, how many have never been the subject of an
    incident, and how many have - the second figure is what tells a reader
    whether "no incidents" is a finding or the class's normal state.

    THIS CANNOT BE READ AS A FLAT PERCENTAGE ACROSS CLASSES
    ---------------------------------------------------------
    Incidents in this estate are seeded against applications and clusters;
    infrastructure classes added by migration 009 (storage arrays, volumes,
    network devices, and soon ~30,000 VMs) structurally never carry one. A
    class where CisWithIncidents is 0 across the board is not a coverage
    gap - it is a class this estate has never generated an incident against
    at all, which is empirically visible right here (CisWithIncidents == 0
    for every row of that class) rather than assumed from a hardcoded list
    of "classes that get incidents". Report and read this per class; a
    blended "90% of CIs have no incidents" headline across all classes would
    look alarming the moment the VM layer lands and mean nothing.
    """
    sql = (
        f"SELECT ci.ClassName, COUNT(*) AS TotalCis, "
        f"SUM(CASE WHEN t.CiId IS NULL THEN 1 ELSE 0 END) AS CisWithNoIncidents, "
        f"SUM(CASE WHEN t.CiId IS NOT NULL THEN 1 ELSE 0 END) AS CisWithIncidents "
        f"FROM {T('ConfigurationItem')} ci "
        f"LEFT JOIN (SELECT DISTINCT CiId FROM {T('TaskCi')} WHERE TaskType = 'Incident') t ON t.CiId = ci.CiId "
        f"GROUP BY ci.ClassName ORDER BY ci.ClassName"
    )
    return fetch_all(sql, max_rows=MAX_HEALTH_ROWS)


def health_report(stale_after_days: int = 90) -> dict:
    """All five checks, one call - the shape a narrator or an API route would
    actually want, rather than five separate round trips."""
    return {
        "classes": known_classes(),
        "completeness_by_class": completeness_by_class(),
        "completeness_by_support_group": completeness_by_support_group(),
        "completeness_by_server_role": completeness_by_server_role(),
        "completeness_by_zone_type": completeness_by_zone_type(),
        "staleness_by_class": staleness_by_class(stale_after_days),
        "stale_after_days": stale_after_days,
        "orphans_by_class": orphans_by_class(),
        "unhosted_application_breakdown": unhosted_application_breakdown(),
        "duplicates_by_class": duplicates_by_class(),
        "coverage_by_class": coverage_by_class(),
    }
