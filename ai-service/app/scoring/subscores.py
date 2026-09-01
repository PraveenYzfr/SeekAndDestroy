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


def node_capacity_subscore(projected: ProjectedUtilization) -> Decimal:
    """clamp(headroom_pct, 0, 100) - the remaining headroom, used directly.

    Deliberately *not* :func:`capacity_subscore`. That one divides by a target
    headroom (20% by default) and clamps at 100, which is right when comparing
    clusters across the estate but useless when comparing siblings: every host
    that clears the target saturates at 100 and the ranking collapses to
    alphabetical order by hostname. Hosts inside one cluster are near-identical
    by construction, so the sub-score has to preserve small real differences
    rather than flatten them.

    Consequence worth knowing when reading a number: node scores are only
    meaningful *relative to other nodes in the same cluster*. A host at 63 is
    not "worse" than its cluster at 91 - the two are on different scales.
    """
    return round2(clamp_d(projected.projected_headroom_percent))


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
    """Min-max normalization over whatever entity ids are passed in. Keyed by
    cluster id for cluster candidates and by node id for node candidates - the
    formula does not care, but the *set* matters: nodes are normalized within
    their own cluster, so a node's cost score is relative to its siblings, not
    to the whole estate.
    """
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


def node_operational_risk_score(
    *,
    lifecycle_status: str,
    open_severe_incident_count: int,
    staleness_days: int,
    stale_after_days: int,
    has_measurements: bool,
) -> Decimal:
    """Node-level risk (higher = worse; the node formula uses ``100 - risk``).

    Deliberately not the cluster ``operational_risk_score``: that one is driven
    by utilization *volatility*, which would mean pulling a full 30-day series
    per node (hundreds of rows x every node in every shortlisted cluster) to
    order at most a handful of siblings. The node signals that actually
    discriminate are reporting freshness, lifecycle and open severe incidents.
    """
    risk = Decimal("0")

    if lifecycle_status == "Deprecated":
        risk += Decimal("30")
    elif lifecycle_status != "Active":
        risk += Decimal("15")

    risk += min(Decimal("40"), Decimal(open_severe_incident_count) * Decimal("20"))

    # A node that stopped reporting is a node nobody can vouch for. Ramps to
    # the full 25 points over one further stale window.
    if stale_after_days > 0 and staleness_days > stale_after_days:
        overdue = Decimal(staleness_days - stale_after_days) / Decimal(stale_after_days)
        risk += min(Decimal("25"), overdue * Decimal("25"))

    if not has_measurements:
        risk += Decimal("20")

    return round2(clamp_d(risk))


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


#: Pseudo-changes added to the denominator of the failure rate.
#:
#: A raw rate treats one failed change out of one as a 100% failure rate and
#: ranks that cluster below one with four failures out of forty (10%). The first
#: is one bad afternoon; the second is a pattern. Dividing by (recent + 5)
#: shrinks thin evidence toward zero rather than toward certainty:
#:
#:      1 of 1   ->  1/6  = 0.17     one bad change, treated as weak evidence
#:      4 of 5   ->  4/10 = 0.40     small sample, mostly bad - ranked badly
#:      4 of 40  ->  4/45 = 0.09     a real rate, on real volume
#:
#: The alternative - shrinking toward the estate mean - would make a cluster with
#: no change history score like an average cluster rather than an unproven one,
#: and "we have never changed it" is not evidence of stability.
_CHANGE_FAILURE_PRIOR = Decimal("5")

#: Score lost per change scheduled in the eligibility window. Capped below so a
#: cluster with fifteen planned changes and one with fifty are both simply
#: "heavily churned" - past a point the difference stops informing the decision.
_UPCOMING_CHANGE_PENALTY = Decimal("12")
_MAX_UPCOMING_PENALTY = Decimal("60")


def change_risk_subscore(risk: dict | None) -> Decimal:
    """How safe this cluster looks from its change record. 100 is safest.

    Two independent signals, because they fail differently:

      upcoming changes   churn the workload would land in the middle of. A
                         forward-looking fact, known with certainty.
      failure rate       how often changes here go wrong. Backward-looking and
                         statistical, so it is smoothed - see the prior above.

    A cluster with no change record at all scores 100. That is deliberate and it
    is the weakest claim here: it means "nothing known against it", not "proven
    stable". Scoring it lower would penalise every cluster the change process
    has not touched, which is most of a healthy estate.

    The churn penalty is then weighted by how much depends on this cluster - see
    services.change_exposure. Queued changes on a cluster 29 applications rely on
    are not the same risk as the same changes on one nothing touches, and until
    that weighting existed the two scored identically.

    The weighting applies to churn only. The failure rate is an observed outcome
    and already reflects a cluster's importance, since busy clusters accumulate
    more change history; scaling it by exposure as well would compound a
    correlation into a penalty.
    """
    if not risk:
        return Decimal("100.00")

    upcoming = Decimal(str(risk.get("upcoming_changes") or 0))
    recent = Decimal(str(risk.get("recent_changes") or 0))
    failures = Decimal(str(risk.get("recent_failures") or 0))

    # Exposure weighting is applied BEFORE the cap, so the cap remains the real
    # ceiling on churn. Applying it after would let a hub cluster exceed a bound
    # that exists to stop any single dimension dominating six others.
    from app.services.change_exposure import exposure_multiplier

    weighted = upcoming * _UPCOMING_CHANGE_PENALTY * exposure_multiplier(
        risk.get("dependent_applications")
    )
    churn_penalty = min(weighted, _MAX_UPCOMING_PENALTY)

    # Recomputed here rather than trusting a failure_rate supplied by the query:
    # the smoothing is a scoring decision, and a raw rate arriving from the
    # database would silently bypass it.
    smoothed_rate = failures / (recent + _CHANGE_FAILURE_PRIOR)
    failure_penalty = smoothed_rate * Decimal("100")

    return round2(clamp_d(Decimal("100") - churn_penalty - failure_penalty))
