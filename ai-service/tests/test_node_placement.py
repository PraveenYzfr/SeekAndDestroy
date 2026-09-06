"""Node-level placement: the second stage that turns "cluster nyc-p006" into
"cluster nyc-p006, host nyc-p006-NODE-15".

Runs against the live seeded database like the rest of the suite (see
conftest.py) - the 256-cluster / ~2,000-node estate is the fixture.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.config import get_settings
from app.models.capacity import ProjectedUtilization
from app.repositories import application_repository, cluster_repository, node_repository
from app.scoring import subscores
from app.scoring.engine import compute_node_overall_score
from app.scoring.weights import get_node_weights
from app.services import node_placement, placement


@pytest.fixture(scope="module")
def crm_requirement():
    app = application_repository.get_by_code("APP-CRM")
    return placement.requirement_for_application(app)


@pytest.fixture(scope="module")
def top_cluster(crm_requirement):
    ranked = placement.find_and_score_candidates(crm_requirement, top_n=1)
    eligible = [c for c in ranked if c.eligibility_status == "Eligible"]
    assert eligible, "expected at least one eligible cluster for APP-CRM"
    return eligible[0]


# =============================================================================
# Weights and scoring formulas
# =============================================================================


def test_node_weights_sum_to_one():
    total = sum(get_node_weights().values())
    assert total == Decimal("1.0") or abs(float(total) - 1.0) < 1e-9


def _projected(headroom: Decimal) -> ProjectedUtilization:
    return ProjectedUtilization(
        required_cpu_cores_effective=Decimal("1"), required_memory_gb_effective=Decimal("1"),
        required_storage_gb_effective=Decimal("1"),
        projected_cpu_utilization_percent=Decimal("100") - headroom,
        projected_memory_utilization_percent=Decimal("0"),
        projected_storage_utilization_percent=Decimal("0"),
        projected_headroom_percent=headroom,
        fits_cpu=True, fits_memory=True, fits_storage=True, fits_all=True,
    )


def test_node_capacity_subscore_does_not_saturate_between_healthy_hosts():
    """The regression this formula exists for: the cluster capacity sub-score
    divides by a 20% target and clamps at 100, so two hosts with 26% and 28%
    headroom would both score 100 and the ranking would collapse to
    alphabetical order by hostname.
    """
    cluster_style_a = subscores.capacity_subscore(_projected(Decimal("26")), target_headroom_percent=Decimal("20"))
    cluster_style_b = subscores.capacity_subscore(_projected(Decimal("28")), target_headroom_percent=Decimal("20"))
    assert cluster_style_a == cluster_style_b == Decimal("100.00")

    node_a = subscores.node_capacity_subscore(_projected(Decimal("26")))
    node_b = subscores.node_capacity_subscore(_projected(Decimal("28")))
    assert node_b > node_a


def test_node_capacity_subscore_is_clamped_to_0_100():
    assert subscores.node_capacity_subscore(_projected(Decimal("-40"))) == Decimal("0.00")
    assert subscores.node_capacity_subscore(_projected(Decimal("140"))) == Decimal("100.00")


def test_node_risk_penalizes_staleness_lifecycle_and_incidents():
    healthy = subscores.node_operational_risk_score(
        lifecycle_status="Active", open_severe_incident_count=0,
        staleness_days=0, stale_after_days=7, has_measurements=True,
    )
    assert healthy == Decimal("0.00")

    for kwargs in (
        {"lifecycle_status": "Deprecated"},
        {"open_severe_incident_count": 2},
        {"staleness_days": 30},
        {"has_measurements": False},
    ):
        base = {
            "lifecycle_status": "Active", "open_severe_incident_count": 0,
            "staleness_days": 0, "stale_after_days": 7, "has_measurements": True,
        }
        assert subscores.node_operational_risk_score(**{**base, **kwargs}) > healthy


def test_node_overall_score_uses_configured_weights():
    from app.models.scoring import NodeSubScores

    sub = NodeSubScores(
        capacity=Decimal("50"), cost=Decimal("100"), reliability=Decimal("100"), risk=Decimal("0")
    )
    w = get_node_weights()
    expected = (
        w["capacity"] * Decimal("50") + w["cost"] * Decimal("100")
        + w["reliability"] * Decimal("100") + w["risk"] * Decimal("100")
    )
    assert compute_node_overall_score(sub) == subscores.round2(expected)


# =============================================================================
# Per-host requirement split
# =============================================================================


def test_spreading_platform_splits_the_requirement_across_active_hosts():
    cluster = cluster_repository.get_by_code("nyc-03")
    assert cluster.Platform == "Kubernetes"
    cpu, mem, storage, model, denominator = node_placement.per_host_requirement(
        cluster, 8,
        required_cpu_cores_effective=Decimal("16"),
        required_memory_gb_effective=Decimal("64"),
        required_storage_gb_effective=Decimal("800"),
    )
    assert model == "share"
    assert denominator == 8
    assert cpu == Decimal("2")
    assert mem == Decimal("8")
    assert storage == Decimal("100")


def test_single_host_cluster_falls_back_to_whole_requirement():
    cluster = cluster_repository.get_by_code("nyc-03")
    _cpu, _mem, _storage, model, denominator = node_placement.per_host_requirement(
        cluster, 1,
        required_cpu_cores_effective=Decimal("16"),
        required_memory_gb_effective=Decimal("64"),
        required_storage_gb_effective=Decimal("800"),
    )
    assert model == "whole"
    assert denominator == 1


def test_bare_metal_requires_one_host_to_absorb_everything():
    bare_metal = next(
        (c for c in cluster_repository.list_all(limit=500) if c.Platform == "BareMetal"), None
    )
    if bare_metal is None:
        pytest.skip("no BareMetal cluster in the seeded estate")
    cpu, mem, storage, model, denominator = node_placement.per_host_requirement(
        bare_metal, 12,
        required_cpu_cores_effective=Decimal("16"),
        required_memory_gb_effective=Decimal("64"),
        required_storage_gb_effective=Decimal("800"),
    )
    assert model == "whole"
    assert denominator == 1
    assert (cpu, mem, storage) == (Decimal("16"), Decimal("64"), Decimal("800"))


# =============================================================================
# Ranking behavior against the real estate
# =============================================================================


def test_ranked_nodes_are_ordered_by_score_descending(crm_requirement, top_cluster):
    ranked = node_placement.rank_nodes_for_candidate(crm_requirement, top_cluster)
    eligible = [n for n in ranked if n.eligibility_status == "Eligible"]
    assert eligible, "expected the best cluster to have at least one eligible host"
    scores = [n.overall_score for n in eligible]
    assert scores == sorted(scores, reverse=True)
    assert [n.rank for n in eligible] == list(range(1, len(eligible) + 1))


def test_node_ranking_is_reproducible(crm_requirement, top_cluster):
    first = node_placement.rank_nodes_for_candidate(crm_requirement, top_cluster)
    second = node_placement.rank_nodes_for_candidate(crm_requirement, top_cluster)
    assert [(n.host_name, n.overall_score, n.eligibility_status) for n in first] == [
        (n.host_name, n.overall_score, n.eligibility_status) for n in second
    ]


def test_best_host_really_has_the_most_headroom(crm_requirement, top_cluster):
    """Guards the ordering against a silent sign flip: with cost, reliability
    and risk tied across sibling hosts (they share a chargeback rate and an
    incident history in the seed), headroom is what decides, and more headroom
    must win.
    """
    ranked = [n for n in node_placement.rank_nodes_for_candidate(crm_requirement, top_cluster)
              if n.eligibility_status == "Eligible"]
    if len(ranked) < 2:
        pytest.skip("need at least two eligible hosts to compare")
    headrooms = [n.projected.projected_headroom_percent for n in ranked]
    assert headrooms[0] == max(headrooms)


def test_rejected_hosts_are_kept_and_sorted_after_eligible_ones(crm_requirement, top_cluster):
    ranked = node_placement.rank_nodes_for_candidate(crm_requirement, top_cluster)
    statuses = [n.eligibility_status for n in ranked]
    assert statuses == sorted(statuses, key=lambda s: s != "Eligible")
    # Every host in the cluster is accounted for - rejections are never dropped.
    assert len(ranked) == len(node_repository.get_by_cluster(top_cluster.cluster_id))


def test_top_n_caps_eligible_hosts_but_never_rejected(crm_requirement, top_cluster):
    full = node_placement.rank_nodes_for_candidate(crm_requirement, top_cluster)
    capped = node_placement.rank_nodes_for_candidate(crm_requirement, top_cluster, top_n=2)

    full_rejected = [n for n in full if n.eligibility_status != "Eligible"]
    capped_eligible = [n for n in capped if n.eligibility_status == "Eligible"]
    capped_rejected = [n for n in capped if n.eligibility_status != "Eligible"]

    assert len(capped_eligible) <= 2
    assert len(capped_rejected) == len(full_rejected)


def test_non_active_hosts_are_rejected_by_node_001(crm_requirement, top_cluster):
    ranked = node_placement.rank_nodes_for_candidate(crm_requirement, top_cluster)
    for n in ranked:
        if n.lifecycle_status != "Active":
            failed = [r["rule_id"] for r in n.rule_results if not r["passed"]]
            assert "NODE-001" in failed
            assert n.eligibility_status == "Rejected"


def test_every_ranked_host_records_the_placement_model(crm_requirement, top_cluster):
    ranked = node_placement.rank_nodes_for_candidate(crm_requirement, top_cluster)
    for n in ranked:
        assert n.evidence["placement_model"] in {"share", "whole"}
        assert n.evidence["share_denominator"] >= 1


# =============================================================================
# attach_top_nodes - the bounded drill-down used by the graph and the API
# =============================================================================


def test_attach_top_nodes_respects_the_configured_shortlist_size(crm_requirement):
    policy = get_settings().policy
    candidates = placement.find_and_score_candidates(crm_requirement, top_n=policy.top_clusters)
    node_placement.attach_top_nodes(crm_requirement, candidates)

    drilled = [c for c in candidates if c.top_nodes]
    assert len(drilled) <= policy.top_clusters
    for c in candidates:
        assert len(c.top_nodes) <= policy.top_nodes_per_cluster
        for n in c.top_nodes:
            assert n.eligibility_status == "Eligible", "only eligible hosts are proposed"
            assert n.cluster_id == c.cluster_id


def test_attach_top_nodes_never_drills_a_rejected_cluster(crm_requirement):
    candidates = placement.find_and_score_candidates(crm_requirement)
    node_placement.attach_top_nodes(crm_requirement, candidates)
    for c in candidates:
        if c.eligibility_status != "Eligible":
            assert c.top_nodes == []


def test_attach_top_nodes_honours_explicit_overrides(crm_requirement):
    candidates = placement.find_and_score_candidates(crm_requirement, top_n=5)
    node_placement.attach_top_nodes(
        crm_requirement, candidates, top_clusters=1, top_nodes_per_cluster=1
    )
    assert sum(1 for c in candidates if c.top_nodes) <= 1
    assert all(len(c.top_nodes) <= 1 for c in candidates)


# =============================================================================
# Node capacity snapshot arithmetic
# =============================================================================


def test_node_snapshot_applies_cluster_reservation_to_each_host(top_cluster):
    from app.services import capacity as capacity_service

    cluster = cluster_repository.get_by_id(top_cluster.cluster_id)
    node = node_repository.get_by_cluster(cluster.ClusterId)[0]
    snapshot = capacity_service.compute_node_capacity(node, cluster)

    expected_cpu = node.CpuCores * (Decimal("1") - cluster.ReservedCpuPercent / Decimal("100"))
    assert snapshot.effective_cpu_cores == capacity_service.round2(expected_cpu)
    assert snapshot.effective_storage_gb == capacity_service.round2(node.StorageGb)
    # available_* is rounded once, from the full-precision difference - exactly
    # how compute_cluster_capacity does it - so it can differ from
    # rounded_effective - rounded_consumed by a single cent.
    assert abs(
        snapshot.available_cpu_cores - (snapshot.effective_cpu_cores - snapshot.consumed_cpu_cores)
    ) <= Decimal("0.01")


def test_node_projection_does_not_reapply_growth(top_cluster):
    """The effective requirement is grown and margined once, at cluster level.
    A node projection that recomputed it would inflate every host's numbers.
    """
    from app.services import capacity as capacity_service

    cluster = cluster_repository.get_by_id(top_cluster.cluster_id)
    node = node_repository.get_by_cluster(cluster.ClusterId)[0]
    snapshot = capacity_service.compute_node_capacity(node, cluster)

    projected = capacity_service.compute_node_projected_utilization(
        snapshot,
        required_cpu_cores_effective=Decimal("2"),
        required_memory_gb_effective=Decimal("8"),
        required_storage_gb_effective=Decimal("100"),
    )
    assert projected.required_cpu_cores_effective == Decimal("2.00")
    assert projected.required_memory_gb_effective == Decimal("8.00")
    assert projected.required_storage_gb_effective == Decimal("100.00")
