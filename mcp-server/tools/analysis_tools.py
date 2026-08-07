"""Deterministic analysis tools - the MCP surface over app/services, app/rules,
app/scoring and app/forecasting. No tool in this module lets the LLM alter a
number; each one runs the real engine and returns its output verbatim.
"""

from __future__ import annotations

from decimal import Decimal

from tools._audit import audited, model_list

from app.config import get_settings
from app.forecasting.engine import forecast_cluster
from app.models.requirements import HostingRequirement
from app.repositories import application_repository, cluster_repository
from app.services import (
    capacity,
    consolidation,
    node_placement,
    placement,
    rightsizing,
    utilization_ranking,
)
from app.utils.json_utils import to_jsonable


def calculate_projected_utilization(
    cluster_code: str, cpu_cores: float, memory_gb: float, storage_gb: float, growth_percent: float = 0.0
) -> dict:
    """Compute projected utilization on a cluster after adding a hypothetical workload."""
    params = dict(locals())

    def run():
        cluster = cluster_repository.get_by_code(cluster_code)
        if cluster is None:
            return {"error": f"cluster {cluster_code} not found"}
        snapshot = capacity.compute_cluster_capacity(cluster)
        projected = capacity.compute_projected_utilization(
            snapshot, cluster, required_cpu=Decimal(str(cpu_cores)), required_memory_gb=Decimal(str(memory_gb)),
            required_storage_gb=Decimal(str(storage_gb)), growth_percent=Decimal(str(growth_percent)),
        )
        return {"snapshot": to_jsonable(snapshot), "projected": to_jsonable(projected)}

    return audited("calculate_projected_utilization", params, run)


def _requirement_from_args(application_code: str, cpu_cores, memory_gb, storage_gb, platform, environment,
                            availability_tier, data_classification, growth_percent, preferred_location):
    if application_code:
        app = application_repository.get_by_code(application_code)
        if app is None:
            return None, {"error": f"application {application_code} not found"}
        return placement.requirement_for_application(app), None
    if None in (cpu_cores, memory_gb, storage_gb, platform, environment, availability_tier, data_classification):
        return None, {"error": "either application_code or the full raw requirement fields must be provided"}
    req = HostingRequirement(
        environment=environment, platform=platform, os_requirement="Any",
        cpu_cores=Decimal(str(cpu_cores)), memory_gb=Decimal(str(memory_gb)), storage_gb=Decimal(str(storage_gb)),
        growth_percent=Decimal(str(growth_percent or 0.0)), availability_tier=availability_tier,
        data_classification=data_classification, preferred_location=preferred_location or None,
        criticality="Medium",
    )
    return req, None


def find_eligible_hosting_candidates(
    application_code: str = "", cpu_cores: float | None = None, memory_gb: float | None = None,
    storage_gb: float | None = None, platform: str = "", environment: str = "",
    availability_tier: str = "", data_classification: str = "", growth_percent: float = 0.0,
    preferred_location: str = "", data_center: str = "",
) -> dict:
    """Run RULE-001..010 against every active cluster and return eligible vs rejected candidates
    (no scoring - see score_hosting_candidates for the ranked version). Set data_center to
    restrict candidates to one data center."""
    params = dict(locals())

    def run():
        req, error = _requirement_from_args(
            application_code, cpu_cores, memory_gb, storage_gb, platform or None, environment or None,
            availability_tier or None, data_classification or None, growth_percent, preferred_location,
        )
        if error:
            return error
        clusters = placement.discover_candidate_clusters(req, data_center=data_center or None)
        eligible, rejected = [], []
        for cluster in clusters:
            candidate, _ctx = placement.evaluate_candidate(req, cluster)
            target = eligible if candidate.eligibility_status == "Eligible" else rejected
            target.append(to_jsonable(candidate))
        return {"eligible": eligible, "rejected": rejected}

    return audited("find_eligible_hosting_candidates", params, run)


def score_hosting_candidates(
    application_code: str = "", cpu_cores: float | None = None, memory_gb: float | None = None,
    storage_gb: float | None = None, platform: str = "", environment: str = "",
    availability_tier: str = "", data_classification: str = "", growth_percent: float = 0.0,
    preferred_location: str = "", data_center: str = "", top_n: int = 0,
    top_nodes_per_cluster: int = 0, include_nodes: bool = True,
) -> dict:
    """Run the full eligibility + weighted-scoring pipeline and return ranked candidates,
    each carrying its best individual hosts under `top_nodes`. Set data_center to restrict
    candidates to one data center (an engineer picking a location first). Set top_n to cap
    how many eligible clusters come back and top_nodes_per_cluster to cap the hosts ranked
    inside each (both default to policy: 3 and 3). Rejected clusters are never truncated.
    Set include_nodes=False for cluster-level results only."""
    params = dict(locals())

    def run():
        req, error = _requirement_from_args(
            application_code, cpu_cores, memory_gb, storage_gb, platform or None, environment or None,
            availability_tier or None, data_classification or None, growth_percent, preferred_location,
        )
        if error:
            return error
        effective_top_n = top_n or get_settings().policy.top_clusters
        ranked = placement.find_and_score_candidates(
            req, data_center=data_center or None, top_n=effective_top_n,
        )
        if include_nodes:
            node_placement.attach_top_nodes(
                req, ranked,
                top_clusters=effective_top_n,
                top_nodes_per_cluster=top_nodes_per_cluster or None,
            )
        return {"candidates": model_list(ranked)}

    return audited("score_hosting_candidates", params, run)


def rank_clusters_by_utilization(
    order: str = "least", limit: int = 10, environment: str = "", data_center: str = "", platform: str = "",
) -> dict:
    """List clusters ranked by current utilization - order='least' surfaces the most idle/
    overprovisioned clusters first, order='most' surfaces the busiest ones first. Optionally
    filtered by environment, data center or platform."""
    params = dict(locals())

    def run():
        if order not in ("least", "most"):
            return {"error": "order must be 'least' or 'most'"}
        results = utilization_ranking.rank_clusters_by_utilization(
            order=order, limit=limit, environment=environment or None,
            data_center=data_center or None, platform=platform or None,
        )
        return {"results": model_list(results)}

    return audited("rank_clusters_by_utilization", params, run)


def run_cluster_right_sizing_analysis(cluster_code: str = "") -> dict:
    """Right-size one cluster (if cluster_code given) or every active cluster."""
    params = dict(locals())

    def run():
        if cluster_code:
            cluster = cluster_repository.get_by_code(cluster_code)
            if cluster is None:
                return {"error": f"cluster {cluster_code} not found"}
            clusters = [cluster]
        else:
            clusters = cluster_repository.list_all(limit=500)
        return {"results": model_list([rightsizing.analyze_cluster_right_sizing(c) for c in clusters])}

    return audited("run_cluster_right_sizing_analysis", params, run)


def run_application_right_sizing_analysis(application_code: str = "") -> dict:
    """Right-size one application's allocation (if application_code given) or all applications."""
    params = dict(locals())

    def run():
        if application_code:
            app = application_repository.get_by_code(application_code)
            if app is None:
                return {"error": f"application {application_code} not found"}
            apps = [app]
        else:
            apps = application_repository.list_all(limit=200)
        results = [rightsizing.analyze_application_right_sizing(a) for a in apps]
        return {"results": model_list([r for r in results if r is not None])}

    return audited("run_application_right_sizing_analysis", params, run)


def run_consolidation_analysis(environment: str = "") -> dict:
    """Find applications that can be consolidated off overprovisioned clusters onto eligible, already-utilized ones."""
    params = dict(locals())

    def run():
        apps = application_repository.search(environment=environment or None, limit=200) if environment else application_repository.list_all(limit=200)
        results = consolidation.find_consolidation_candidates(apps)
        return {"results": model_list(results)}

    return audited("run_consolidation_analysis", params, run)


def run_capacity_forecast(cluster_code: str, horizon_days: int = 90) -> dict:
    """Deterministic OLS capacity forecast for a cluster (30/60/90/180-day horizons supported)."""
    params = dict(locals())

    def run():
        cluster = cluster_repository.get_by_code(cluster_code)
        if cluster is None:
            return {"error": f"cluster {cluster_code} not found"}
        result = forecast_cluster(cluster, horizon_days=horizon_days)
        return to_jsonable(result)

    return audited("run_capacity_forecast", params, run)
