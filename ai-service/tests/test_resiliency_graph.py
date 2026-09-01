"""Resiliency computed from the CI graph, and the rule that acts on it.

Every count in these tests is computed from SQL in the test itself rather than
written as a literal. The estate is regenerating - servers, VMs, storage arrays,
volumes and network devices are all landing tonight - so a literal would be
asserting on tonight's seed rather than on behaviour, and would fail for a reason
that has nothing to do with the code.

These use the real database like the other repository-backed tests. They never
touch an embedding provider or an LLM: the whole path is SQL and arithmetic.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models.requirements import HostingRequirement
from app.repositories import ci_graph_repository as graph
from app.repositories import cluster_repository
from app.repositories.base import fetch_all
from app.rules import eligibility
from app.services import resiliency as R


def _requirement(criticality: str) -> HostingRequirement:
    return HostingRequirement(
        environment="Production", platform="Kubernetes", os_requirement="Any",
        cpu_cores=Decimal("2"), memory_gb=Decimal("8"), storage_gb=Decimal("100"),
        growth_percent=Decimal("0"), availability_tier="Tier-1",
        data_classification="Internal", criticality=criticality,
    )


def _context(cluster, criticality="Critical", profile=None):
    return eligibility.EligibilityContext(
        requirement=_requirement(criticality),
        cluster=cluster,
        projected=None,
        active_node_count=12,
        resiliency_profile=profile,
    )


@pytest.fixture(scope="module")
def any_application() -> str:
    rows = fetch_all(
        "SELECT TOP 1 Name FROM sad.ConfigurationItem WHERE ClassName='cmdb_ci_appl' "
        "AND EXISTS (SELECT 1 FROM sad.CiRelationship r WHERE r.ChildCiId = CiId) "
        "ORDER BY CiId",
        max_rows=1,
    )
    if not rows:
        pytest.skip("no CI graph loaded")
    return rows[0]["Name"]


@pytest.fixture(scope="module")
def a_cluster():
    return cluster_repository.list_all(limit=1)[0]


# =============================================================================
# Direction. The failure mode this guards is a plausible wrong answer.
# =============================================================================

class TestDirection:
    def test_up_and_down_are_not_the_same_set(self, any_application):
        """Resiliency walks up, blast radius walks down.

        Computing resiliency with the downward walk returns the application's
        dependents instead of its dependencies. Both are non-empty and both are
        the right order of magnitude, which is exactly why this needs a test
        rather than care.
        """
        ci = graph.ci_for_application(any_application)
        up = {n.ci_id for n in graph.support_graph(ci.ci_id)}
        down = {n.ci_id for n in graph.blast_radius(ci.ci_id)}
        assert up, "an application with edges must stand on something"
        assert up != down

    def test_upward_reaches_the_site_and_downward_does_not(self, any_application):
        """A data centre contains an application; it does not depend on one."""
        ci = graph.ci_for_application(any_application)
        up_classes = {n.class_name for n in graph.support_graph(ci.ci_id)}
        down_classes = {n.class_name for n in graph.blast_radius(ci.ci_id)}
        assert graph.CLASS_DATACENTER in up_classes
        assert graph.CLASS_DATACENTER not in down_classes

    def test_the_walk_agrees_with_a_plain_join(self, any_application):
        """One hop up, computed two ways. Guards the CTE against drifting from
        what the same question looks like in ordinary SQL."""
        ci = graph.ci_for_application(any_application)
        direct = {
            r["ParentCiId"]
            for r in fetch_all(
                "SELECT ParentCiId FROM sad.CiRelationship WHERE ChildCiId = :c AND TypeId IN (1,2,3,5,6)",
                {"c": ci.ci_id}, max_rows=1000,
            )
        }
        walked = {n.ci_id for n in graph.support_graph(ci.ci_id, max_depth=1)}
        assert walked == direct


# =============================================================================
# Termination and truncation
# =============================================================================

class TestGuards:
    def test_a_dependency_cycle_terminates(self):
        """`Depends on::Used by` is genuinely cyclic - two applications calling
        each other is a real topology and is deliberately seeded. Without the
        visited-path guard this does not return."""
        rows = fetch_all(
            "SELECT TOP 1 a.ChildCiId AS Start FROM sad.CiRelationship a "
            "JOIN sad.CiRelationship b ON b.ChildCiId = a.ParentCiId AND b.ParentCiId = a.ChildCiId "
            "WHERE a.TypeId = 4 AND b.TypeId = 4",
            max_rows=1,
        )
        if not rows:
            pytest.skip("no dependency cycle in the current seed")
        # The assertion is that this returns at all.
        walk = graph.blast_radius(rows[0]["Start"])
        assert walk.max_depth == graph.DEFAULT_MAX_DEPTH
        assert walk.observed_depth <= walk.max_depth

    def test_hit_ceiling_fires_when_the_walk_is_cut(self, any_application):
        ci = graph.ci_for_application(any_application)
        deep = graph.support_graph(ci.ci_id)
        shallow = graph.support_graph(ci.ci_id, max_depth=1)
        assert not deep.hit_ceiling, "a 20-deep walk over a 3-deep topology is not truncated"
        assert shallow.hit_ceiling
        assert len(shallow) < len(deep)
        # max_depth is the requested cap; observed_depth is how far it got.
        assert shallow.max_depth == 1 and shallow.observed_depth == 1
        assert deep.max_depth == graph.DEFAULT_MAX_DEPTH
        assert deep.observed_depth < deep.max_depth

    def test_hit_ceiling_survives_the_min_depth_aggregation(self, any_application):
        """The bug c2 caught. The projection reports MIN(Depth) per CI, so a node
        reached cheaply by one path hides that another path was cut. The flag is
        computed from the deepest raw depth, before that aggregation."""
        ci = graph.ci_for_application(any_application)
        shallow = graph.support_graph(ci.ci_id, max_depth=1)
        assert all(n.depth == 1 for n in shallow), "every surviving depth looks unremarkable"
        assert shallow.hit_ceiling, "and yet the walk was truncated"


# =============================================================================
# Absent is not zero. The trap this module exists to avoid.
# =============================================================================

class TestAbsentIsNotZero:
    def test_an_unplaced_application_is_unknown_not_fragile(self):
        rows = fetch_all(
            "SELECT TOP 1 a.Name FROM sad.ConfigurationItem a WHERE a.ClassName='cmdb_ci_appl' "
            "AND NOT EXISTS (SELECT 1 FROM sad.CiRelationship r WHERE r.ChildCiId = a.CiId)",
            max_rows=1,
        )
        if not rows:
            pytest.skip("every application has a placement in the current seed")
        p = R.profile_for_application(rows[0]["Name"])
        assert p.unplaced
        assert p.redundancy is None
        assert not p.is_single_point_of_failure, (
            "an application nobody recorded a placement for is not thereby a single "
            "point of failure - that is a confident answer to an unanswerable question"
        )
        assert R.graph_resiliency_subscore(p, "Tier-1") is None

    def test_an_unknown_application_does_not_raise(self):
        p = R.profile_for_application("APP-DOES-NOT-EXIST-ANYWHERE")
        assert p.unplaced and p.redundancy is None

    def test_empty_domains_are_reported_as_not_evaluated(self, any_application):
        """Storage and network CIs are not seeded yet. They must be absent from
        the score, not scored as zero - otherwise the entire estate becomes a
        single point of failure on the strength of our own seed data."""
        p = R.profile_for_application(any_application)
        seeded = {
            r["ClassName"]
            for r in fetch_all(
                "SELECT DISTINCT ClassName FROM sad.ConfigurationItem", max_rows=100
            )
        }
        for label, class_name in R.DOMAINS:
            if class_name not in seeded:
                assert label in p.not_evaluated
                assert label not in p.domains

    def test_a_cluster_node_is_never_counted_as_a_physical_host(self, any_application):
        """A node is a membership record; a server is the machine.

        They were one row until migration 011, and conflating them produced
        hardware averaging 7 cores because the row carried total_cpu/node_count.
        Counting nodes as hosts is the dangerous version of the mistake: with one
        server per node the two counts AGREE, so it looks right until hardware is
        shared and then silently is not.

        Right now the estate has 2,007 nodes and zero servers loaded, so this
        test has teeth today: anything reporting a host count is counting nodes.
        """
        seeded = {
            r["ClassName"]: r["C"]
            for r in fetch_all(
                "SELECT ClassName, COUNT(*) C FROM sad.ConfigurationItem GROUP BY ClassName",
                max_rows=100,
            )
        }
        if seeded.get(graph.CLASS_SERVER):
            pytest.skip("real servers are loaded - the confusion is no longer detectable this way")
        assert seeded.get(graph.CLASS_CLUSTER_NODE), "expected cluster nodes in the graph"
        p = R.profile_for_application(any_application)
        assert "physical host" not in p.domains, (
            "host count resolved from an estate with no servers - it is counting nodes"
        )
        assert "physical host" in p.not_evaluated

    def test_truncation_suppresses_the_spof_claim(self, any_application):
        """Truncation can only under-state a count, and an under-stated count
        manufactures a single point of failure that is not there."""
        ci = graph.ci_for_application(any_application)
        truncated = R.ResiliencyProfile(
            application_ci_id=ci.ci_id, application_name=any_application,
            domains={"cluster": R.FailureDomain("cluster", graph.CLASS_CLUSTER, 1)},
            redundancy=1, weakest="cluster", truncated=True,
        )
        assert not truncated.is_single_point_of_failure
        assert "may be higher" in truncated.summary()


# =============================================================================
# The minimum, not the mean
# =============================================================================

class TestMinimumNotMean:
    def test_the_weakest_domain_is_the_answer(self):
        """Eight hosts, four switches, one volume is one-way redundant. The mean
        would say 4.3 and hide the volume - the old node-count defect one level
        up."""
        p = R.ResiliencyProfile(
            application_ci_id=1, application_name="APP-X",
            domains={
                "physical host": R.FailureDomain("physical host", graph.CLASS_SERVER, 8),
                "network device": R.FailureDomain("network device", graph.CLASS_NETWORK, 4),
                "storage volume": R.FailureDomain("storage volume", graph.CLASS_STORAGE_VOLUME, 1),
            },
            redundancy=1, weakest="storage volume",
        )
        assert p.is_single_point_of_failure
        assert "storage volume" in p.summary()

    def test_redundancy_equals_the_minimum_over_the_real_graph(self, any_application):
        p = R.profile_for_application(any_application)
        if p.redundancy is None:
            pytest.skip("nothing evaluable")
        assert p.redundancy == min(d.count for d in p.domains.values())
        assert p.domains[p.weakest].count == p.redundancy

    def test_many_hosts_do_not_rescue_a_single_cluster(self):
        """The disagreement with the old score, stated as a property.

        The old formula caps its node bonus at four extra nodes, so any
        application on four or more hosts scored maximum regardless of how those
        hosts were distributed.
        """
        # Pick the single-cluster application with the MOST hosts behind it.
        # Taking an arbitrary one skipped this test on a small application and
        # quietly stopped exercising the disagreement it exists to demonstrate.
        rows = fetch_all(
            "SELECT TOP 1 a.Name AS Name, COUNT(DISTINCT m.ChildCiId) AS Hosts "
            "FROM sad.ConfigurationItem a "
            "JOIN sad.CiRelationship r ON r.ChildCiId = a.CiId AND r.TypeId = 1 "
            "JOIN sad.CiRelationship m ON m.ParentCiId = r.ParentCiId AND m.TypeId = 3 "
            "WHERE a.ClassName = 'cmdb_ci_appl' "
            "GROUP BY a.Name "
            "HAVING COUNT(DISTINCT r.ParentCiId) = 1 "
            "ORDER BY COUNT(DISTINCT m.ChildCiId) DESC",
            max_rows=1,
        )
        if not rows:
            pytest.skip("no single-cluster application in the current seed")
        p = R.profile_for_application(rows[0]["Name"])

        # The claim that survives migration 011. Physical servers were
        # reclassified to cmdb_ci_cluster_node and the real cmdb_ci_server rows
        # are not loaded yet, so the host domain may legitimately have no data.
        # What must hold either way is that a single-cluster application is
        # flagged, and that an absent host domain is absent rather than zero.
        hosts = p.domains.get("physical host")
        if hosts is None:
            assert "physical host" in p.not_evaluated
        else:
            # 4 is where the old formula's node bonus saturates: base + 5 per
            # extra node capped at 20, so from four extra nodes on it cannot
            # tell any two applications apart.
            assert hosts.count >= 4, (
                f"{rows[0]['Name']} has {hosts.count} hosts - the old score maxed out here"
            )
        assert p.is_single_point_of_failure, "the new score sees the single cluster"
        assert p.weakest == "cluster"


# =============================================================================
# RULE-012
# =============================================================================

class TestRule012:
    def test_passes_when_there_is_no_profile(self, a_cluster):
        """An estate without a populated CI graph must not become ineligible."""
        r = eligibility.rule_012_single_point_of_failure(_context(a_cluster, profile=None))
        assert r.passed

    def test_passes_for_an_unplaced_application(self, a_cluster):
        r = eligibility.rule_012_single_point_of_failure(
            _context(a_cluster, profile={"unplaced": True, "single_point_of_failure": False})
        )
        assert r.passed

    def test_passes_when_the_walk_was_truncated(self, a_cluster):
        r = eligibility.rule_012_single_point_of_failure(
            _context(a_cluster, profile={"truncated": True, "single_point_of_failure": True,
                                         "redundancy": 1, "weakest": "cluster"})
        )
        assert r.passed, "a lower bound cannot establish a single point of failure"

    def test_passes_for_low_criticality(self, a_cluster):
        r = eligibility.rule_012_single_point_of_failure(
            _context(a_cluster, criticality="Low",
                     profile={"single_point_of_failure": True, "redundancy": 1, "weakest": "zone"})
        )
        assert r.passed

    def test_fails_a_critical_workload_on_a_single_zone(self, a_cluster):
        r = eligibility.rule_012_single_point_of_failure(
            _context(a_cluster, profile={
                "single_point_of_failure": True, "redundancy": 1, "weakest": "zone",
                "domains": {"physical host": 38, "zone": 1},
            })
        )
        assert not r.passed
        assert "zone" in r.reason
        assert r.evidence["domains"]["physical host"] == 38, (
            "the evidence must carry the host count - it is what makes the finding "
            "surprising to a reader who trusts the old score"
        )

    def test_passes_when_this_placement_adds_a_second_cluster(self, a_cluster):
        """The one case where the candidate demonstrably fixes the problem."""
        r = eligibility.rule_012_single_point_of_failure(
            _context(a_cluster, profile={
                "single_point_of_failure": True, "redundancy": 1, "weakest": "cluster",
                "weakest_members": ["some-other-cluster"],
            })
        )
        assert r.passed
        assert r.evidence.get("resolves") is True

    def test_fails_when_the_candidate_is_already_the_only_cluster(self, a_cluster):
        r = eligibility.rule_012_single_point_of_failure(
            _context(a_cluster, profile={
                "single_point_of_failure": True, "redundancy": 1, "weakest": "cluster",
                "weakest_members": [a_cluster.ClusterCode],
            })
        )
        assert not r.passed
        assert a_cluster.ClusterCode in r.reason

    def test_does_not_claim_to_fix_a_storage_single_point(self, a_cluster):
        """Which volume a workload lands on is not decided by choosing a cluster,
        so the rule must not pass on the theory that this placement helps."""
        r = eligibility.rule_012_single_point_of_failure(
            _context(a_cluster, profile={
                "single_point_of_failure": True, "redundancy": 1, "weakest": "storage volume",
            })
        )
        assert not r.passed
        assert "not decided by the choice of cluster" in r.reason


class TestRuleRegistration:
    def test_rule_012_is_actually_wired_into_evaluate_all(self):
        """RULE-011 shipped once without being registered. A rule that is never
        called passes every test written against the function directly."""
        import inspect

        source = inspect.getsource(eligibility.evaluate_all)
        assert "rule_012_single_point_of_failure(ctx)" in source
