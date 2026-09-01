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

from ._cmdb_load_state import require_loaded_graph


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
    # Skips on an empty CMDB, FAILS on a half-loaded one. See _cmdb_load_state:
    # this suite once went from 22 passing to 14 skipped during a reload and
    # reported success, which is a false negative wearing a friendly face.
    require_loaded_graph()
    rows = fetch_all(
        "SELECT TOP 1 Name FROM sad.ConfigurationItem WHERE ClassName='cmdb_ci_appl' "
        "AND EXISTS (SELECT 1 FROM sad.CiRelationship r WHERE r.ChildCiId = CiId) "
        "ORDER BY CiId",
        max_rows=1,
    )
    if not rows:
        pytest.skip("no application has any relationship")
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
        """`Depends on::Used by` is genuinely cyclic and the seed plants a ring.

        APP-IDENTITY -> APP-SSO -> APP-APIGATEWAY -> APP-IDENTITY.

        Three-deep rather than a mutual pair, and that is the point: a guard that
        only remembers the immediately preceding node terminates correctly on a
        pair and loops forever on a triangle. A two-app cycle would have passed a
        broken implementation and proved nothing. e7 measured a naive one-step
        guard walking ten hops on this ring without terminating.

        The assertion is that this returns at all, and returns bounded.
        """
        import generate_seed

        ring = generate_seed.SCENARIOS.get("dependency_cycle_applications") or []
        if not ring:
            pytest.skip("seed has no dependency_cycle_applications fixture")
        require_loaded_graph()

        for name in ring:
            ci = graph.ci_for_application(name)
            assert ci is not None, f"{name} is in SCENARIOS but not in the CMDB"
            walk = graph.blast_radius(ci.ci_id)
            assert walk.observed_depth <= walk.max_depth
            # Every CI appears once. Without the visited-path guard a cycle
            # re-emits the same nodes at increasing depths until the ceiling.
            assert len({n.ci_id for n in walk}) == len(walk.nodes)

    def test_the_cycle_members_actually_reach_each_other(self):
        """Guards the guard. If the ring were not really a ring - a broken seed,
        or edges pointing the other way - the termination test above would pass
        for the wrong reason, having walked a short acyclic path."""
        import generate_seed

        ring = generate_seed.SCENARIOS.get("dependency_cycle_applications") or []
        if len(ring) < 3:
            pytest.skip("no three-deep cycle fixture")
        require_loaded_graph()

        start = graph.ci_for_application(ring[0])
        reached = {n.name for n in graph.blast_radius(start.ci_id)}
        for other in ring[1:]:
            assert other in reached, f"{ring[0]} does not reach {other} - the ring is not a ring"

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
        """The 87, asserted against the fixture rather than found incidentally.

        Two sessions arrived at this population from opposite ends: I found 87
        applications whose resiliency cannot be assessed, and c2 found 87
        applications the packer never placed. They are the same set - Staging
        applications that hit the 97%-of-physical-cores allocation ceiling,
        because non-production clusters are sized at 0.75 of a production target.
        The trade-off is deliberate; an overcommitted cluster is worse than an
        unplaced application.

        What matters here is that they come back UNKNOWN and never RISKY.
        Reporting "0 hosts, single point of failure" for an application nobody
        recorded a placement for is a confident answer to a question the CMDB
        cannot answer, and it would put 87 fabricated findings in front of a
        reader who has no way to tell them from the 387 real ones.
        """
        import generate_seed

        unhosted = generate_seed.SCENARIOS.get("unhosted_applications") or []
        if not unhosted:
            pytest.skip("no unhosted_applications fixture")
        require_loaded_graph()
        for name in unhosted:
            p = R.profile_for_application(name)
            assert p.redundancy is None, f"{name} is unhosted but reported a redundancy figure"
            assert not p.is_single_point_of_failure, (
                f"{name} has no recorded placement and must not be called fragile"
            )
            assert R.graph_resiliency_subscore(p, "Tier-1") is None

    def test_the_two_derivations_of_the_unassessable_set_agree(self):
        """Cross-check between sessions.

        c2 derives this set from the packer's allocation failures; I derive it
        from applications whose support graph yields no evaluable failure domain.
        Nothing forces the two to match, so if they diverge, one of us has a bug
        and neither would notice from inside our own code.
        """
        import generate_seed

        unhosted = set(generate_seed.SCENARIOS.get("unhosted_applications") or [])
        if not unhosted:
            pytest.skip("no unhosted_applications fixture")
        require_loaded_graph()
        rows = fetch_all(
            "SELECT Name FROM sad.ConfigurationItem WHERE ClassName='cmdb_ci_appl'",
            max_rows=5000,
        )
        mine = {r["Name"] for r in rows if R.profile_for_application(r["Name"]).redundancy is None}
        assert mine == unhosted, (
            f"the two derivations disagree - only I see {sorted(mine - unhosted)[:5]}, "
            f"only the packer sees {sorted(unhosted - mine)[:5]}"
        )

    def test_the_graph_and_the_hosting_table_agree_on_who_is_unplaced(self):
        """Cross-check across two different sources of truth, not two readings
        of one.

        My unassessable set comes from the CI GRAPH - applications whose upward
        walk yields no evaluable failure domain. The relational answer comes from
        sad.ApplicationHosting - applications with no hosting row. Those are
        separate tables maintained by separate code paths, so nothing forces them
        to agree, and a divergence means the graph and the relational model have
        drifted apart.

        That drift is the failure this catches, and it is invisible from either
        side alone: each looks internally consistent.
        """
        require_loaded_graph()
        rows = fetch_all(
            "SELECT Name FROM sad.ConfigurationItem WHERE ClassName='cmdb_ci_appl'",
            max_rows=5000,
        )
        from_graph = {r["Name"] for r in rows if R.profile_for_application(r["Name"]).redundancy is None}
        from_table = {
            r["ApplicationCode"]
            for r in fetch_all(
                "SELECT a.ApplicationCode FROM sad.CmdbApplication a "
                "WHERE NOT EXISTS (SELECT 1 FROM sad.ApplicationHosting h "
                "                  WHERE h.ApplicationId = a.ApplicationId)",
                max_rows=5000,
            )
        }
        assert from_graph == from_table, (
            f"graph and hosting table disagree - only the graph says "
            f"{sorted(from_graph - from_table)[:5]}, only the table says "
            f"{sorted(from_table - from_graph)[:5]}"
        )

    def test_c2s_unconnected_applications_are_a_subset_of_mine(self):
        """c2 splits the unplaced population into unconnected and
        dependency-linked. Their unconnected set must be a subset of my
        unassessable set, and the arithmetic is DERIVED here rather than
        hardcoded - asserting "65 + 22 = 87" would bake in three numbers that
        move with every seed.

        Why the subset must hold: an application with only a Depends-on edge is
        still unassessable to me, because SUPPORT_EDGES deliberately excludes
        type 4 - an application is not redundant across the services it calls.
        So anything c2 calls unconnected is necessarily also unassessable, while
        the reverse is not true.
        """
        require_loaded_graph()
        from app.insights import cmdb_health

        breakdown = cmdb_health.unhosted_application_breakdown()
        rows = fetch_all(
            "SELECT Name FROM sad.ConfigurationItem WHERE ClassName='cmdb_ci_appl'",
            max_rows=5000,
        )
        mine = sum(1 for r in rows if R.profile_for_application(r["Name"]).redundancy is None)

        assert breakdown["total_unhosted"] == mine, (
            f"c2 counts {breakdown['total_unhosted']} unhosted, I count {mine} "
            f"unassessable - these are the same population reached two ways"
        )
        assert breakdown["unhosted_and_unconnected"] <= mine
        assert (
            breakdown["unhosted_and_unconnected"] + breakdown["unhosted_but_dependency_linked"]
            == mine
        ), "the split must partition the population, not overlap or lose members"

    def test_the_unassessable_population_stays_small(self):
        """e7's invariant, checked from my side too.

        A handful of unplaceable staging applications is a capacity trade-off. A
        large fraction would mean resiliency is silently not being measured for
        most of the estate, and the headline SPOF count would be describing a
        minority while reading like a census.
        """
        import generate_seed

        unhosted = generate_seed.SCENARIOS.get("unhosted_applications") or []
        if not unhosted:
            pytest.skip("no unhosted_applications fixture")
        total = fetch_all(
            "SELECT COUNT(*) AS C FROM sad.ConfigurationItem WHERE ClassName='cmdb_ci_appl'",
            max_rows=1,
        )[0]["C"]
        assert len(unhosted) < total * 0.10, (
            f"{len(unhosted)} of {total} applications are unassessable - resiliency is "
            f"not being measured for a meaningful share of the estate"
        )

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
        for label, class_name, _scale in R.DOMAINS:
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
            domains={"cluster": R.FailureDomain(label="cluster", class_name=graph.CLASS_CLUSTER, count=1, scale=R.COMPONENT)},
            redundancy=1, weakest="cluster", truncated=True,
        )
        assert not truncated.is_single_point_of_failure
        assert "may be higher" in truncated.summary()


# =============================================================================
# The minimum, not the mean
# =============================================================================

class TestComponentAndSiteScale:
    """The control fixture caught this design being wrong.

    The first version took the minimum across EVERY domain, so applications
    verified as well distributed - three hosts, three volumes, three arrays, two
    switches - were reported as single points of failure because they sit in one
    data centre. Most applications deliberately do. A rule that fires on the
    control fires on everything.
    """

    def test_the_control_fixture_is_not_a_single_point_of_failure(self):
        import generate_seed

        controls = generate_seed.SCENARIOS.get("well_distributed_applications") or []
        if not controls:
            pytest.skip("no well_distributed_applications fixture")
        require_loaded_graph()
        for name in controls:
            p = R.profile_for_application(name)
            assert not p.is_single_point_of_failure, (
                f"{name} is the CONTROL - it spans "
                f"{ {k: v.count for k, v in p.domains.items()} }. A rule that fires "
                f"here fires on everything and measures nothing."
            )

    def test_the_control_is_still_reported_as_single_site(self):
        """Not folded into the headline, but not hidden either. Suppressing it
        entirely would be the opposite error - the fact is true and worth saying,
        it just is not the same finding as a single NAS head."""
        import generate_seed

        controls = generate_seed.SCENARIOS.get("well_distributed_applications") or []
        if not controls:
            pytest.skip("no well_distributed_applications fixture")
        require_loaded_graph()
        single_site = [n for n in controls if R.profile_for_application(n).is_single_site]
        assert single_site, "expected the control applications to be single-site"

    def test_site_domains_never_lower_the_component_figure(self):
        p = R.ResiliencyProfile(
            application_ci_id=1, application_name="APP-X",
            domains={
                "physical host": R.FailureDomain(label="physical host", class_name=graph.CLASS_SERVER, count=4, scale=R.COMPONENT),
                "data centre": R.FailureDomain(label="data centre", class_name=graph.CLASS_DATACENTER, count=1, scale=R.SITE),
            },
            redundancy=4, weakest="physical host",
            site_redundancy=1, weakest_site="data centre",
        )
        assert not p.is_single_point_of_failure
        assert p.is_single_site

    @pytest.mark.parametrize(
        "fixture,expected_domain",
        [
            ("spof_single_host_applications", "physical host"),
            ("spof_single_volume_applications", "storage volume"),
            ("spof_single_array_applications", "storage array"),
        ],
    )
    def test_each_spof_fixture_names_its_own_domain(self, fixture, expected_domain):
        """Firing is not enough - it has to fire for the right reason.

        The array case is the sharpest: two DISTINCT volumes on ONE array. A
        traversal that stops at the volume level reports two-way redundancy and
        is confidently wrong, so this asserts the walk went the extra level.
        """
        import generate_seed

        apps = generate_seed.SCENARIOS.get(fixture) or []
        if not apps:
            pytest.skip(f"no {fixture} fixture")
        require_loaded_graph()
        for name in apps:
            p = R.profile_for_application(name)
            assert p.is_single_point_of_failure, f"{name} should be a single point of failure"
            assert p.weakest == expected_domain, (
                f"{name}: expected the weakest domain to be {expected_domain}, got "
                f"{p.weakest} from { {k: v.count for k, v in p.domains.items()} }"
            )


class TestMinimumNotMean:
    def test_the_weakest_domain_is_the_answer(self):
        """Eight hosts, four switches, one volume is one-way redundant. The mean
        would say 4.3 and hide the volume - the old node-count defect one level
        up."""
        p = R.ResiliencyProfile(
            application_ci_id=1, application_name="APP-X",
            domains={
                "physical host": R.FailureDomain(label="physical host", class_name=graph.CLASS_SERVER, count=8, scale=R.COMPONENT),
                "network device": R.FailureDomain(label="network device", class_name=graph.CLASS_NETWORK, count=4, scale=R.COMPONENT),
                "storage volume": R.FailureDomain(label="storage volume", class_name=graph.CLASS_STORAGE_VOLUME, count=1, scale=R.COMPONENT),
            },
            redundancy=1, weakest="storage volume",
        )
        assert p.is_single_point_of_failure
        assert "storage volume" in p.summary()

    def test_redundancy_equals_the_minimum_over_the_real_graph(self, any_application):
        p = R.profile_for_application(any_application)
        if p.redundancy is None:
            pytest.skip("nothing evaluable")
        assert p.redundancy == min(
            d.count for d in p.domains.values() if d.scale == R.COMPONENT
        )
        assert p.domains[p.weakest].count == p.redundancy

    def test_hardware_spread_does_not_rescue_a_single_storage_domain(self):
        """The disagreement with the old score, as a property rather than a number.

        This previously looked for an application on many hosts inside one
        CLUSTER. After the VM layer landed, applications no longer attach to
        clusters directly and that query stopped matching anything - the test
        skipped, which is the failure mode this whole suite exists to avoid.

        The modern equivalent is sharper anyway: an application genuinely spread
        across several physical hosts that still dies with one volume or one
        array. The old formula caps its node bonus at four extra nodes, so it
        cannot tell such an application apart from a fully redundant one, and
        counting hosts - however carefully - never finds it.
        """
        require_loaded_graph()
        rows = fetch_all(
            "SELECT Name FROM sad.ConfigurationItem WHERE ClassName='cmdb_ci_appl' ORDER BY CiId",
            max_rows=2000,
        )
        found = []
        for row in rows:
            p = R.profile_for_application(row["Name"])
            if not p.is_single_point_of_failure:
                continue
            hosts = p.domains.get("physical host")
            if hosts and hosts.count > 1 and p.weakest != "physical host":
                found.append((row["Name"], hosts.count, p.weakest))
            if len(found) >= 3:
                break
        assert found, (
            "no application is spread across hosts yet single elsewhere - either the "
            "estate genuinely has none, or the storage and network domains stopped "
            "resolving and every finding collapsed onto the host count"
        )
        for name, host_count, weakest in found:
            assert weakest in ("storage volume", "storage array", "network device", "cluster")
            assert host_count > 1, f"{name} was supposed to be spread across hardware"


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
