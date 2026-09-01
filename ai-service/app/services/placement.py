"""Hosting-placement orchestration: candidate discovery -> eligibility ->
scoring -> ranking. This is the deterministic core behind Scenario A/B
(find hosting candidates for an application or a raw capacity requirement)
and is what the MCP tools ``find_eligible_hosting_candidates`` /
``score_hosting_candidates`` and the FastAPI hosting endpoints call.

Nothing in this module is an LLM call - see app/agents for the narration
layer that runs on top of this module's output.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal

from app.config import get_settings
from app.forecasting.engine import cluster_breaches_within_horizon
from app.models.capacity import ClusterCapacitySnapshot, ProjectedUtilization
from app.models.entities import CmdbApplication, InfrastructureCluster
from app.models.requirements import DependencyLocalityCheck, HostingRequirement
from app.models.scoring import CandidateScore, SubScores
from app.repositories import (
    application_repository,
    cluster_repository,
    dependency_repository,
    hosting_repository,
    incident_repository,
    node_repository,
    utilization_repository,
)
from app.rules import eligibility
from app.scoring import subscores
from app.scoring.engine import compute_overall_score, rank_candidates
from app.scoring.subscores import round2
from app.services import capacity

import structlog

logger = structlog.get_logger(__name__)


def resolve_dependency_checks(application_id: int) -> list[DependencyLocalityCheck]:
    outbound = dependency_repository.get_outbound(application_id)
    checks: list[DependencyLocalityCheck] = []
    for dep in outbound:
        region = data_center = None
        desc = "unknown target"
        if dep.TargetApplicationId:
            target_app = application_repository.get_by_id(dep.TargetApplicationId)
            desc = target_app.ApplicationCode if target_app else f"application #{dep.TargetApplicationId}"
            hostings = hosting_repository.get_active_for_application(dep.TargetApplicationId)
            if hostings:
                target_cluster = cluster_repository.get_by_id(hostings[0].ClusterId)
                if target_cluster:
                    region, data_center = target_cluster.Region, target_cluster.DataCenter
        elif dep.TargetClusterId:
            target_cluster = cluster_repository.get_by_id(dep.TargetClusterId)
            if target_cluster:
                desc = f"cluster {target_cluster.ClusterCode}"
                region, data_center = target_cluster.Region, target_cluster.DataCenter
        checks.append(
            DependencyLocalityCheck(
                dependency_id=dep.DependencyId,
                dependency_type=dep.DependencyType,
                is_critical=dep.IsCritical,
                latency_sensitivity=dep.LatencySensitivity,
                target_description=desc,
                target_region=region,
                target_data_center=data_center,
            )
        )
    return checks


def requirement_for_application(app: CmdbApplication) -> HostingRequirement:
    checks = resolve_dependency_checks(app.ApplicationId)
    return HostingRequirement.from_application(app, checks)


def discover_candidate_clusters(
    requirement: HostingRequirement, *, exclude_cluster_id: int | None = None,
    data_center: str | None = None, limit: int = 400,
) -> list[InfrastructureCluster]:
    """Narrows the estate with a cheap SQL-level environment filter before any
    per-candidate work happens - at 256 clusters, evaluating every one of
    them (each candidate needs several queries: capacity snapshot, node
    count, incidents, utilization series, forecast) on every hosting request
    would be needlessly slow. RULE-001 requires an environment match anyway,
    so this isn't a shortcut around the rule - it's just doing that specific
    check once, in SQL, instead of once per candidate in Python.

    Platform compatibility is filtered in Python after the DB round-trip
    (dict lookups are effectively free; the repository's single-value
    ``platform=`` filter can't express "any compatible platform" in one
    query).
    """
    from app.models.enums import PLATFORM_COMPATIBILITY, PRODUCTION_ENVIRONMENTS

    environment = requirement.environment if requirement.environment not in PRODUCTION_ENVIRONMENTS else "Production"
    candidates = cluster_repository.search(
        environment=environment, exclude_ineligible_lifecycle=True, data_center=data_center, limit=limit,
    )
    compatible_platforms = PLATFORM_COMPATIBILITY.get(requirement.platform)
    if compatible_platforms:
        candidates = [c for c in candidates if c.Platform in compatible_platforms]
    if exclude_cluster_id is not None:
        candidates = [c for c in candidates if c.ClusterId != exclude_cluster_id]
    return candidates


def _min_required_nodes(requirement: HostingRequirement) -> int:
    settings = get_settings()
    if requirement.criticality == "Critical":
        return settings.policy.min_nodes_tier1
    if requirement.criticality == "High":
        return settings.policy.min_nodes_tier2
    return 1


def estimate_monthly_cost(cluster: InfrastructureCluster, requirement: HostingRequirement) -> Decimal:
    cpu_share = requirement.cpu_cores / cluster.TotalCpuCores if cluster.TotalCpuCores else Decimal("0")
    mem_share = requirement.memory_gb / cluster.TotalMemoryGb if cluster.TotalMemoryGb else Decimal("0")
    share = max(cpu_share, mem_share)
    return round2(cluster.MonthlyCost * share)


def build_eligibility_context(
    requirement: HostingRequirement,
    cluster: InfrastructureCluster,
    change_risk: dict | None = None,
    resiliency_profile: dict | None = None,
) -> tuple[eligibility.EligibilityContext, ClusterCapacitySnapshot]:
    """``change_risk`` is this cluster's row from
    itsm_repository.change_risk_for_clusters. Passed in rather than fetched here
    because a placement run scores up to 256 clusters and that query is designed
    to answer for all of them at once - fetching per cluster would turn one query
    into 256 on the hot path.

    Optional, and None is a valid answer: RULE-011 passes without it, so a caller
    that has no change data still gets a complete evaluation.

    ``resiliency_profile`` is the same arrangement for RULE-012, with one
    difference worth noting: change risk is per CLUSTER and so is a dict keyed by
    cluster id, whereas resiliency is a property of the APPLICATION being placed.
    It is therefore the same object for every candidate in a run, fetched once,
    and it does not vary as the loop walks the estate.
    """
    snapshot = capacity.compute_cluster_capacity(cluster)
    projected = capacity.compute_projected_utilization(
        snapshot, cluster,
        required_cpu=requirement.cpu_cores, required_memory_gb=requirement.memory_gb,
        required_storage_gb=requirement.storage_gb, growth_percent=requirement.growth_percent,
    )
    active_nodes = node_repository.count_active_by_cluster(cluster.ClusterId)
    ctx = eligibility.EligibilityContext(
        requirement=requirement, cluster=cluster, projected=projected,
        active_node_count=active_nodes, change_risk=change_risk,
        resiliency_profile=resiliency_profile,
    )
    return ctx, snapshot


def evaluate_candidate(
    requirement: HostingRequirement,
    cluster: InfrastructureCluster,
    change_risk: dict | None = None,
    resiliency_profile: dict | None = None,
) -> tuple[CandidateScore, eligibility.EligibilityContext]:
    ctx, snapshot = build_eligibility_context(
        requirement, cluster, change_risk, resiliency_profile
    )
    rule_results = eligibility.evaluate_all(ctx, snapshot)
    eligible = eligibility.is_eligible(rule_results)
    cost = estimate_monthly_cost(cluster, requirement)
    candidate = CandidateScore(
        cluster_id=cluster.ClusterId,
        cluster_code=cluster.ClusterCode,
        eligibility_status="Eligible" if eligible else "Rejected",
        rule_results=[asdict(r) for r in rule_results],
        snapshot=snapshot,
        projected=ctx.projected,
        estimated_monthly_cost=cost,
    )
    return candidate, ctx


_AVG_TARGET_HEADROOM_CACHE: Decimal | None = None


def _average_target_headroom() -> Decimal:
    global _AVG_TARGET_HEADROOM_CACHE
    if _AVG_TARGET_HEADROOM_CACHE is None:
        settings = get_settings()
        targets = [
            Decimal("100") - capacity.d(settings.policy.cpu_threshold_percent),
            Decimal("100") - capacity.d(settings.policy.memory_threshold_percent),
            Decimal("100") - capacity.d(settings.policy.storage_threshold_percent),
        ]
        _AVG_TARGET_HEADROOM_CACHE = round2(sum(targets) / len(targets))
    return _AVG_TARGET_HEADROOM_CACHE


def score_candidate(
    requirement: HostingRequirement,
    cluster: InfrastructureCluster,
    ctx: eligibility.EligibilityContext,
    candidate: CandidateScore,
    cost_score: Decimal,
) -> CandidateScore:
    min_nodes = _min_required_nodes(requirement)
    cap_score = subscores.capacity_subscore(ctx.projected, target_headroom_percent=_average_target_headroom())
    compat_score = subscores.compatibility_subscore(requirement, cluster)
    resil_score = subscores.resiliency_subscore(cluster, ctx.active_node_count, min_required_nodes=min_nodes)
    dep_score = subscores.dependency_locality_subscore(requirement, cluster)

    incidents = incident_repository.get_recent_for_cluster(cluster.ClusterId, 90)
    hist_score = subscores.historical_performance_subscore(incidents)

    # Reads ctx.change_risk rather than querying: the caller fetched it for
    # every candidate in one query. A cluster with no change record scores 100,
    # meaning "nothing known against it" - see the subscore's own note on why
    # that is the weakest claim here.
    change_score = subscores.change_risk_subscore(ctx.change_risk)

    series = utilization_repository.get_cluster_series(cluster.ClusterId, 30)
    cpu_series = [float(s.CpuUsedPercent) for s in series]
    mem_series = [float(s.MemoryUsedPercent) for s in series]
    open_severe = incident_repository.get_open_severe_for_cluster(cluster.ClusterId)
    forecast_breach = cluster_breaches_within_horizon(cluster, horizon_days=90)

    risk_score = subscores.operational_risk_score(
        cpu_series_percent=cpu_series, memory_series_percent=mem_series,
        lifecycle_status=cluster.LifecycleStatus, open_severe_incident_count=len(open_severe),
        forecast_breach_within_horizon=forecast_breach,
    )

    sub = SubScores(
        capacity=cap_score, compatibility=compat_score, resiliency=resil_score,
        cost=cost_score, dependency=dep_score, historical=hist_score, risk=risk_score,
        change_risk=change_score,
    )
    candidate.subscores = sub
    candidate.overall_score = compute_overall_score(sub)
    candidate.evidence = {
        "active_node_count": ctx.active_node_count,
        "min_required_nodes": min_nodes,
        "forecast_breach_within_90d": forecast_breach,
        "open_severe_incidents": len(open_severe),
        # The change figures behind change_risk, so a ranking can be explained
        # without re-running the query that produced it.
        "upcoming_changes": (ctx.change_risk or {}).get("upcoming_changes", 0),
        "recent_change_failures": (ctx.change_risk or {}).get("recent_failures", 0),
    }
    return candidate


def find_and_score_candidates(
    requirement: HostingRequirement, *, exclude_cluster_id: int | None = None,
    data_center: str | None = None, top_n: int | None = None,
) -> list[CandidateScore]:
    """``top_n``, when set, caps how many *eligible* candidates are returned
    (an infra engineer asking "top 5 best clusters" shouldn't have to scroll
    past all 15). Rejected candidates are never truncated - "why was X
    rejected" stays answerable regardless of top_n.
    """
    clusters = discover_candidate_clusters(requirement, exclude_cluster_id=exclude_cluster_id, data_center=data_center)

    # One query for every candidate's change record, before the loop. Fetching
    # inside evaluate_candidate would issue 256 queries on the hot path to
    # answer a question designed to be answered once. RULE-011 and the
    # change-risk sub-score both read from this.
    #
    # Best-effort: on an estate without the ITSM tables, or if the query fails,
    # every cluster simply has no change record - RULE-011 passes and the
    # sub-score returns 100. Placement must not stop working because change
    # management data is missing.
    change_risk: dict[int, dict] = {}
    try:
        from app.repositories import itsm_repository

        change_risk = itsm_repository.change_risk_for_clusters([c.ClusterId for c in clusters])
    except Exception as exc:  # noqa: BLE001
        logger.warning("placement.change_risk_unavailable", error=str(exc))

    # The application's CMDB resiliency, fetched once. Unlike change risk this
    # does not vary per candidate - it describes the workload being placed, not
    # the cluster being considered - so one graph walk serves the whole run.
    #
    # Best-effort for the same reason as change risk, and it matters more here:
    # RULE-012 rejects Critical and High workloads, so a failure that produced a
    # partial profile rather than none could reject the entire estate on bad
    # data. None is unambiguous - the rule declines to assess and passes.
    resiliency_profile: dict | None = None
    app_code = requirement.source_application_code
    if app_code:
        try:
            from app.services import resiliency as _resiliency

            resiliency_profile = _resiliency.profile_for_application(app_code).to_rule_input()
        except Exception as exc:  # noqa: BLE001
            logger.warning("placement.resiliency_unavailable", application=app_code, error=str(exc))

    evaluated: list[tuple[CandidateScore, eligibility.EligibilityContext, InfrastructureCluster]] = []
    for cluster in clusters:
        candidate, ctx = evaluate_candidate(
            requirement, cluster, change_risk.get(cluster.ClusterId), resiliency_profile
        )
        evaluated.append((candidate, ctx, cluster))

    eligible_triples = [t for t in evaluated if t[0].eligibility_status == "Eligible"]
    costs = {c.cluster_id: c.estimated_monthly_cost for c, _, _ in eligible_triples}
    cost_scores = subscores.cost_efficiency_scores(costs)

    for candidate, ctx, cluster in eligible_triples:
        score_candidate(requirement, cluster, ctx, candidate, cost_scores.get(cluster.ClusterId, Decimal("0")))

    all_candidates = [c for c, _, _ in evaluated]
    ranked = rank_candidates(all_candidates)

    if top_n is not None and top_n > 0:
        eligible = [c for c in ranked if c.eligibility_status == "Eligible"][:top_n]
        rejected = [c for c in ranked if c.eligibility_status != "Eligible"]
        return eligible + rejected

    return ranked
