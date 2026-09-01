"""RULE-001 .. RULE-010 - deterministic hard eligibility rules.

Every function takes plain data (never touches the database or the LLM) and
returns a :class:`RuleResult`. :func:`evaluate_all` always runs all ten rules
- even after the first failure - so a rejected candidate's explanation can
list every reason, not just the first one encountered.

Design notes on the two rules that are not a literal 1:1 reading of the
one-line spec text (see docs/business-rules.md for the full rationale):

- RULE-006 (location): the schema only carries a *preferred* location, not a
  separate "mandatory" flag. We treat preferred-location as a hard constraint
  only when the workload's DataClassification is Restricted (a data-residency
  reading of "mandatory ... constraints"); every other mismatch is a soft
  compatibility-score penalty, never a hard rejection.
- RULE-010 (resiliency): uses the candidate's *actual* active node count
  (from ClusterNode, not the possibly-stale InfrastructureCluster.NodeCount
  column) so a cluster that advertises Tier-1 but cannot structurally back it
  (too few live nodes) is correctly rejected for Critical/High workloads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.config import get_settings
from app.models.capacity import ProjectedUtilization
from app.models.entities import InfrastructureCluster
from app.models.enums import (
    INELIGIBLE_LIFECYCLE_STATES,
    availability_satisfies,
    classification_permits,
    environment_compatible,
    os_is_compatible,
    platform_compatible,
)
from app.models.requirements import HostingRequirement


@dataclass
class RuleResult:
    rule_id: str
    name: str
    passed: bool
    reason: str
    evidence: dict = field(default_factory=dict)


@dataclass
class EligibilityContext:
    requirement: HostingRequirement
    cluster: InfrastructureCluster
    projected: ProjectedUtilization
    active_node_count: int
    #: Change record for this cluster, from itsm_repository.change_risk_for_clusters:
    #: upcoming_changes, recent_changes, recent_failures, failure_rate, freeze_until.
    #: Defaulted to None so every existing construction of this context keeps
    #: working and RULE-011 passes when the data is simply absent - an estate
    #: with no change management must not become entirely ineligible.
    change_risk: dict | None = None


def rule_001_environment(ctx: EligibilityContext) -> RuleResult:
    ok = environment_compatible(ctx.requirement.environment, ctx.cluster.Environment)
    return RuleResult(
        "RULE-001", "Environment compatibility", ok,
        (
            f"Application environment '{ctx.requirement.environment}' is compatible with "
            f"cluster environment '{ctx.cluster.Environment}'."
            if ok else
            f"Production/{ctx.requirement.environment} workloads may not be placed on "
            f"'{ctx.cluster.Environment}' infrastructure."
        ),
        {"required": ctx.requirement.environment, "candidate": ctx.cluster.Environment},
    )


def rule_002_platform(ctx: EligibilityContext) -> RuleResult:
    platform_ok = platform_compatible(ctx.requirement.platform, ctx.cluster.Platform)
    os_ok = os_is_compatible(ctx.requirement.os_requirement, ctx.cluster.OperatingSystem)
    ok = platform_ok and os_ok
    reasons = []
    if not platform_ok:
        reasons.append(f"platform '{ctx.requirement.platform}' is not supported on cluster platform '{ctx.cluster.Platform}'")
    if not os_ok:
        reasons.append(f"OS requirement '{ctx.requirement.os_requirement}' is not satisfied by '{ctx.cluster.OperatingSystem}'")
    reason = "Platform and OS are compatible." if ok else "; ".join(reasons)
    return RuleResult(
        "RULE-002", "Platform compatibility", ok, reason,
        {
            "required_platform": ctx.requirement.platform, "candidate_platform": ctx.cluster.Platform,
            "required_os": ctx.requirement.os_requirement, "candidate_os": ctx.cluster.OperatingSystem,
        },
    )


def rule_003_capacity(ctx: EligibilityContext, snapshot) -> RuleResult:
    p = ctx.projected
    cpu_ok = snapshot.available_cpu_cores >= p.required_cpu_cores_effective
    mem_ok = snapshot.available_memory_gb >= p.required_memory_gb_effective
    storage_ok = snapshot.available_storage_gb >= p.required_storage_gb_effective
    ok = cpu_ok and mem_ok and storage_ok
    shortfalls = []
    if not cpu_ok:
        shortfalls.append(f"CPU short by {p.required_cpu_cores_effective - snapshot.available_cpu_cores:.2f} cores")
    if not mem_ok:
        shortfalls.append(f"memory short by {p.required_memory_gb_effective - snapshot.available_memory_gb:.2f} GB")
    if not storage_ok:
        shortfalls.append(f"storage short by {p.required_storage_gb_effective - snapshot.available_storage_gb:.2f} GB")
    reason = "Sufficient CPU, memory and storage are available." if ok else "; ".join(shortfalls)
    return RuleResult(
        "RULE-003", "Capacity requirement", ok, reason,
        {
            "available_cpu": str(snapshot.available_cpu_cores), "required_cpu": str(p.required_cpu_cores_effective),
            "available_memory": str(snapshot.available_memory_gb), "required_memory": str(p.required_memory_gb_effective),
            "available_storage": str(snapshot.available_storage_gb), "required_storage": str(p.required_storage_gb_effective),
        },
    )


def rule_004_availability(ctx: EligibilityContext) -> RuleResult:
    ok = availability_satisfies(ctx.cluster.AvailabilityTier, ctx.requirement.availability_tier)
    return RuleResult(
        "RULE-004", "Availability requirement", ok,
        (
            f"Cluster tier '{ctx.cluster.AvailabilityTier}' satisfies required tier "
            f"'{ctx.requirement.availability_tier}'."
            if ok else
            f"Cluster tier '{ctx.cluster.AvailabilityTier}' does not meet required tier "
            f"'{ctx.requirement.availability_tier}'."
        ),
        {"required_tier": ctx.requirement.availability_tier, "candidate_tier": ctx.cluster.AvailabilityTier},
    )


def rule_005_classification(ctx: EligibilityContext) -> RuleResult:
    ok = classification_permits(ctx.cluster.ComplianceClassification, ctx.requirement.data_classification)
    return RuleResult(
        "RULE-005", "Data classification", ok,
        (
            f"Cluster compliance classification '{ctx.cluster.ComplianceClassification}' permits "
            f"'{ctx.requirement.data_classification}' data."
            if ok else
            f"Cluster is only certified for '{ctx.cluster.ComplianceClassification}' data; "
            f"workload requires '{ctx.requirement.data_classification}'."
        ),
        {"required": ctx.requirement.data_classification, "candidate": ctx.cluster.ComplianceClassification},
    )


def rule_006_location(ctx: EligibilityContext) -> RuleResult:
    preferred = ctx.requirement.preferred_location
    if not preferred:
        return RuleResult("RULE-006", "Location constraint", True, "No location constraint specified.", {})
    same_dc = ctx.cluster.DataCenter == preferred
    if same_dc:
        return RuleResult(
            "RULE-006", "Location constraint", True,
            f"Cluster is in the preferred location '{preferred}'.",
            {"preferred_location": preferred, "candidate_location": ctx.cluster.DataCenter},
        )
    is_mandatory = ctx.requirement.data_classification == "Restricted"
    if is_mandatory:
        return RuleResult(
            "RULE-006", "Location constraint", False,
            (
                f"'{ctx.requirement.data_classification}' data must stay in its designated location "
                f"'{preferred}'; candidate is in '{ctx.cluster.DataCenter}'."
            ),
            {"preferred_location": preferred, "candidate_location": ctx.cluster.DataCenter, "mandatory": True},
        )
    return RuleResult(
        "RULE-006", "Location constraint", True,
        f"Candidate is outside the preferred location '{preferred}' (soft preference, scored not rejected).",
        {"preferred_location": preferred, "candidate_location": ctx.cluster.DataCenter, "mandatory": False},
    )


def rule_007_lifecycle(ctx: EligibilityContext) -> RuleResult:
    ok = ctx.cluster.LifecycleStatus not in INELIGIBLE_LIFECYCLE_STATES
    return RuleResult(
        "RULE-007", "Lifecycle status", ok,
        (
            f"Cluster lifecycle status '{ctx.cluster.LifecycleStatus}' is eligible."
            if ok else
            f"Cluster lifecycle status '{ctx.cluster.LifecycleStatus}' is not eligible for new placements."
        ),
        {"lifecycle_status": ctx.cluster.LifecycleStatus},
    )


def rule_008_dependency(ctx: EligibilityContext) -> RuleResult:
    violations = []
    for dep in ctx.requirement.dependency_checks:
        if not (dep.is_critical and dep.latency_sensitivity == "High"):
            continue
        if dep.target_region is None:
            continue
        if dep.target_region != ctx.cluster.Region:
            violations.append(
                f"critical high-latency-sensitivity dependency on {dep.target_description} "
                f"is in region '{dep.target_region}', candidate is in '{ctx.cluster.Region}'"
            )
    ok = not violations
    reason = "No unacceptable cross-region critical dependencies." if ok else "; ".join(violations)
    return RuleResult(
        "RULE-008", "Dependency compatibility", ok, reason,
        {"candidate_region": ctx.cluster.Region, "violation_count": len(violations)},
    )


def rule_009_headroom(ctx: EligibilityContext) -> RuleResult:
    p = ctx.projected
    ok = p.fits_all
    settings = get_settings()
    breaches = []
    if not p.fits_cpu:
        breaches.append(f"projected CPU {p.projected_cpu_utilization_percent}% >= threshold {settings.policy.cpu_threshold_percent}%")
    if not p.fits_memory:
        breaches.append(f"projected memory {p.projected_memory_utilization_percent}% >= threshold {settings.policy.memory_threshold_percent}%")
    if not p.fits_storage:
        breaches.append(f"projected storage {p.projected_storage_utilization_percent}% >= threshold {settings.policy.storage_threshold_percent}%")
    reason = "Projected utilization stays within configured headroom thresholds." if ok else "; ".join(breaches)
    return RuleResult(
        "RULE-009", "Capacity headroom", ok, reason,
        {
            "projected_cpu_percent": str(p.projected_cpu_utilization_percent),
            "projected_memory_percent": str(p.projected_memory_utilization_percent),
            "projected_storage_percent": str(p.projected_storage_utilization_percent),
            "headroom_percent": str(p.projected_headroom_percent),
        },
    )


def rule_010_resiliency(ctx: EligibilityContext) -> RuleResult:
    settings = get_settings()
    criticality = ctx.requirement.criticality
    tier = ctx.cluster.AvailabilityTier
    nodes = ctx.active_node_count

    if criticality == "Critical":
        required_tier_ok = tier == "Tier-1"
        required_nodes = settings.policy.min_nodes_tier1
        nodes_ok = nodes >= required_nodes
        ok = required_tier_ok and nodes_ok
        if not ok:
            reason = (
                f"Critical workloads require Tier-1 infrastructure with >= {required_nodes} active nodes "
                f"(candidate: tier={tier}, active_nodes={nodes})."
            )
        else:
            reason = f"Candidate offers Tier-1 with {nodes} active nodes, meeting Critical resiliency requirements."
    elif criticality == "High":
        required_tier_ok = tier in ("Tier-1", "Tier-2")
        required_nodes = settings.policy.min_nodes_tier2
        nodes_ok = nodes >= required_nodes
        ok = required_tier_ok and nodes_ok
        if not ok:
            reason = (
                f"High-criticality workloads require Tier-1/Tier-2 infrastructure with >= {required_nodes} "
                f"active nodes (candidate: tier={tier}, active_nodes={nodes})."
            )
        else:
            reason = f"Candidate offers {tier} with {nodes} active nodes, meeting High resiliency requirements."
    else:
        ok = True
        reason = f"'{criticality}' criticality has no minimum node-count requirement."

    return RuleResult(
        "RULE-010", "Resiliency", ok, reason,
        {"criticality": criticality, "candidate_tier": tier, "active_node_count": nodes},
    )


def rule_011_change_freeze(ctx: EligibilityContext) -> RuleResult:
    """A cluster under an active change freeze cannot take new work.

    Hard, not weighted. A freeze is a decision somebody made about this cluster
    for a stated period - a release window, an audit, a migration - and it is not
    the kind of thing a high capacity score should be able to outvote. That is
    the same reasoning as RULE-007 lifecycle: some facts disqualify rather than
    discount.

    Distinct from the change-risk sub-score, which is soft and reads the same
    data. Scheduled churn and a poor failure rate make a cluster a worse choice;
    an active freeze makes it not a choice at all.

    Passes when there is no change record. An estate that does not do change
    management should not find every cluster ineligible - absence of evidence is
    not a freeze.
    """
    freeze_until = (ctx.change_risk or {}).get("freeze_until")
    if not freeze_until:
        return RuleResult(
            "RULE-011", "Change freeze", True, "No active change freeze on this cluster.", {}
        )
    return RuleResult(
        "RULE-011", "Change freeze", False,
        f"Cluster is under a change freeze until {freeze_until}.",
        {"freeze_until": str(freeze_until)},
    )


def evaluate_all(ctx: EligibilityContext, snapshot) -> list[RuleResult]:
    return [
        rule_001_environment(ctx),
        rule_002_platform(ctx),
        rule_003_capacity(ctx, snapshot),
        rule_004_availability(ctx),
        rule_005_classification(ctx),
        rule_006_location(ctx),
        rule_007_lifecycle(ctx),
        rule_008_dependency(ctx),
        rule_009_headroom(ctx),
        rule_010_resiliency(ctx),
        rule_011_change_freeze(ctx),
    ]


def is_eligible(results: list[RuleResult]) -> bool:
    return all(r.passed for r in results)


def failed_rules(results: list[RuleResult]) -> list[RuleResult]:
    return [r for r in results if not r.passed]
