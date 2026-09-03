"""RIGHTSIZE-001..006 - cluster and application right-sizing.

Every number here is deterministic arithmetic over repository data; nothing in
this module ever calls the LLM. See docs/business-rules.md for the formulas.
"""

from __future__ import annotations

import math
from decimal import Decimal, InvalidOperation

from app.config import get_settings
from app.models.entities import CmdbApplication, InfrastructureCluster
from app.models.rightsizing import ApplicationRightSizingResult, ClusterRightSizingResult
from app.repositories import hosting_repository, node_repository, usage_repository
from app.scoring.subscores import round2
from app.services import capacity


def classify_cluster(snapshot, *, overprov_cpu: Decimal, overprov_mem: Decimal,
                      underprov_cpu: Decimal, underprov_mem: Decimal) -> str:
    cpu = snapshot.current_cpu_utilization_percent
    mem = snapshot.current_memory_utilization_percent
    if cpu >= underprov_cpu or mem >= underprov_mem:
        return "Underprovisioned"
    if cpu < overprov_cpu and mem < overprov_mem:
        return "Overprovisioned"
    return "Healthy"


def _min_nodes_for_headroom(
    consumed: Decimal, per_node_capacity: Decimal, threshold_percent: Decimal
) -> int:
    """Smallest node count N such that consumed / (per_node_capacity * N) < threshold."""
    if per_node_capacity <= 0:
        return 1
    if consumed <= 0:
        return 1
    required_effective = consumed / (threshold_percent / Decimal("100"))
    n = math.ceil(required_effective / per_node_capacity)
    return max(1, n)


def analyze_cluster_right_sizing(cluster: InfrastructureCluster) -> ClusterRightSizingResult:
    settings = get_settings()
    p = settings.policy
    snapshot = capacity.compute_cluster_capacity(cluster)
    active_nodes = node_repository.count_active_by_cluster(cluster.ClusterId)
    active_nodes = max(active_nodes, 1)

    classification = classify_cluster(
        snapshot,
        overprov_cpu=capacity.d(p.overprovision_cpu_percent),
        overprov_mem=capacity.d(p.overprovision_memory_percent),
        underprov_cpu=capacity.d(p.underprovision_cpu_percent),
        underprov_mem=capacity.d(p.underprovision_memory_percent),
    )

    per_node_cpu = cluster.TotalCpuCores / active_nodes
    per_node_mem = cluster.TotalMemoryGb / active_nodes

    risks: list[str] = []
    node_delta = 0
    recommended_nodes = active_nodes
    rationale = ""

    if classification == "Overprovisioned":
        needed_cpu_nodes = _min_nodes_for_headroom(
            snapshot.consumed_cpu_cores, per_node_cpu * (Decimal("1") - cluster.ReservedCpuPercent / 100),
            capacity.d(p.cpu_threshold_percent),
        )
        needed_mem_nodes = _min_nodes_for_headroom(
            snapshot.consumed_memory_gb, per_node_mem * (Decimal("1") - cluster.ReservedMemoryPercent / 100),
            capacity.d(p.memory_threshold_percent),
        )
        min_structural = p.node_failure_tolerance + 1
        candidate_nodes = max(needed_cpu_nodes, needed_mem_nodes, min_structural)
        # N-1 failure tolerance: with one fewer node, load must still stay
        # under a 95% emergency ceiling.
        while candidate_nodes < active_nodes:
            remaining = candidate_nodes - 1
            if remaining < 1:
                break
            cpu_after_failure = snapshot.consumed_cpu_cores / (per_node_cpu * remaining) * 100
            mem_after_failure = snapshot.consumed_memory_gb / (per_node_mem * remaining) * 100
            if cpu_after_failure < Decimal("95") and mem_after_failure < Decimal("95"):
                break
            candidate_nodes += 1
        recommended_nodes = min(candidate_nodes, active_nodes)
        node_delta = recommended_nodes - active_nodes
        rationale = (
            f"Sustained low utilization (CPU {snapshot.current_cpu_utilization_percent}%, "
            f"memory {snapshot.current_memory_utilization_percent}%) supports reducing from "
            f"{active_nodes} to {recommended_nodes} active nodes while preserving "
            f"N-{p.node_failure_tolerance} failure tolerance and headroom thresholds."
        )
    elif classification == "Underprovisioned":
        target_cpu_nodes = _min_nodes_for_headroom(
            snapshot.consumed_cpu_cores, per_node_cpu * (Decimal("1") - cluster.ReservedCpuPercent / 100),
            capacity.d(p.cpu_threshold_percent),
        )
        target_mem_nodes = _min_nodes_for_headroom(
            snapshot.consumed_memory_gb, per_node_mem * (Decimal("1") - cluster.ReservedMemoryPercent / 100),
            capacity.d(p.memory_threshold_percent),
        )
        recommended_nodes = max(active_nodes, target_cpu_nodes, target_mem_nodes)
        node_delta = recommended_nodes - active_nodes
        if node_delta > 0:
            risks.append(
                f"Cluster is trending toward its capacity ceiling; add {node_delta} node(s) "
                f"to stay under configured thresholds."
            )
        rationale = (
            f"Utilization is approaching capacity limits (CPU {snapshot.current_cpu_utilization_percent}%, "
            f"memory {snapshot.current_memory_utilization_percent}%); recommend expanding to "
            f"{recommended_nodes} active nodes."
        )
    else:
        rationale = "Utilization is within healthy operating bounds; no node-count change recommended."

    if cluster.LifecycleStatus == "Deprecated":
        risks.append("Cluster lifecycle status is Deprecated.")

    #  Signed, both directions, no floor. The money version of these two lines
    #  computed a saving only when node_delta < 0 and returned 0.00 otherwise,
    #  so an expansion reported no financial effect at all. Capacity has no
    #  such asymmetry: removing two nodes frees exactly what adding two would
    #  consume, and the sign carries which it is.
    cpu_cores_delta = round2(per_node_cpu * Decimal(node_delta))
    memory_gb_delta = round2(per_node_mem * Decimal(node_delta))

    return ClusterRightSizingResult(
        cluster_id=cluster.ClusterId,
        cluster_code=cluster.ClusterCode,
        classification=classification,
        snapshot=snapshot,
        current_node_count=active_nodes,
        recommended_node_count=recommended_nodes,
        node_delta=node_delta,
        cpu_cores_delta=cpu_cores_delta,
        memory_gb_delta=memory_gb_delta,
        risks=risks,
        rationale=rationale,
    )


def rank_right_sizing(results: list[dict]) -> list[dict]:
    """A total order over right-sizing findings, worth-acting-on first.

    WHY THIS EXISTS. Hosting and Capacity go through
    app.scoring.engine.rank_candidates, which promises "score desc, cost asc,
    cluster_code asc. No ties, ever." Right-sizing had no equivalent, and two
    things went wrong for want of one.

    Narration picked its subjects with a bare slice - the first five
    non-Healthy rows in whatever order cluster_repository.list_all returned -
    so the clusters an engineer read about were an artefact of SQL. And the
    final report reads top_candidates, which only the Hosting path fills, so
    "which 3 clusters are the best right-sizing candidates?" met an empty list
    and the reporting model correctly refused to invent one. The refusal was
    the guard working; the missing ranking was the defect.

    RANKED ON RECLAIMED CAPACITY, NOT MONEY. The first version of this
    function sorted by estimated_monthly_savings, which no longer exists - see
    the note in app/models/rightsizing.py. These data centres are owned, so
    the capacity is paid for whether or not anything runs on it, and there was
    no saving to sort by. Nodes and cores are what right-sizing actually
    moves.

    ACTIONABLE IS NOT THE SAME AS NOT-HEALTHY. A cluster can be classified
    Overprovisioned and still have node_delta == 0, because the recommended
    count is floored by N-1 failure tolerance and by node_failure_tolerance+1.
    It is genuinely underused and there is genuinely nothing to do about it.
    Ranking those beside real candidates is how a top-3 ends up listing three
    clusters nobody can act on. They are kept, not dropped - "why is this not
    on the list" deserves an answer - but they sort last, exactly as rejected
    candidates do in the hosting ranker.

    THE ORDER:

        1. actionable (node_delta != 0) before the rest
        2. reductions before expansions. Both are real findings, but "best
           right-sizing candidate" asks which capacity can be handed back.
        3. size of the change, largest first - nodes, then cores as the
           tie-break, because two clusters can free the same node count and
           very different capacity.
        4. cluster_code, so the order is total and a re-run is identical.

    Expansions therefore sort after every reduction, which is a real cost: a
    cluster approaching its ceiling is a risk, and risk is not less urgent
    than reclaimed capacity. What stops the ranking from hiding it is that the
    caller states the counts of each rather than showing a bare top 5 - see
    app.graph.nodes.generate_final_report.

    Takes dicts because that is what lives in the graph state
    (capacity_calculations["right_sizing"] is to_jsonable'd). Missing or null
    fields sort last rather than raising: a partial row is a reason to rank it
    low, not to fail an investigation that has 255 good ones.
    """
    def key(r: dict) -> tuple:
        delta = r.get("node_delta") or 0
        try:
            cores = Decimal(str(r.get("cpu_cores_delta") or "0"))
        except (InvalidOperation, ValueError):
            cores = Decimal("0")
        return (
            0 if delta else 1,      # actionable first
            0 if delta < 0 else 1,  # reductions before expansions
            -abs(Decimal(delta)),   # biggest change first, either direction
            -abs(cores),
            str(r.get("cluster_code") or ""),
        )

    return sorted(results, key=key)


def analyze_application_right_sizing(app: CmdbApplication) -> ApplicationRightSizingResult | None:
    settings = get_settings()
    hostings = hosting_repository.get_active_for_application(app.ApplicationId)
    if not hostings:
        return None
    hosting = hostings[0]

    usage = usage_repository.get_window_average(app.ApplicationId, settings.policy.utilization_window_days)
    from app.repositories import cluster_repository

    cluster = cluster_repository.get_by_id(hosting.ClusterId)
    cluster_code = cluster.ClusterCode if cluster else "UNKNOWN"

    if not usage:
        return ApplicationRightSizingResult(
            application_id=app.ApplicationId,
            application_code=app.ApplicationCode,
            cluster_code=cluster_code,
            allocated_cpu_cores=hosting.AllocatedCpuCores,
            allocated_memory_gb=hosting.AllocatedMemoryGb,
            allocated_storage_gb=hosting.AllocatedStorageGb,
            measured_cpu_consumed=None,
            measured_memory_consumed_gb=None,
            measured_storage_consumed_gb=None,
            recommended_cpu_cores=hosting.AllocatedCpuCores,
            recommended_memory_gb=hosting.AllocatedMemoryGb,
            recommended_storage_gb=hosting.AllocatedStorageGb,
            classification="RightSized",
            cpu_cores_delta=Decimal("0.00"),
            memory_gb_delta=Decimal("0.00"),
            rationale="No usage history available; keeping current allocation.",
        )

    margin = Decimal("1") + capacity.d(settings.policy.safety_margin_percent) / 100
    measured_cpu = capacity.d(usage["cpu_consumed"])
    measured_mem = capacity.d(usage["memory_consumed_gb"])
    measured_storage = capacity.d(usage["storage_consumed_gb"])

    recommended_cpu = round2(max(measured_cpu * margin, measured_cpu))
    recommended_mem = round2(max(measured_mem * margin, measured_mem))
    recommended_storage = round2(max(measured_storage * margin, measured_storage))

    over_allocated = (
        hosting.AllocatedCpuCores > recommended_cpu * Decimal("1.15")
        or hosting.AllocatedMemoryGb > recommended_mem * Decimal("1.15")
    )
    under_allocated = (
        hosting.AllocatedCpuCores < measured_cpu
        or hosting.AllocatedMemoryGb < measured_mem
    )

    if under_allocated:
        classification = "UnderAllocated"
        recommended_cpu = max(recommended_cpu, hosting.AllocatedCpuCores)
        recommended_mem = max(recommended_mem, hosting.AllocatedMemoryGb)
        rationale = (
            f"Measured consumption exceeds current allocation "
            f"(CPU {measured_cpu} vs allocated {hosting.AllocatedCpuCores}); recommend increasing allocation."
        )
    elif over_allocated:
        classification = "OverAllocated"
        rationale = (
            f"Allocation significantly exceeds measured consumption "
            f"(CPU {hosting.AllocatedCpuCores} allocated vs {measured_cpu} consumed); recommend reducing."
        )
    else:
        classification = "RightSized"
        recommended_cpu = hosting.AllocatedCpuCores
        recommended_mem = hosting.AllocatedMemoryGb
        recommended_storage = hosting.AllocatedStorageGb
        rationale = "Current allocation is consistent with measured consumption."

    cpu_delta = hosting.AllocatedCpuCores - recommended_cpu
    mem_delta = hosting.AllocatedMemoryGb - recommended_mem
    #  The allocation change itself, signed the same way as the cluster result:
    #  negative hands capacity back, positive asks for more. This replaced a
    #  currency figure that apportioned the cluster's internal chargeback rate
    #  by the share of CPU or memory released - a share of a number that does
    #  not change when the workload shrinks, because the hardware is owned and
    #  already paid for.

    return ApplicationRightSizingResult(
        application_id=app.ApplicationId,
        application_code=app.ApplicationCode,
        cluster_code=cluster_code,
        allocated_cpu_cores=hosting.AllocatedCpuCores,
        allocated_memory_gb=hosting.AllocatedMemoryGb,
        allocated_storage_gb=hosting.AllocatedStorageGb,
        measured_cpu_consumed=round2(measured_cpu),
        measured_memory_consumed_gb=round2(measured_mem),
        measured_storage_consumed_gb=round2(measured_storage),
        recommended_cpu_cores=recommended_cpu,
        recommended_memory_gb=recommended_mem,
        recommended_storage_gb=recommended_storage,
        classification=classification,
        cpu_cores_delta=round2(-cpu_delta),
        memory_gb_delta=round2(-mem_delta),
        rationale=rationale,
    )
