"""Node-placement orchestration: rank the individual hosts *inside* a
shortlisted cluster.

This is the second stage of a two-stage placement. Stage one
(:mod:`app.services.placement`) answers "which clusters can host this
workload, best first". This module answers, for one of those clusters, "and
which hosts inside it, best first" - so a recommendation reads
``nyc-03 -> nyc-03-node-04`` rather than stopping at the cluster boundary.

Three properties are deliberate:

* **The effective requirement is not recomputed here.** It is passed in from
  the cluster projection, already grown and safety-margined. Applying growth
  twice would silently inflate every node projection relative to its own
  cluster's.
* **What each host is asked to absorb depends on the platform.** On a
  clustered platform (Kubernetes, OpenShift, VMware, Hyper-V) a workload is
  scheduled *across* the cluster, so each host is evaluated against the
  workload's per-host share. On BareMetal there is no scheduler to spread it,
  so a single host must absorb the whole requirement. Evaluating a 16-core
  workload against one 6-core host on Kubernetes would reject every host in
  the estate and return an empty shortlist - technically true, operationally
  meaningless.
* **Cost is normalized within the cluster, not across the estate.** A node's
  cost sub-score answers "is this host cheap *for this cluster*" - comparing a
  node in a Tier-1 cluster against a node in a Tier-3 one would just re-derive
  the cluster ordering that stage one already produced.

Nothing in this module is an LLM call, and no value here is ever produced by
one - same trust boundary as the rest of ``app/services``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

#: Upper bound on concurrent host drills - see attach_top_nodes.
_DRILL_WORKERS = 6

from dataclasses import asdict
from decimal import Decimal

from app.config import get_settings
from app.models.entities import ClusterNode, InfrastructureCluster
from app.models.requirements import HostingRequirement
from app.models.scoring import CandidateScore, NodeCandidateScore, NodeSubScores
from app.repositories import cluster_repository, incident_repository, node_repository
from app.rules import node_eligibility
from app.scoring import subscores
from app.scoring.engine import compute_node_overall_score, rank_node_candidates
from app.scoring.subscores import round2
from app.services import capacity


def estimate_node_monthly_cost(
    node: ClusterNode, *, cpu_cores: Decimal, memory_gb: Decimal
) -> Decimal:
    """Same share-of-host model as :func:`placement.estimate_monthly_cost`, one
    level down: the larger of the CPU or memory share of the host, applied to
    the host's internal chargeback rate.

    ``cpu_cores``/``memory_gb`` are the *per-host* portion of the raw (ungrown)
    requirement - on a spreading platform, a host is only charged for the slice
    it actually carries.
    """
    cpu_share = cpu_cores / node.CpuCores if node.CpuCores else Decimal("0")
    mem_share = memory_gb / node.MemoryGb if node.MemoryGb else Decimal("0")
    return round2(node.MonthlyCost * max(cpu_share, mem_share))


def _staleness_days(node: ClusterNode, newest_last_seen) -> int:
    delta = newest_last_seen - node.LastSeenAt
    return max(0, delta.days)


#: Platforms whose scheduler spreads a workload across the cluster's hosts.
#: BareMetal is the only platform in the schema's CK constraint that does not.
_SPREADING_PLATFORMS = frozenset({"Kubernetes", "OpenShift", "VMware", "Hyper-V"})


def per_host_requirement(
    cluster: InfrastructureCluster,
    active_node_count: int,
    *,
    required_cpu_cores_effective: Decimal,
    required_memory_gb_effective: Decimal,
    required_storage_gb_effective: Decimal,
) -> tuple[Decimal, Decimal, Decimal, str, int]:
    """How much of the workload a single host in ``cluster`` has to absorb.

    Returns ``(cpu, memory, storage, model, denominator)`` where ``model`` is
    ``"share"`` (spread across the cluster's active hosts) or ``"whole"`` (one
    host takes all of it). The denominator is reported so a reviewer can see
    exactly what each host was measured against - the share is a plain even
    split, not a bin-packing simulation, and it should be read as "this host's
    fair portion", not as a guaranteed scheduler placement.
    """
    if cluster.Platform not in _SPREADING_PLATFORMS or active_node_count <= 1:
        return (
            required_cpu_cores_effective,
            required_memory_gb_effective,
            required_storage_gb_effective,
            "whole",
            1,
        )
    n = Decimal(active_node_count)
    return (
        required_cpu_cores_effective / n,
        required_memory_gb_effective / n,
        required_storage_gb_effective / n,
        "share",
        active_node_count,
    )


def evaluate_node(
    requirement: HostingRequirement,
    cluster: InfrastructureCluster,
    node: ClusterNode,
    *,
    required_cpu_cores_effective: Decimal,
    required_memory_gb_effective: Decimal,
    required_storage_gb_effective: Decimal,
    newest_last_seen,
    placement_model: str = "whole",
    share_denominator: int = 1,
) -> NodeCandidateScore:
    snapshot = capacity.compute_node_capacity(node, cluster)
    projected = capacity.compute_node_projected_utilization(
        snapshot,
        required_cpu_cores_effective=required_cpu_cores_effective,
        required_memory_gb_effective=required_memory_gb_effective,
        required_storage_gb_effective=required_storage_gb_effective,
    )
    ctx = node_eligibility.NodeEligibilityContext(
        node=node, snapshot=snapshot, projected=projected,
        staleness_days=_staleness_days(node, newest_last_seen),
    )
    rule_results = node_eligibility.evaluate_all(ctx)
    eligible = node_eligibility.is_eligible(rule_results)

    return NodeCandidateScore(
        node_id=node.NodeId,
        host_name=node.HostName,
        cluster_id=cluster.ClusterId,
        cluster_code=cluster.ClusterCode,
        lifecycle_status=node.LifecycleStatus,
        eligibility_status="Eligible" if eligible else "Rejected",
        rule_results=[asdict(r) for r in rule_results],
        snapshot=snapshot,
        projected=projected,
        estimated_monthly_cost=estimate_node_monthly_cost(
            node,
            cpu_cores=requirement.cpu_cores / Decimal(share_denominator),
            memory_gb=requirement.memory_gb / Decimal(share_denominator),
        ),
        evidence={
            "staleness_days": ctx.staleness_days,
            "measurement_sample_count": snapshot.measurement_sample_count,
            "placement_model": placement_model,
            "share_denominator": share_denominator,
        },
    )


def score_node(
    candidate: NodeCandidateScore, node: ClusterNode, cost_score: Decimal
) -> NodeCandidateScore:
    settings = get_settings()
    cap_score = subscores.node_capacity_subscore(candidate.projected)

    incidents = incident_repository.get_recent_for_node(
        node.NodeId, settings.policy.node_incident_window_days
    )
    reliability = subscores.historical_performance_subscore(incidents)

    open_severe = incident_repository.get_open_severe_for_node(node.NodeId)
    risk = subscores.node_operational_risk_score(
        lifecycle_status=node.LifecycleStatus,
        open_severe_incident_count=len(open_severe),
        staleness_days=candidate.evidence.get("staleness_days", 0),
        stale_after_days=settings.policy.node_stale_after_days,
        has_measurements=candidate.snapshot.has_measurements if candidate.snapshot else False,
    )

    sub = NodeSubScores(capacity=cap_score, cost=cost_score, reliability=reliability, risk=risk)
    candidate.subscores = sub
    candidate.overall_score = compute_node_overall_score(sub)
    candidate.evidence = {
        **candidate.evidence,
        "recent_incidents": len(incidents),
        "open_severe_incidents": len(open_severe),
    }
    return candidate


def rank_nodes_for_cluster(
    requirement: HostingRequirement,
    cluster: InfrastructureCluster,
    *,
    required_cpu_cores_effective: Decimal,
    required_memory_gb_effective: Decimal,
    required_storage_gb_effective: Decimal,
    top_n: int | None = None,
) -> list[NodeCandidateScore]:
    """Every host in ``cluster``, ranked best-first for this workload.

    Non-Active hosts are evaluated and returned as ``Rejected`` rather than
    filtered out up front, so "why not that host" has an answer. ``top_n``
    caps only the *eligible* hosts returned; rejections are never truncated -
    the same contract :func:`placement.find_and_score_candidates` uses.

    Each host is measured against its per-host portion of the requirement (see
    :func:`per_host_requirement`), and the portion used is recorded on every
    candidate's ``evidence`` as ``placement_model``/``share_denominator``.
    """
    nodes = node_repository.get_by_cluster(cluster.ClusterId)
    if not nodes:
        return []

    newest_last_seen = max(n.LastSeenAt for n in nodes)
    active_node_count = sum(1 for n in nodes if n.LifecycleStatus == "Active")

    host_cpu, host_mem, host_storage, model, denominator = per_host_requirement(
        cluster, active_node_count,
        required_cpu_cores_effective=required_cpu_cores_effective,
        required_memory_gb_effective=required_memory_gb_effective,
        required_storage_gb_effective=required_storage_gb_effective,
    )

    evaluated: list[tuple[NodeCandidateScore, ClusterNode]] = []
    for node in nodes:
        candidate = evaluate_node(
            requirement, cluster, node,
            required_cpu_cores_effective=host_cpu,
            required_memory_gb_effective=host_mem,
            required_storage_gb_effective=host_storage,
            newest_last_seen=newest_last_seen,
            placement_model=model,
            share_denominator=denominator,
        )
        evaluated.append((candidate, node))

    eligible_pairs = [(c, n) for c, n in evaluated if c.eligibility_status == "Eligible"]
    costs = {c.node_id: c.estimated_monthly_cost for c, _ in eligible_pairs}
    cost_scores = subscores.cost_efficiency_scores(costs)

    for candidate, node in eligible_pairs:
        score_node(candidate, node, cost_scores.get(node.NodeId, Decimal("0")))

    ranked = rank_node_candidates([c for c, _ in evaluated])

    if top_n is not None and top_n > 0:
        eligible = [c for c in ranked if c.eligibility_status == "Eligible"][:top_n]
        rejected = [c for c in ranked if c.eligibility_status != "Eligible"]
        return eligible + rejected

    return ranked


def rank_nodes_for_candidate(
    requirement: HostingRequirement, candidate: CandidateScore, *, top_n: int | None = None
) -> list[NodeCandidateScore]:
    """Convenience wrapper over :func:`rank_nodes_for_cluster` that reads the
    already-computed effective requirement off a scored cluster candidate.
    Returns ``[]`` when the candidate was never projected (a cluster rejected
    before capacity was computed has no effective requirement to apply).
    """
    if candidate.projected is None:
        return []
    cluster = cluster_repository.get_by_id(candidate.cluster_id)
    if cluster is None:
        return []
    return rank_nodes_for_cluster(
        requirement, cluster,
        required_cpu_cores_effective=candidate.projected.required_cpu_cores_effective,
        required_memory_gb_effective=candidate.projected.required_memory_gb_effective,
        required_storage_gb_effective=candidate.projected.required_storage_gb_effective,
        top_n=top_n,
    )


def attach_top_nodes(
    requirement: HostingRequirement,
    candidates: list[CandidateScore],
    *,
    top_clusters: int | None = None,
    top_nodes_per_cluster: int | None = None,
) -> list[CandidateScore]:
    """Populates ``CandidateScore.top_nodes`` for the leading eligible clusters,
    in place, and returns the same list.

    Bounded on purpose: node ranking costs a handful of queries per host, and
    nobody reviews host #47 of cluster #9. Only the clusters that will actually
    be proposed for review get drilled into.
    """
    settings = get_settings()
    n_clusters = top_clusters if top_clusters is not None else settings.policy.top_clusters
    n_nodes = (
        top_nodes_per_cluster if top_nodes_per_cluster is not None
        else settings.policy.top_nodes_per_cluster
    )

    #  DRILLED IN PARALLEL, AND THE REASON IS THE REVIEW DECK.
    #
    #  Each cluster's drill is several independent queries against a different
    #  cluster's hosts - no shared state, nothing to order - and the loop that
    #  did them one after another was the whole cost of widening the review
    #  panel beyond three options. Measured on production, Tier-3 4c/16GB, 11
    #  eligible clusters:
    #
    #      sequential, 3 clusters    2.06s   (what this used to do)
    #      sequential, 11 clusters  11.00s
    #      parallel,   11 clusters   3.42s   (4 workers)
    #      parallel,   11 clusters   2.98s   (6 workers)
    #
    #  Identical output either way - 33 hosts, no errors. So the engineer gets
    #  a deck of twelve to page through for about a second more than the three
    #  they used to get.
    #
    #  Bounded at 6 workers rather than one per cluster: these are database
    #  round-trips on a shared pool, and the estate can rank a hundred
    #  eligible clusters on a small request. Past six the curve is already
    #  flat (2.98s vs 3.42s), so more workers would buy nothing and risk
    #  starving the rest of the request.
    #
    #  Order is preserved by assigning into the candidate objects rather than
    #  collecting results - the ranking IS the claim about which cluster is
    #  the recommendation, and a thread pool must not reorder it.
    to_drill = [c for c in candidates if c.eligibility_status == "Eligible"][:n_clusters]
    if not to_drill:
        return candidates

    def drill(candidate) -> None:
        candidate.top_nodes = [
            n for n in rank_nodes_for_candidate(requirement, candidate, top_n=n_nodes)
            if n.eligibility_status == "Eligible"
        ][:n_nodes]

    if len(to_drill) == 1:
        drill(to_drill[0])
        return candidates

    with ThreadPoolExecutor(max_workers=min(_DRILL_WORKERS, len(to_drill))) as pool:
        #  list() so an exception inside a worker is raised here rather than
        #  discarded - a cluster that silently ends up with no hosts looks
        #  exactly like a cluster that genuinely has none.
        list(pool.map(drill, to_drill))
    return candidates
