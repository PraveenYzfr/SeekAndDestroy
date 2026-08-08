"""NODE-001 .. NODE-004 - deterministic hard eligibility rules for a single
node inside an already-eligible cluster.

These do not replace RULE-001..RULE-010; they run *after* them. By the time a
node is evaluated, its cluster has already satisfied every cluster-level rule
(environment, platform, availability tier, classification, location,
dependency locality, lifecycle). Re-checking those per node would return the
same answer for every sibling - a node inherits them all. What is left is
strictly node-local: is this host alive, is it reporting, and does the
workload actually fit on it.

Like :mod:`app.rules.eligibility`, every function takes plain data - no
database, no LLM - and all rules are evaluated even after the first failure so
a rejected host can explain every reason it was rejected.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings
from app.models.capacity import NodeCapacitySnapshot, ProjectedUtilization
from app.models.entities import ClusterNode
from app.rules.eligibility import RuleResult


@dataclass
class NodeEligibilityContext:
    node: ClusterNode
    snapshot: NodeCapacitySnapshot
    projected: ProjectedUtilization
    #: Days between this node's LastSeenAt and the freshest LastSeenAt in the
    #: same cluster. Relative, not wall-clock: seed and historical estates are
    #: not guaranteed to reach the current instant, and the same reasoning
    #: already governs utilization windows (see utilization_repository).
    staleness_days: int


def node_001_lifecycle(ctx: NodeEligibilityContext) -> RuleResult:
    status = ctx.node.LifecycleStatus
    # Stricter than the cluster rule on purpose: a cluster may legitimately be
    # 'Planned' and still be a valid placement target, but a *host* has to be
    # live right now to receive a workload.
    ok = status == "Active"
    return RuleResult(
        "NODE-001", "Node lifecycle", ok,
        (
            f"Host {ctx.node.HostName} is Active."
            if ok else
            f"Host {ctx.node.HostName} is '{status}'; only Active hosts may receive new placements."
        ),
        {"host_name": ctx.node.HostName, "lifecycle_status": status},
    )


def node_002_capacity(ctx: NodeEligibilityContext) -> RuleResult:
    s = ctx.snapshot
    ok = (
        s.available_cpu_cores >= ctx.projected.required_cpu_cores_effective
        and s.available_memory_gb >= ctx.projected.required_memory_gb_effective
        and s.available_storage_gb >= ctx.projected.required_storage_gb_effective
    )
    return RuleResult(
        "NODE-002", "Node absolute capacity", ok,
        (
            f"Host {s.host_name} has enough free CPU, memory and storage for its portion "
            f"of the effective requirement."
            if ok else
            f"Host {s.host_name} cannot absorb its portion of the effective requirement "
            f"(free: {s.available_cpu_cores} cores / {s.available_memory_gb} GB RAM / "
            f"{s.available_storage_gb} GB disk)."
        ),
        {
            "available_cpu_cores": str(s.available_cpu_cores),
            "available_memory_gb": str(s.available_memory_gb),
            "available_storage_gb": str(s.available_storage_gb),
            "required_cpu_cores_effective": str(ctx.projected.required_cpu_cores_effective),
            "required_memory_gb_effective": str(ctx.projected.required_memory_gb_effective),
            "required_storage_gb_effective": str(ctx.projected.required_storage_gb_effective),
        },
    )


def node_003_headroom(ctx: NodeEligibilityContext) -> RuleResult:
    p = ctx.projected
    ok = p.fits_all
    return RuleResult(
        "NODE-003", "Node capacity headroom", ok,
        (
            f"Projected utilization on {ctx.snapshot.host_name} stays under every threshold "
            f"({p.projected_headroom_percent}% headroom remaining)."
            if ok else
            f"Placing this workload on {ctx.snapshot.host_name} would breach a utilization "
            f"threshold (CPU {p.projected_cpu_utilization_percent}%, "
            f"memory {p.projected_memory_utilization_percent}%, "
            f"storage {p.projected_storage_utilization_percent}%)."
        ),
        {
            "fits_cpu": p.fits_cpu, "fits_memory": p.fits_memory, "fits_storage": p.fits_storage,
            "projected_headroom_percent": str(p.projected_headroom_percent),
        },
    )


def node_004_reporting(ctx: NodeEligibilityContext) -> RuleResult:
    limit = get_settings().policy.node_stale_after_days
    ok = ctx.staleness_days <= limit
    return RuleResult(
        "NODE-004", "Node is reporting", ok,
        (
            f"Host {ctx.node.HostName} last reported {ctx.staleness_days} day(s) behind the "
            f"freshest host in its cluster."
            if ok else
            f"Host {ctx.node.HostName} last reported {ctx.staleness_days} day(s) behind its "
            f"cluster (limit {limit}); a host nobody has heard from cannot be recommended."
        ),
        {"staleness_days": ctx.staleness_days, "stale_after_days": limit,
         "has_measurements": ctx.snapshot.has_measurements},
    )


def evaluate_all(ctx: NodeEligibilityContext) -> list[RuleResult]:
    return [
        node_001_lifecycle(ctx),
        node_002_capacity(ctx),
        node_003_headroom(ctx),
        node_004_reporting(ctx),
    ]


def is_eligible(results: list[RuleResult]) -> bool:
    return all(r.passed for r in results)


def failed_rules(results: list[RuleResult]) -> list[RuleResult]:
    return [r for r in results if not r.passed]
