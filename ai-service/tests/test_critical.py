"""The 13 critical tests named explicitly in the specification (§21), plus
test 14, added when recommendations were extended from clusters down to
individual hosts.

Each of the first 13 tests' docstrings is the spec's own sentence, verbatim,
so the mapping from requirement to test is unambiguous.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.agents.guards import NumberDriftError, assert_no_number_drift
from app.graph.graph import resume_investigation, run_investigation
from app.models.agent_contracts import CandidateExplanation
from app.models.requirements import HostingRequirement
from app.repositories import cluster_repository, recommendation_repository
from app.repositories.base import T, fetch_one
from app.services import capacity, placement, rightsizing


def _requirement(**overrides) -> HostingRequirement:
    base = dict(
        environment="Production", platform="Kubernetes", os_requirement="Any",
        cpu_cores=Decimal("8"), memory_gb=Decimal("32"), storage_gb=Decimal("500"),
        growth_percent=Decimal("0"), availability_tier="Tier-2", data_classification="Internal",
        criticality="Medium",
    )
    base.update(overrides)
    return HostingRequirement(**base)


# 1. Candidate without sufficient capacity is rejected.
def test_candidate_without_sufficient_capacity_is_rejected():
    cluster = cluster_repository.get_by_code("nyc-03")
    requirement = _requirement(cpu_cores=Decimal("100000"))  # impossibly large
    candidate, _ctx = placement.evaluate_candidate(requirement, cluster)
    assert candidate.eligibility_status == "Rejected"
    failed_ids = {r["rule_id"] for r in candidate.rule_results if not r["passed"]}
    assert "RULE-003" in failed_ids


# 2. Environment mismatch is rejected.
def test_environment_mismatch_is_rejected():
    cluster = cluster_repository.get_by_code("msp-09")
    requirement = _requirement(environment="Production")
    candidate, _ctx = placement.evaluate_candidate(requirement, cluster)
    assert candidate.eligibility_status == "Rejected"
    rule_001 = next(r for r in candidate.rule_results if r["rule_id"] == "RULE-001")
    assert rule_001["passed"] is False


# 3. Compliance mismatch is rejected.
def test_compliance_mismatch_is_rejected():
    cluster = cluster_repository.get_by_code("msp-03")  # Internal-only
    requirement = _requirement(data_classification="Restricted")
    candidate, _ctx = placement.evaluate_candidate(requirement, cluster)
    assert candidate.eligibility_status == "Rejected"
    rule_005 = next(r for r in candidate.rule_results if r["rule_id"] == "RULE-005")
    assert rule_005["passed"] is False


# 4. Projected utilization is calculated correctly.
def test_projected_utilization_is_calculated_correctly():
    cluster = cluster_repository.get_by_code("atl-03")
    snapshot = capacity.compute_cluster_capacity(cluster)
    requirement = _requirement(cpu_cores=Decimal("10"), memory_gb=Decimal("40"), storage_gb=Decimal("1000"), growth_percent=Decimal("0"))
    projected = capacity.compute_projected_utilization(
        snapshot, cluster, required_cpu=requirement.cpu_cores, required_memory_gb=requirement.memory_gb,
        required_storage_gb=requirement.storage_gb, growth_percent=requirement.growth_percent,
    )
    # No growth, 10% safety margin (default): required_effective = 10 * 1.10 = 11
    expected_cpu_eff = (Decimal("10") * Decimal("1.10"))
    assert projected.required_cpu_cores_effective == expected_cpu_eff.quantize(Decimal("0.01"))
    expected_pct = ((snapshot.consumed_cpu_cores + projected.required_cpu_cores_effective) / snapshot.effective_cpu_cores * 100).quantize(Decimal("0.01"))
    assert projected.projected_cpu_utilization_percent == expected_pct


# 5. Candidate scores are reproducible.
def test_candidate_scores_are_reproducible():
    requirement = _requirement(cpu_cores=Decimal("6"), memory_gb=Decimal("24"), storage_gb=Decimal("300"))
    first = placement.find_and_score_candidates(requirement)
    second = placement.find_and_score_candidates(requirement)
    first_scores = [(c.cluster_code, c.overall_score, c.eligibility_status) for c in first]
    second_scores = [(c.cluster_code, c.overall_score, c.eligibility_status) for c in second]
    assert first_scores == second_scores


# 6. Best candidate is ranked correctly.
def test_best_candidate_is_ranked_correctly():
    requirement = _requirement(cpu_cores=Decimal("4"), memory_gb=Decimal("16"), storage_gb=Decimal("200"))
    ranked = placement.find_and_score_candidates(requirement)
    eligible = [c for c in ranked if c.eligibility_status == "Eligible"]
    assert eligible, "expected at least one eligible candidate for this small, unconstrained requirement"

    scores = [c.overall_score for c in eligible]
    assert scores == sorted(scores, reverse=True), "eligible candidates must be sorted by score descending"

    for a, b in zip(eligible, eligible[1:]):
        if a.overall_score == b.overall_score:
            assert a.estimated_monthly_cost <= b.estimated_monthly_cost, "tied scores must break by cost ascending"

    ranks = [c.rank for c in ranked]
    assert ranks == sorted(ranks)


# 7. LLM cannot change numeric scores.
def test_llm_cannot_change_numeric_scores():
    # Cost is no longer part of the explanation contract - it is an internal
    # chargeback rate, not spend, so it is withheld from narration entirely.
    # The drift guard is unchanged; overall_score is what it now protects here.
    evidence = {"overall_score": 91.34, "cluster_code": "nyc-03", "eligibility_status": "Eligible"}
    bad_explanation = CandidateExplanation(
        cluster_code="nyc-03", eligibility_status="Eligible", overall_score=15.0,  # tampered
        summary="tampered",
    )
    with pytest.raises(NumberDriftError):
        assert_no_number_drift(bad_explanation, evidence)

    good_explanation = CandidateExplanation(
        cluster_code="nyc-03", eligibility_status="Eligible", overall_score=91.34,
        summary="consistent",
    )
    assert_no_number_drift(good_explanation, evidence)  # must not raise


# 8. Right-sizing recommendation preserves required headroom.
def test_right_sizing_preserves_required_headroom(scenarios):
    from app.config import get_settings

    settings = get_settings()
    for code in scenarios["overprovisioned_clusters"]:
        cluster = cluster_repository.get_by_code(code)
        result = rightsizing.analyze_cluster_right_sizing(cluster)
        if result.node_delta >= 0:
            continue
        per_node_cpu = cluster.TotalCpuCores / result.current_node_count
        per_node_mem = cluster.TotalMemoryGb / result.current_node_count
        remaining_cpu = per_node_cpu * result.recommended_node_count * (1 - cluster.ReservedCpuPercent / 100)
        remaining_mem = per_node_mem * result.recommended_node_count * (1 - cluster.ReservedMemoryPercent / 100)
        cpu_pct_after = result.snapshot.consumed_cpu_cores / remaining_cpu * 100
        mem_pct_after = result.snapshot.consumed_memory_gb / remaining_mem * 100
        assert cpu_pct_after < Decimal(str(settings.policy.cpu_threshold_percent))
        assert mem_pct_after < Decimal(str(settings.policy.memory_threshold_percent))


# 9. Critical application resiliency requirements are enforced.
def test_critical_application_resiliency_is_enforced(scenarios):
    for code in scenarios["insufficient_resiliency_clusters"]:
        cluster = cluster_repository.get_by_code(code)
        requirement = _requirement(criticality="Critical", availability_tier="Tier-1")
        candidate, _ctx = placement.evaluate_candidate(requirement, cluster)
        assert candidate.eligibility_status == "Rejected"
        rule_010 = next(r for r in candidate.rule_results if r["rule_id"] == "RULE-010")
        assert rule_010["passed"] is False


# 10. No infrastructure modification is executed.
def test_no_infrastructure_modification_tool_exists():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mcp-server"))
    import server as mcp_server

    tool_names = set()

    async def _collect():
        from mcp import Client

        async with Client(mcp_server.server) as client:
            listed = await client.list_tools()
            return {t.name for t in listed.tools}

    import asyncio

    tool_names = asyncio.run(_collect())

    forbidden_substrings = ["execute_sql", "provision", "decommission", "migrate_", "delete_cluster", "delete_node", "create_cluster", "create_node"]
    for name in tool_names:
        for forbidden in forbidden_substrings:
            assert forbidden not in name.lower(), f"tool {name!r} looks like an infrastructure-mutation tool"

    # The only write tools present are governance-table tools.
    write_tools = {"create_capacity_request", "create_investigation", "save_recommendation", "submit_recommendation_decision"}
    assert write_tools.issubset(tool_names)


# 11. Human-review interrupt works.
def test_human_review_interrupt_works():
    result = run_investigation(query="Find the best clusters for hosting APP-ONBOARDING.", created_by=1)
    assert result["status"] == "AwaitingReview"
    assert result.get("review_payload") is not None


# 12. Graph resumes after review.
def test_graph_resumes_after_review():
    started = run_investigation(query="Find the best clusters for hosting APP-CRM.", created_by=1)
    assert started["status"] == "AwaitingReview"
    resumed = resume_investigation(
        investigation_id=started["investigation_id"], decision="Approve", reviewer_employee_id=1, comments="ok"
    )
    assert resumed["status"] == "Completed"
    row = fetch_one(f"SELECT Status FROM {T('Investigation')} WHERE InvestigationId = :id", {"id": started["investigation_id"]})
    assert row["Status"] == "Completed"


# 13. Every recommendation includes evidence.
def test_every_recommendation_includes_evidence():
    started = run_investigation(query="Find the best clusters for hosting APP-KYC.", created_by=1)
    if started["status"] == "AwaitingReview":
        resume_investigation(investigation_id=started["investigation_id"], decision="Approve", reviewer_employee_id=1, comments=None)
    recs = recommendation_repository.list_for_investigation(started["investigation_id"])
    assert recs, "expected at least one persisted recommendation"
    for rec in recs:
        assert rec.EvidenceJson is not None
        assert len(rec.EvidenceJson) > 0


# 14. A persisted shortlist names hosts, not just clusters, and comes back in
#     display order: each cluster immediately followed by its own hosts.
def test_persisted_shortlist_groups_hosts_under_their_cluster():
    import json

    from app.config import get_settings

    policy = get_settings().policy
    started = run_investigation(query="Find the best clusters for hosting APP-CRM.", created_by=1)
    if started["status"] == "AwaitingReview":
        resume_investigation(
            investigation_id=started["investigation_id"], decision="Approve",
            reviewer_employee_id=1, comments=None,
        )
    recs = recommendation_repository.list_for_investigation(started["investigation_id"])

    clusters = [r for r in recs if r.CandidateEntityType == "Cluster"]
    nodes = [r for r in recs if r.CandidateEntityType == "Node"]
    assert clusters, "expected persisted cluster rows"
    assert nodes, "expected persisted host rows - the shortlist must reach node level"
    assert len(clusters) <= policy.top_clusters
    assert len(nodes) <= policy.top_clusters * policy.top_nodes_per_cluster

    # Rows arrive grouped: every Node row's parent is the most recent Cluster
    # row above it, and node ranks restart at 1 inside each cluster.
    current_cluster, seen_ranks = None, []
    for r in recs:
        if r.CandidateEntityType == "Cluster":
            current_cluster = json.loads(r.EvidenceJson)["cluster_code"]
            seen_ranks = []
            continue
        evidence = json.loads(r.EvidenceJson)
        assert evidence["parent_cluster_code"] == current_cluster
        seen_ranks.append(r.Rank)
        assert seen_ranks == list(range(1, len(seen_ranks) + 1))
        # Cluster-level sub-scores are the parent's to record, not the host's.
        assert r.CompatibilityScore is None
        assert r.ResiliencyScore is None
        assert r.DependencyScore is None
