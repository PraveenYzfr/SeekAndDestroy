from __future__ import annotations

from app.repositories import application_repository, cluster_repository
from app.services import placement, utilization_ranking


def test_least_used_ranking_is_ascending():
    ranked = utilization_ranking.rank_clusters_by_utilization(order="least", limit=5)
    values = [r.overall_utilization_percent for r in ranked]
    assert values == sorted(values)


def test_most_used_ranking_is_descending():
    ranked = utilization_ranking.rank_clusters_by_utilization(order="most", limit=5)
    values = [r.overall_utilization_percent for r in ranked]
    assert values == sorted(values, reverse=True)


def test_least_used_ranking_matches_lowest_seeded_utilization_cluster():
    # "Overprovisioned" is a capacity/headroom classification (see
    # test_right_sizing_preserves_required_headroom), not "lowest raw
    # utilization %" - the two are not guaranteed to correlate, especially
    # among 256 procedurally varied clusters. What IS a real invariant: the
    # single most-idle cluster by measured utilization must rank first.
    from app.services import capacity as capacity_service

    all_clusters = cluster_repository.list_all(limit=500)
    snapshots = {c.ClusterCode: capacity_service.compute_cluster_capacity(c) for c in all_clusters}

    def overall(code):
        s = snapshots[code]
        return max(
            s.current_cpu_utilization_percent, s.current_memory_utilization_percent,
            s.current_storage_utilization_percent,
        )

    expected_lowest = min((c.ClusterCode for c in all_clusters), key=overall)
    ranked = utilization_ranking.rank_clusters_by_utilization(order="least", limit=1)
    assert ranked[0].cluster_code == expected_lowest


def test_data_center_filter_only_returns_that_data_center():
    ranked = utilization_ranking.rank_clusters_by_utilization(data_center="Atlanta-DC1", limit=50)
    assert ranked  # Atlanta-DC1 has seeded clusters
    for r in ranked:
        assert r.data_center == "Atlanta-DC1"


def test_list_data_centers_returns_seeded_locations():
    dcs = utilization_ranking.list_data_centers()
    assert "Atlanta-DC1" in dcs
    assert "New York-DC1" in dcs
    assert dcs == sorted(dcs)


def test_top_n_caps_eligible_candidates_but_not_rejected():
    requirement_app = application_repository.get_by_code("APP-CRM")
    requirement = placement.requirement_for_application(requirement_app)
    full = placement.find_and_score_candidates(requirement)
    capped = placement.find_and_score_candidates(requirement, top_n=1)

    full_eligible = [c for c in full if c.eligibility_status == "Eligible"]
    capped_eligible = [c for c in capped if c.eligibility_status == "Eligible"]
    full_rejected = [c for c in full if c.eligibility_status != "Eligible"]
    capped_rejected = [c for c in capped if c.eligibility_status != "Eligible"]

    assert len(capped_eligible) <= 1
    if full_eligible:
        assert capped_eligible[0].cluster_code == full_eligible[0].cluster_code
    assert len(capped_rejected) == len(full_rejected)


def test_data_center_filter_on_find_and_score_candidates():
    requirement_app = application_repository.get_by_code("APP-CRM")
    requirement = placement.requirement_for_application(requirement_app)
    results = placement.find_and_score_candidates(requirement, data_center="Atlanta-DC1")
    for c in results:
        cluster = cluster_repository.get_by_id(c.cluster_id)
        assert cluster.DataCenter == "Atlanta-DC1"
