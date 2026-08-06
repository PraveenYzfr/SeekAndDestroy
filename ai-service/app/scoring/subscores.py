"""The seven 0-100 candidate sub-scores. See docs/scoring-model.md for the
full derivation of each formula; this module is the executable source of
truth those docs describe.

Every function is a pure function of its arguments - no database access, no
LLM calls - so scores are byte-identical across runs given the same inputs.
"""

from __future__ import annotations

import statistics
from decimal import ROUND_HALF_UP, Decimal

from app.models.capacity import ProjectedUtilization
from app.models.entities import InfrastructureCluster, Incident
from app.models.enums import INCIDENT_SEVERITY_WEIGHT
from app.models.requirements import HostingRequirement

TWOPLACES = Decimal("0.01")


def clamp_d(value: Decimal, lo: Decimal = Decimal("0"), hi: Decimal = Decimal("100")) -> Decimal:
    return max(lo, min(hi, value))


def round2(value) -> Decimal:
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(TWOPLACES, rounding=ROUND_HALF_UP)


# =============================================================================
# Capacity
# =============================================================================


def capacity_subscore(projected: ProjectedUtilization, *, target_headroom_percent: Decimal) -> Decimal:
    """clamp(headroom_pct / target_headroom_pct * 100, 0, 100)."""
    if target_headroom_percent <= 0:
        return Decimal("0")
    raw = (projected.projected_headroom_percent / target_headroom_percent) * Decimal("100")
    return round2(clamp_d(raw))


# =============================================================================
# Compatibility
# =============================================================================

EXACT_MATCH_SCORE = Decimal("100")
COMPATIBLE_SCORE = Decimal("82")
PREFERRED_LOCATION_MISMATCH_PENALTY = Decimal("10")


def compatibility_subscore(requirement: HostingRequirement, cluster: InfrastructureCluster) -> Decimal:
    exact_platform = requirement.platform == cluster.Platform
    score = EXACT_MATCH_SCORE if exact_platform else COMPATIBLE_SCORE
    if requirement.preferred_location and cluster.DataCenter != requirement.preferred_location:
        score -= PREFERRED_LOCATION_MISMATCH_PENALTY
    return round2(clamp_d(score))


# =============================================================================
# Resiliency
# =============================================================================

TIER_BASE_SCORE = {"Tier-1": Decimal("100"), "Tier-2": Decimal("80"), "Tier-3": Decimal("60")}
NODE_BONUS_PER_EXTRA_NODE = Decimal("5")
NODE_BONUS_CAP = Decimal("20")


def resiliency_subscore(
    cluster: InfrastructureCluster, active_node_count: int, *, min_required_nodes: int
) -> Decimal:
    base = TIER_BASE_SCORE.get(cluster.AvailabilityTier, Decimal("40"))
    extra_nodes = max(0, active_node_count - min_required_nodes)
    bonus = min(NODE_BONUS_CAP, Decimal(extra_nodes) * NODE_BONUS_PER_EXTRA_NODE)
    if active_node_count < min_required_nodes:
        # Structurally cannot back its own advertised tier - heavily penalized,
        # even though this candidate would already have failed RULE-010 for
        # Critical/High workloads. Kept for Medium/Low workloads where the
        # rule doesn't hard-reject but the risk is still real.
        base = base * Decimal("0.5")
    return round2(clamp_d(base + bonus))


# =============================================================================
# Cost efficiency - batch normalized across the eligible candidate set.
# =============================================================================


def cost_efficiency_scores(costs_by_cluster_id: dict[int, Decimal]) -> dict[int, Decimal]:
    if not costs_by_cluster_id:
        return {}
    values = list(costs_by_cluster_id.values())
    min_cost, max_cost = min(values), max(values)
    if max_cost == min_cost:
        return {cid: Decimal("100") for cid in costs_by_cluster_id}
    result = {}
    span = max_cost - min_cost
    for cid, cost in costs_by_cluster_id.items():
        raw = (max_cost - cost) / span * Decimal("100")
        result[cid] = round2(clamp_d(raw))
    return result


# =============================================================================
# Dependency locality
# =============================================================================

SAME_DC_SCORE = Decimal("100")
SAME_REGION_SCORE = Decimal("75")
CROSS_REGION_SCORE = Decimal("40")
UNKNOWN_SCORE = Decimal("0")


def dependency_locality_subscore(requirement: HostingRequirement, cluster: InfrastructureCluster) -> Decimal:
    checks = requirement.dependency_checks
    if not checks:
        return Decimal("100.00")

    def weight(dep) -> Decimal:
        w = Decimal("1")
        if dep.is_critical:
            w += Decimal("1")
        if dep.latency_sensitivity == "High":
            w += Decimal("1")
        elif dep.latency_sensitivity == "Medium":
            w += Decimal("0.5")
        return w

    total_weight = Decimal("0")
    weighted_score = Decimal("0")
    for dep in checks:
        w = weight(dep)
        if dep.target_data_center is None and dep.target_region is None:
            s = UNKNOWN_SCORE
        elif dep.target_data_center == cluster.DataCenter:
            s = SAME_DC_SCORE
        elif dep.target_region == cluster.Region:
            s = SAME_REGION_SCORE
        else:
            s = CROSS_REGION_SCORE
        weighted_score += w * s
        total_weight += w

    if total_weight == 0:
        return Decimal("100.00")
    return round2(clamp_d(weighted_score / total_weight))


# =============================================================================
# Historical performance
# =============================================================================


def historical_performance_subscore(incidents: list[Incident]) -> Decimal:
    total_weight = Decimal("0")
    for inc in incidents:
        total_weight += Decimal(str(INCIDENT_SEVERITY_WEIGHT.get(inc.Severity, 1.0)))
    # Each point of weighted incident load costs 2 score points - a single
    # Sev1 (weight 10) costs 20 points, a Sev4 (weight 1) costs 2.
    score = Decimal("100") - total_weight * Decimal("2")
    return round2(clamp_d(score))


# =============================================================================
# Operational risk (higher = worse; the overall formula uses 100 - risk)
# =============================================================================


def operational_risk_score(
    *,
    cpu_series_percent: list[float],
    memory_series_percent: list[float],
    lifecycle_status: str,
    open_severe_incident_count: int,
    forecast_breach_within_horizon: bool,
) -> Decimal:
    risk = Decimal("0")

    def volatility(series: list[float]) -> Decimal:
        if len(series) < 2:
            return Decimal("0")
        return Decimal(str(statistics.pstdev(series)))

    risk += clamp_d(volatility(cpu_series_percent), Decimal("0"), Decimal("20"))
    risk += clamp_d(volatility(memory_series_percent), Decimal("0"), Decimal("20"))

    if lifecycle_status == "Deprecated":
        risk += Decimal("30")

    risk += min(Decimal("30"), Decimal(open_severe_incident_count) * Decimal("15"))

    if forecast_breach_within_horizon:
        risk += Decimal("15")

    return round2(clamp_d(risk))
