"""Tests for app.insights.impact_analysis.

The cycle-safety test at the bottom is the one that matters most: a
traversal bug here does not crash, it returns a blast radius that looks
complete and is wrong (see the module docstring). It is written to run
against e7's planted SCENARIOS["dependency_cycle_applications"] fixture the
moment that lands, and to prove the depth-ceiling mechanism today in the
meantime, rather than skip the property entirely until the fixture exists.
"""

from __future__ import annotations

import pytest

from app.insights import impact_analysis
from app.insights.impact_analysis import UnknownCiError, blast_radius, blast_radius_for_name, resolve_ci_id
from app.repositories.base import T, fetch_all


def test_resolve_ci_id_finds_a_real_cluster():
    cluster = fetch_all(f"SELECT TOP (1) CiId, Name FROM {T('ConfigurationItem')} WHERE ClassName = 'cmdb_ci_cluster'")[0]
    assert resolve_ci_id(cluster["Name"], "cmdb_ci_cluster") == cluster["CiId"]


def test_resolve_ci_id_returns_none_for_unknown_name():
    assert resolve_ci_id("no-such-ci-name-exists-anywhere") is None


def test_blast_radius_for_name_raises_on_unknown_ci():
    with pytest.raises(UnknownCiError):
        blast_radius_for_name("definitely-not-a-real-ci")


def test_blast_radius_matches_independent_bfs_at_depth_one():
    """Independent check, written fresh: pull the direct children of a real
    cluster CI via a plain query (no recursion), and confirm blast_radius at
    max_depth=1 reports exactly that set - a bug in the recursive term
    should not agree with a non-recursive query by coincidence."""
    cluster = fetch_all(f"SELECT TOP (1) CiId FROM {T('ConfigurationItem')} WHERE ClassName = 'cmdb_ci_cluster'")[0]
    start = cluster["CiId"]

    direct_children = {
        r["ChildCiId"] for r in fetch_all(f"SELECT ChildCiId FROM {T('CiRelationship')} WHERE ParentCiId = :start", {"start": start})
    }
    result = blast_radius(start, max_depth=1)
    assert result["affected_cis"] == len(direct_children)
    if direct_children:
        assert result["max_depth"] == 1


def test_blast_radius_direction_is_parent_to_child():
    """Migration 008 defines parent as the container/depended-upon; walking
    the other way would silently invert every result. Confirmed by picking a
    CI that is a known CHILD in some relationship and checking its blast
    radius as a PARENT does not include the thing it is a child of (unless a
    separate edge also makes that true in the other direction, which the
    independent query below accounts for)."""
    edge = fetch_all(f"SELECT TOP (1) ParentCiId, ChildCiId FROM {T('CiRelationship')}")[0]
    # Walking from the CHILD as if it were a parent must not immediately
    # report the real parent as reachable, unless the child is *also* a
    # parent of that same CI via some other edge - check for that directly
    # rather than assuming.
    child_is_also_a_parent_of_it = fetch_all(
        f"SELECT 1 AS x FROM {T('CiRelationship')} WHERE ParentCiId = :child AND ChildCiId = :parent",
        {"child": edge["ChildCiId"], "parent": edge["ParentCiId"]},
    )
    if child_is_also_a_parent_of_it:
        pytest.skip("this specific edge pair is bidirectional in the current seed; direction cannot be isolated from it")
    result = blast_radius(edge["ChildCiId"], max_depth=1)
    direct_from_child = {
        r["ChildCiId"] for r in fetch_all(
            f"SELECT ChildCiId FROM {T('CiRelationship')} WHERE ParentCiId = :start", {"start": edge["ChildCiId"]}
        )
    }
    assert edge["ParentCiId"] not in direct_from_child
    assert result["affected_cis"] == len(direct_from_child)


def test_blast_radius_reports_lower_bound_when_depth_ceiling_hit():
    """A CI known to have a multi-hop chain beneath it (a data centre - see
    migration 008's containment backfill: datacenter -> zone -> cluster ->
    server) must report hit_depth_ceiling=True and a smaller, explicitly
    partial count when capped below its real depth - proving "at least N"
    is reachable behaviour, not just a field that always reads False."""
    dc = fetch_all(f"SELECT TOP (1) CiId FROM {T('ConfigurationItem')} WHERE ClassName = 'cmdb_ci_datacenter'")[0]
    uncapped = blast_radius(dc["CiId"])
    if uncapped["max_depth"] < 2:
        pytest.skip("this data centre's current graph is only 1 hop deep - nothing to cap")

    capped = blast_radius(dc["CiId"], max_depth=1)
    assert capped["hit_depth_ceiling"] is True
    assert capped["affected_cis_is_lower_bound"] is True
    assert capped["affected_cis"] <= uncapped["affected_cis"]
    assert uncapped["hit_depth_ceiling"] is False


def test_blast_radius_zero_depth_returns_nothing_without_crashing():
    """max_depth=0 is a degenerate input no real call site uses (DEFAULT_MAX_DEPTH
    is 20; a deliberate small value would be >=1, "who does this directly
    affect"). ci_graph_repository._walk short-circuits before running any SQL
    when max_depth < 1 - documenting that behaviour (empty result, no crash)
    rather than asserting a different one."""
    dc = fetch_all(f"SELECT TOP (1) CiId FROM {T('ConfigurationItem')} WHERE ClassName = 'cmdb_ci_datacenter'")[0]
    result = blast_radius(dc["CiId"], max_depth=0)
    assert result["affected_cis"] == 0
    assert result["hit_depth_ceiling"] is False


def test_blast_radius_deduplicates_a_ci_reached_by_multiple_parents():
    """e7's warning: Provides::Uses (and, already today, Depends on - see the
    query below) let a CI have more than one parent, because several hosts
    can legitimately share one dependency. The recursive CTE explores each
    parent path independently and can produce more than one row for the same
    CiId; COUNT(DISTINCT CiId) must collapse them.

    Verified against an independent, from-scratch graph traversal in Python
    (plain BFS, global visited-by-id) over the real edge table - not against
    blast_radius's own SQL a second time, which could share the same bug.
    Uses today's real data (500+ CIs already have more than one direct
    parent via 'Depends on' edges - application dependency fan-in existed
    before this migration) rather than constructing anything.
    """
    # Existence check only - the estate is now large enough (Provides::Uses
    # alone is ~38,000 edges) that pulling every fan-in CI would itself need
    # a raised row cap for no reason; all this needs to know is whether at
    # least one exists.
    fan_in = fetch_all(
        f"SELECT TOP (1) ChildCiId FROM {T('CiRelationship')} GROUP BY ChildCiId HAVING COUNT(DISTINCT ParentCiId) > 1",
        max_rows=1,
    )
    if not fan_in:
        pytest.skip("no CI with multiple direct parents exists in the current graph")

    dc = fetch_all(f"SELECT TOP (1) CiId FROM {T('ConfigurationItem')} WHERE ClassName = 'cmdb_ci_datacenter'")[0]
    sql_result = blast_radius(dc["CiId"])
    if sql_result["hit_depth_ceiling"]:
        pytest.skip("this data centre's graph is deeper than MAX_DEPTH today - independent BFS would not be comparable")

    # The full estate is ~87,500 edges as of tonight's seed and growing -
    # generous headroom rather than a number tied to today's exact count.
    edges = fetch_all(f"SELECT ParentCiId, ChildCiId FROM {T('CiRelationship')}", max_rows=200_000)
    children_of: dict[int, list[int]] = {}
    for e in edges:
        children_of.setdefault(e["ParentCiId"], []).append(e["ChildCiId"])

    visited: set[int] = set()
    frontier = [dc["CiId"]]
    while frontier:
        current = frontier.pop()
        for child in children_of.get(current, []):
            if child not in visited:
                visited.add(child)
                frontier.append(child)

    assert sql_result["affected_cis"] == len(visited)


def test_blast_radius_terminates_on_the_planted_dependency_cycle(scenarios):
    """The one test that actually exercises the cycle guard against a real
    cycle rather than inferring it works. Skips (does not fail) until e7's
    seed exports SCENARIOS["dependency_cycle_applications"] - once it does,
    this must pass, and a hang or a timeout here means the visited-path
    guard regressed exactly as e7 warned."""
    cycle_apps = scenarios.get("dependency_cycle_applications")
    if not cycle_apps:
        pytest.skip("SCENARIOS['dependency_cycle_applications'] not seeded yet")

    app_code = cycle_apps[0]
    ci = fetch_all(
        f"SELECT ci.CiId FROM {T('ConfigurationItem')} ci WHERE ci.ClassName = 'cmdb_ci_appl' AND ci.Name = :code",
        {"code": app_code},
    )
    assert ci, f"planted cycle application {app_code!r} has no CI row"

    result = blast_radius(ci[0]["CiId"])  # must return, not hang
    assert result["affected_cis"] >= 1
