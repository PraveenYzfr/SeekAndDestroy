"""Change risk weighted by how much depends on the cluster.

A maintenance window on a cluster 29 applications rely on and the same window on
a cluster nothing touches are the same change and not the same risk. Until this
existed they scored identically, because both inputs to the change score - queued
changes and historical failure rate - describe the change rather than the
consequence.

The property these tests protect above all others: a cluster with no exposure
data scores EXACTLY what it scored before this feature existed. An estate whose
CMDB is not populated must not start looking dangerous.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.repositories import ci_graph_repository as graph
from app.repositories.base import fetch_all
from app.scoring import exposure, subscores
from app.services import change_exposure

from ._cmdb_load_state import require_loaded_graph

_RECORD = {"upcoming_changes": 3, "recent_changes": 20, "recent_failures": 2}


class TestMultiplier:
    def test_absent_exposure_has_no_effect(self):
        """None must mean "unknown", not "nothing depends on it"."""
        assert exposure.exposure_multiplier(None) == Decimal("1.0")

    def test_zero_dependents_has_no_effect(self):
        assert exposure.exposure_multiplier(0) == Decimal("1.0")

    def test_it_rises_with_dependents(self):
        values = [exposure.exposure_multiplier(n) for n in (1, 5, 15, 29)]
        assert values == sorted(values)
        assert all(v > Decimal("1.0") for v in values)

    def test_it_is_capped(self):
        """Uncapped, a hub cluster could contribute an unbounded penalty and
        dominate a score with six other dimensions in it."""
        assert exposure.exposure_multiplier(10_000) == exposure.MAX_EXPOSURE_MULTIPLIER

    def test_a_negative_count_is_treated_as_unknown(self):
        assert exposure.exposure_multiplier(-5) == Decimal("1.0")


class TestChangeRiskScoring:
    def test_the_score_is_unchanged_when_exposure_is_absent(self):
        """The compatibility guarantee. This is the assertion that matters most:
        adding exposure weighting must not move any score in an estate that has
        no CI graph."""
        assert subscores.change_risk_subscore(_RECORD) == Decimal("56.00")
        assert subscores.change_risk_subscore({**_RECORD, "dependent_applications": None}) == Decimal("56.00")
        assert subscores.change_risk_subscore({**_RECORD, "dependent_applications": 0}) == Decimal("56.00")

    def test_an_empty_record_still_scores_100(self):
        """"Nothing known against it" survives the change."""
        assert subscores.change_risk_subscore(None) == Decimal("100.00")

    def test_more_dependents_scores_worse(self):
        low = subscores.change_risk_subscore({**_RECORD, "dependent_applications": 5})
        high = subscores.change_risk_subscore({**_RECORD, "dependent_applications": 29})
        assert high < low < Decimal("56.00")

    def test_exposure_does_nothing_without_queued_changes(self):
        """Exposure weights CHURN. A cluster with nothing queued has no churn to
        weight, however much depends on it - the consequence of a change that is
        not happening is not a risk.
        """
        quiet = {"upcoming_changes": 0, "recent_changes": 20, "recent_failures": 2}
        assert subscores.change_risk_subscore(quiet) == subscores.change_risk_subscore(
            {**quiet, "dependent_applications": 29}
        )

    def test_exposure_does_not_scale_the_failure_rate(self):
        """The failure rate is an observed outcome and already reflects a
        cluster's importance - busy clusters accumulate more change history.
        Scaling it by exposure too would compound a correlation into a penalty.
        """
        failures_only = {"upcoming_changes": 0, "recent_changes": 10, "recent_failures": 5}
        assert subscores.change_risk_subscore(failures_only) == subscores.change_risk_subscore(
            {**failures_only, "dependent_applications": 29}
        )

    def test_the_score_stays_in_range(self):
        worst = {"upcoming_changes": 999, "recent_changes": 10, "recent_failures": 10}
        score = subscores.change_risk_subscore({**worst, "dependent_applications": 9999})
        assert Decimal("0") <= score <= Decimal("100")


class TestExposureLookup:
    def test_it_returns_nothing_for_no_clusters(self):
        assert change_exposure.exposure_for_clusters([]) == {}

    def test_it_counts_dependents_for_real_clusters(self):
        require_loaded_graph()
        rows = fetch_all(
            "SELECT TOP 5 ic.ClusterId FROM sad.InfrastructureCluster ic "
            "JOIN sad.ConfigurationItem ci ON ci.Name = ic.ClusterCode "
            "AND ci.ClassName = 'cmdb_ci_cluster'",
            max_rows=5,
        )
        if not rows:
            pytest.skip("no cluster CIs in the graph")
        ids = [r["ClusterId"] for r in rows]
        out = change_exposure.exposure_for_clusters(ids)
        assert out, "clusters that exist in the CMDB must resolve"
        for cluster_id, values in out.items():
            assert cluster_id in ids
            assert values["dependent_cis"] >= values["dependent_applications"] >= 0

    def test_a_real_cluster_actually_carries_applications(self):
        """The assertion that was missing, and the reason this regressed.

        The existing check was `dependent_cis >= dependent_applications >= 0`,
        which is satisfied by zero. When e7 removed the 1,933 shortcut
        cluster-to-application edges, the downward walk dead-ended on the cluster
        nodes and returned 0 dependent applications for EVERY cluster in the
        estate. The multiplier became 1.0 everywhere, the weighting silently did
        nothing, and this suite stayed green throughout.

        A relationship that holds trivially is not a check. This asserts the
        measurement is non-zero somewhere, which is the weakest claim that still
        has teeth.
        """
        require_loaded_graph()
        rows = fetch_all(
            "SELECT TOP 20 ic.ClusterId FROM sad.InfrastructureCluster ic "
            "JOIN sad.ConfigurationItem ci ON ci.Name = ic.ClusterCode "
            "AND ci.ClassName = 'cmdb_ci_cluster' ORDER BY ic.ClusterId",
            max_rows=20,
        )
        if not rows:
            pytest.skip("no cluster CIs in the graph")
        out = change_exposure.exposure_for_clusters([r["ClusterId"] for r in rows])
        assert out, "clusters that exist in the CMDB must resolve"
        assert any(v["dependent_applications"] > 0 for v in out.values()), (
            "every sampled cluster carries zero applications - the traversal has "
            "stopped reaching workloads and exposure weighting is inert"
        )
        assert any(v["dependent_cis"] > 10 for v in out.values()), (
            "no cluster carries more than ten CIs - the walk is not reaching "
            "the VMs behind the hardware"
        )

    def test_exposure_varies_between_clusters(self):
        """If every cluster returns the same number, the feature ranks nothing
        even when the number is non-zero."""
        require_loaded_graph()
        rows = fetch_all(
            "SELECT TOP 20 ic.ClusterId FROM sad.InfrastructureCluster ic "
            "JOIN sad.ConfigurationItem ci ON ci.Name = ic.ClusterCode "
            "AND ci.ClassName = 'cmdb_ci_cluster' ORDER BY ic.ClusterId",
            max_rows=20,
        )
        if not rows:
            pytest.skip("no cluster CIs in the graph")
        out = change_exposure.exposure_for_clusters([r["ClusterId"] for r in rows])
        assert len({v["dependent_cis"] for v in out.values()}) > 1, (
            "every cluster has identical exposure - nothing is being discriminated"
        )

    def test_an_unknown_cluster_id_is_simply_absent(self):
        """Not an error, and not a zero. A cluster the CMDB does not know about
        gets no entry, which means a multiplier of 1.0 downstream."""
        assert change_exposure.exposure_for_clusters([-1]) == {}

    def test_the_cluster_to_ci_bridge_pairs_the_right_rows(self):
        """Guards the ClusterId-to-CiId join, which is on the CODE because the
        two id spaces are unrelated. Pairing the wrong rows would report a real
        number for the wrong cluster, which no range check would catch.

        This no longer compares against blast_radius: exposure deliberately
        stopped being a downward walk when the shortcut edge was removed, so the
        two are no longer the same computation.
        """
        require_loaded_graph()
        rows = fetch_all(
            "SELECT TOP 3 ic.ClusterId, ic.ClusterCode, ci.CiId "
            "FROM sad.InfrastructureCluster ic "
            "JOIN sad.ConfigurationItem ci ON ci.Name = ic.ClusterCode "
            "AND ci.ClassName = 'cmdb_ci_cluster' ORDER BY ic.ClusterId",
            max_rows=3,
        )
        if not rows:
            pytest.skip("no cluster CIs in the graph")
        for row in rows:
            servers = graph.servers_under_clusters([row["CiId"]])
            out = change_exposure.exposure_for_clusters([row["ClusterId"]])
            assert out[row["ClusterId"]]["dependent_cis"] >= len(servers), (
                f"{row['ClusterCode']}: exposure is smaller than its own hardware count"
            )
