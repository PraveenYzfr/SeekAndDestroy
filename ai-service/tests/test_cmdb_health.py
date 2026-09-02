"""Tests for app.insights.cmdb_health.

Same discipline as test_insights.py: every assertion checks against SQL
written fresh in the test, or against generate_seed.SCENARIOS once CI-specific
fixtures exist there - never a literal count. The CI estate is scaling up
significantly as this suite is being written (e7: servers 2,007 -> 10,000,
plus a VM layer arriving), so any number here would be stale within hours.
"""

from __future__ import annotations

from app.insights import cmdb_health
from app.repositories.base import T, fetch_all


def test_known_classes_matches_independent_sql():
    expected = {r["ClassName"] for r in fetch_all(f"SELECT DISTINCT ClassName FROM {T('ConfigurationItem')}")}
    assert set(cmdb_health.known_classes()) == expected


def test_known_classes_is_never_a_hardcoded_list():
    """The whole point of known_classes() existing: it must reflect
    whatever the CHECK constraint currently allows, not a snapshot. Proven by
    asserting it agrees with a live query rather than any fixed set of
    class names - if this test named the classes literally, it would stop
    catching a class this module forgot about the same way a hardcoded list
    inside cmdb_health.py itself would."""
    live_classes = {r["ClassName"] for r in fetch_all(f"SELECT DISTINCT ClassName FROM {T('ConfigurationItem')}")}
    assert set(cmdb_health.known_classes()) == live_classes
    assert len(live_classes) > 0  # sanity: migration 008 is applied


def test_completeness_by_class_matches_independent_sql():
    expected_rows = fetch_all(
        f"SELECT ClassName, COUNT(*) AS TotalCis, "
        f"SUM(CASE WHEN OwnedById IS NULL THEN 1 ELSE 0 END) AS MissingOwnedById "
        f"FROM {T('ConfigurationItem')} GROUP BY ClassName"
    )
    expected = {r["ClassName"]: (r["TotalCis"], r["MissingOwnedById"]) for r in expected_rows}

    actual_rows = cmdb_health.completeness_by_class()
    actual = {r["ClassName"]: (r["TotalCis"], r["MissingOwnedById"]) for r in actual_rows}
    assert actual == expected


def test_completeness_by_class_never_drops_a_class_with_gaps():
    """GUARDRAILS-equivalent for this feature: a class where every CI is
    missing a field must still appear (with the gap reported), not be
    silently excluded - completeness reporting exists specifically to
    surface total gaps, which a naive filter (e.g. HAVING Missing > 0 AND
    Missing < Total) could hide."""
    rows = cmdb_health.completeness_by_class()
    by_class = {r["ClassName"]: r for r in rows}
    expected_classes = {r["ClassName"] for r in fetch_all(f"SELECT DISTINCT ClassName FROM {T('ConfigurationItem')}")}
    assert set(by_class) == expected_classes
    for row in rows:
        assert row["MissingOwnedById"] <= row["TotalCis"]


def test_completeness_by_support_group_includes_unassigned_cis():
    """LEFT JOIN, not INNER: a CI with no SupportGroupId is itself a
    completeness gap and must show up (under a NULL GroupName), not be
    silently dropped by requiring the very field being measured."""
    rows = cmdb_health.completeness_by_support_group()
    total_via_report = sum(r["TotalCis"] for r in rows)
    total_cis = fetch_all(f"SELECT COUNT(*) AS N FROM {T('ConfigurationItem')}")[0]["N"]
    assert total_via_report == total_cis


def test_staleness_by_class_matches_independent_sql():
    expected_rows = fetch_all(
        f"SELECT ClassName, COUNT(*) AS TotalCis, "
        f"SUM(CASE WHEN LastDiscovered IS NULL OR LastDiscovered < DATEADD(day, -90, SYSUTCDATETIME()) "
        f"         THEN 1 ELSE 0 END) AS StaleCis "
        f"FROM {T('ConfigurationItem')} GROUP BY ClassName"
    )
    expected = {r["ClassName"]: r["StaleCis"] for r in expected_rows}
    actual = {r["ClassName"]: r["StaleCis"] for r in cmdb_health.staleness_by_class(90)}
    assert actual == expected


def test_staleness_treats_never_discovered_as_stale():
    """A CI with LastDiscovered = NULL has never been seen at all - the more
    severe case of staleness, not a different, unmeasured one. Verified by
    pushing the cutoff far into the FUTURE (a large NEGATIVE stale_after_days,
    since the query computes DATEADD(day, -stale_after_days, now)) so every
    real LastDiscovered value, however recent, falls before the cutoff and
    counts as stale - confirming StaleCis == TotalCis for every class."""
    rows = cmdb_health.staleness_by_class(stale_after_days=-100_000)
    for row in rows:
        assert row["StaleCis"] == row["TotalCis"]


def test_orphans_by_class_matches_independent_sql():
    expected_rows = fetch_all(
        f"SELECT ci.ClassName, COUNT(*) AS OrphanCis FROM {T('ConfigurationItem')} ci "
        f"WHERE NOT EXISTS (SELECT 1 FROM {T('CiRelationship')} r WHERE r.ParentCiId = ci.CiId) "
        f"  AND NOT EXISTS (SELECT 1 FROM {T('CiRelationship')} r WHERE r.ChildCiId = ci.CiId) "
        f"GROUP BY ci.ClassName"
    )
    expected = {r["ClassName"]: r["OrphanCis"] for r in expected_rows}
    actual = {r["ClassName"]: r["OrphanCis"] for r in cmdb_health.orphans_by_class()}
    assert actual == expected


def test_unhosted_application_breakdown_matches_independent_sql():
    expected_total = fetch_all(
        f"SELECT COUNT(*) AS N FROM {T('CmdbApplication')} a "
        f"WHERE NOT EXISTS (SELECT 1 FROM {T('ApplicationHosting')} h WHERE h.ApplicationId = a.ApplicationId)"
    )[0]["N"]
    expected_unconnected = fetch_all(
        f"SELECT COUNT(*) AS N FROM {T('CmdbApplication')} a "
        f"JOIN {T('ConfigurationItem')} ci ON ci.CiId = a.CiId "
        f"WHERE NOT EXISTS (SELECT 1 FROM {T('ApplicationHosting')} h WHERE h.ApplicationId = a.ApplicationId) "
        f"  AND NOT EXISTS (SELECT 1 FROM {T('CiRelationship')} r WHERE r.ParentCiId = ci.CiId) "
        f"  AND NOT EXISTS (SELECT 1 FROM {T('CiRelationship')} r WHERE r.ChildCiId = ci.CiId)"
    )[0]["N"]

    actual = cmdb_health.unhosted_application_breakdown()
    assert actual["total_unhosted"] == expected_total
    assert actual["unhosted_and_unconnected"] == expected_unconnected
    assert actual["unhosted_but_dependency_linked"] == expected_total - expected_unconnected


def test_unhosted_application_breakdown_matches_planted_fixture(scenarios):
    """e7's SCENARIOS["unhosted_applications"] is the guaranteed, exported
    version of this same fact - matching it directly (once the fixture
    exists) is a stronger check than only trusting a second copy of the same
    SQL logic."""
    planted = scenarios.get("unhosted_applications")
    if not planted:
        import pytest

        pytest.skip("SCENARIOS['unhosted_applications'] not exported by this seed yet")
    assert cmdb_health.unhosted_application_breakdown()["total_unhosted"] == len(planted)


def test_duplicates_by_class_matches_independent_sql():
    expected_rows = fetch_all(
        f"SELECT ClassName, Name, COUNT(*) AS N FROM {T('ConfigurationItem')} "
        f"GROUP BY ClassName, Name HAVING COUNT(*) > 1"
    )
    expected = {(r["ClassName"], r["Name"]): r["N"] for r in expected_rows}
    actual = {(r["ClassName"], r["Name"]): r["DuplicateCount"] for r in cmdb_health.duplicates_by_class()}
    assert actual == expected


def test_duplicates_report_is_valid_when_empty():
    """GUARDRAILS: empty result is a valid answer, not an error - a corpus
    with genuinely no duplicate CIs should return [], not raise."""
    rows = cmdb_health.duplicates_by_class()
    assert isinstance(rows, list)  # must not raise regardless of whether any exist


def test_completeness_by_server_role_matches_independent_sql():
    """Must read sad.CiServer, not sad.ClusterNode - migration 011 split
    node (membership) from server (the machine); ClusterNode's own
    ServerRole is a pre-split leftover this function must not use."""
    expected_rows = fetch_all(
        f"SELECT srv.ServerRole, COUNT(*) AS TotalCis, "
        f"SUM(CASE WHEN ci.OwnedById IS NULL THEN 1 ELSE 0 END) AS MissingOwnedById "
        f"FROM {T('ConfigurationItem')} ci JOIN {T('CiServer')} srv ON srv.CiId = ci.CiId "
        f"GROUP BY srv.ServerRole"
    )
    expected = {r["ServerRole"]: (r["TotalCis"], r["MissingOwnedById"]) for r in expected_rows}
    actual = {r["ServerRole"]: (r["TotalCis"], r["MissingOwnedById"]) for r in cmdb_health.completeness_by_server_role()}
    assert actual == expected
    assert sum(v[0] for v in expected.values()) > 0  # sanity: servers are actually linked to CIs


def test_completeness_by_zone_type_matches_independent_sql():
    expected_rows = fetch_all(
        f"SELECT zone.ZoneType, COUNT(*) AS TotalCis, "
        f"SUM(CASE WHEN ci.OwnedById IS NULL THEN 1 ELSE 0 END) AS MissingOwnedById "
        f"FROM {T('ConfigurationItem')} ci JOIN {T('Neighborhood')} zone ON zone.CiId = ci.CiId "
        f"GROUP BY zone.ZoneType"
    )
    expected = {r["ZoneType"]: (r["TotalCis"], r["MissingOwnedById"]) for r in expected_rows}
    actual = {r["ZoneType"]: (r["TotalCis"], r["MissingOwnedById"]) for r in cmdb_health.completeness_by_zone_type()}
    assert actual == expected
    assert sum(v[0] for v in expected.values()) > 0  # sanity: zones are actually linked to CIs


def test_coverage_by_class_matches_independent_sql():
    expected_rows = fetch_all(
        f"SELECT ci.ClassName, COUNT(*) AS TotalCis, "
        f"SUM(CASE WHEN t.CiId IS NULL THEN 1 ELSE 0 END) AS CisWithNoIncidents, "
        f"SUM(CASE WHEN t.CiId IS NOT NULL THEN 1 ELSE 0 END) AS CisWithIncidents "
        f"FROM {T('ConfigurationItem')} ci "
        f"LEFT JOIN (SELECT DISTINCT CiId FROM {T('TaskCi')} WHERE TaskType = 'Incident') t ON t.CiId = ci.CiId "
        f"GROUP BY ci.ClassName"
    )
    expected = {r["ClassName"]: (r["TotalCis"], r["CisWithNoIncidents"], r["CisWithIncidents"]) for r in expected_rows}
    actual = {r["ClassName"]: (r["TotalCis"], r["CisWithNoIncidents"], r["CisWithIncidents"]) for r in cmdb_health.coverage_by_class()}
    assert actual == expected


def test_coverage_by_class_exposes_whether_a_class_ever_receives_incidents():
    """The actual guardrail from e7's warning: once infrastructure classes
    that never carry incidents (storage, network devices, VMs) dominate the
    estate, a reader must be able to tell "this class structurally has none"
    from "this class has a real gap" using the numbers alone - CisWithIncidents
    being 0 for an entire class is that signal, and it must be present and
    correct, not just CisWithNoIncidents."""
    for row in cmdb_health.coverage_by_class():
        assert row["CisWithIncidents"] + row["CisWithNoIncidents"] == row["TotalCis"]


def test_health_report_bundles_every_check():
    report = cmdb_health.health_report()
    for key in (
        "classes", "completeness_by_class", "completeness_by_support_group",
        "completeness_by_server_role", "completeness_by_zone_type",
        "staleness_by_class", "orphans_by_class", "unhosted_application_breakdown",
        "duplicates_by_class", "coverage_by_class",
    ):
        assert key in report
