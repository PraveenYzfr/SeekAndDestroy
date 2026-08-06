"""Workload consolidation analysis.

Identifies applications that could move off an overprovisioned/expensive
cluster onto an already-utilized, eligible target cluster without violating
capacity, availability, security, environment, dependency or platform
constraints (RULE-001..010 - the same hard-rule engine used for placement).

Savings are estimated from cost-per-effective-capacity-unit: moving off a
cluster that charges a premium per core/GB (typically the overprovisioned or
HIGH_COST_LOW_UTIL fixtures) onto a cheaper-per-unit, well-utilized cluster is
a real efficiency gain even before any follow-on node reduction on the
vacated cluster (see app.services.rightsizing for that half of the picture).
"""

from __future__ import annotations

from decimal import Decimal

from app.models.entities import CmdbApplication, InfrastructureCluster
from app.models.rightsizing import ConsolidationCandidate
from app.repositories import cluster_repository, hosting_repository
from app.scoring.subscores import round2
from app.services import placement, rightsizing


def _cost_per_cpu_unit(cluster: InfrastructureCluster) -> Decimal:
    if cluster.TotalCpuCores == 0:
        return Decimal("0")
    return cluster.MonthlyCost / cluster.TotalCpuCores


def evaluate_consolidation_for_application(app: CmdbApplication) -> ConsolidationCandidate | None:
    hostings = hosting_repository.get_active_for_application(app.ApplicationId)
    if not hostings:
        return None
    current_hosting = hostings[0]
    current_cluster = cluster_repository.get_by_id(current_hosting.ClusterId)
    if current_cluster is None:
        return None

    current_sizing = rightsizing.analyze_cluster_right_sizing(current_cluster)
    if current_sizing.classification != "Overprovisioned":
        return ConsolidationCandidate(
            application_id=app.ApplicationId, application_code=app.ApplicationCode,
            current_cluster_code=current_cluster.ClusterCode, target_cluster_code="",
            reason="Current cluster is not overprovisioned; no consolidation benefit identified.",
            estimated_monthly_savings=Decimal("0.00"), blocking_constraints=[], feasible=False,
        )

    requirement = placement.requirement_for_application(app)
    ranked = placement.find_and_score_candidates(requirement, exclude_cluster_id=current_cluster.ClusterId)

    for candidate in ranked:
        if candidate.eligibility_status != "Eligible":
            continue
        target_cluster = cluster_repository.get_by_id(candidate.cluster_id)
        if target_cluster is None:
            continue
        # A real consolidation target should already carry other workloads -
        # otherwise this is just a placement, not a consolidation.
        existing_hosting = hosting_repository.get_active_for_cluster(target_cluster.ClusterId)
        if len(existing_hosting) < 1:
            continue

        current_rate = _cost_per_cpu_unit(current_cluster)
        target_rate = _cost_per_cpu_unit(target_cluster)
        savings = round2(max(Decimal("0"), (current_rate - target_rate)) * app.CpuRequirement)

        return ConsolidationCandidate(
            application_id=app.ApplicationId, application_code=app.ApplicationCode,
            current_cluster_code=current_cluster.ClusterCode, target_cluster_code=target_cluster.ClusterCode,
            reason=(
                f"{current_cluster.ClusterCode} is overprovisioned "
                f"(CPU {current_sizing.snapshot.current_cpu_utilization_percent}%, "
                f"memory {current_sizing.snapshot.current_memory_utilization_percent}%); "
                f"{target_cluster.ClusterCode} is eligible, already hosts {len(existing_hosting)} other "
                f"workload(s), and offers a lower cost per CPU core."
            ),
            estimated_monthly_savings=savings, blocking_constraints=[], feasible=True,
        )

    blocking = []
    if ranked:
        top_rejected = next((c for c in ranked if c.eligibility_status == "Rejected"), None)
        if top_rejected:
            blocking = [r["reason"] for r in top_rejected.rule_results if not r["passed"]]
    return ConsolidationCandidate(
        application_id=app.ApplicationId, application_code=app.ApplicationCode,
        current_cluster_code=current_cluster.ClusterCode, target_cluster_code="",
        reason="No eligible, already-utilized consolidation target found.",
        estimated_monthly_savings=Decimal("0.00"), blocking_constraints=blocking, feasible=False,
    )


def find_consolidation_candidates(applications: list[CmdbApplication]) -> list[ConsolidationCandidate]:
    results = []
    for app in applications:
        result = evaluate_consolidation_for_application(app)
        if result is not None:
            results.append(result)
    return results
