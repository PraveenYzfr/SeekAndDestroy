"""How much depends on a cluster, for weighting the risk of changing it.

THE GAP
-------
change_risk_subscore reads two signals: how many changes are queued against a
cluster, and how often changes there have gone wrong. Both describe the CHANGE.
Neither describes the CONSEQUENCE.

Measured on the current estate, a sample of forty clusters: the most
depended-upon carries 378 dependent CIs and 27 applications, the median carries
101 and 4. A maintenance window on the busiest and the quietest is the same
change and is not the same risk, and until now they scored identically.

The graph makes the consequence computable, so the churn half of the change
score can be weighted by it.

WHY THIS SCALES CHURN AND NOT THE FAILURE RATE
----------------------------------------------
Churn is forward-looking - changes that have not happened yet, which this
workload would land in the middle of. Weighting it by how much depends on the
cluster is asking "how bad would it be if one of those goes wrong", which is the
right question about a future event.

The failure rate is backward-looking and already an observed outcome. Scaling it
by exposure would count the same fact twice: a cluster that many things depend on
tends to accumulate more change history, so its failure rate already reflects its
importance. Multiplying that by exposure again would compound a correlation into
a penalty.

WHY AN ABSOLUTE REFERENCE, NOT NORMALISED ACROSS THE CANDIDATE SET
------------------------------------------------------------------
cost_efficiency_scores normalises min-max across whatever candidates are in the
run, and that is right for cost: "cheap" only means anything relative to the
alternatives.

Risk is not relative. A cluster that 29 applications depend on is exactly that
risky whether it is being compared against a busier cluster or an idle one, and a
normalised figure would move a cluster's risk score because of which OTHER
clusters happened to be considered - so the same placement question asked twice,
with a different candidate filter, would give two different risk answers for the
same cluster. That is not a property a risk number may have.
"""

from __future__ import annotations

import structlog

from app.repositories import ci_graph_repository as graph
from app.repositories.base import T, fetch_all

logger = structlog.get_logger(__name__)

#  exposure_multiplier and its two constants MOVED to app.scoring.exposure.
#
#  They were arithmetic - an int in, a Decimal out, no database - sitting here
#  because their caller's DATA comes from this module. app.scoring.subscores had
#  to reach UP into services to call them, with a function-local import written
#  to break the cycle a module-level one would have caused.
#
#  scoring is the layer the trust boundary rests on. A scoring module that
#  depends on a service is one whose result depends on what that service later
#  becomes. This module still answers WHAT DEPENDS ON A CLUSTER; what that is
#  worth is now decided next to the rest of the arithmetic.


def exposure_for_clusters(cluster_ids: list[int]) -> dict[int, dict]:
    """Dependent CI and application counts, keyed by InfrastructureCluster id.

    WHY THIS IS NOT blast_radius(cluster)
    -------------------------------------
    It was, and it silently stopped working. The original version walked
    downward from the cluster CI, which was correct while migration 008's
    shortcut edge existed: cluster -[Runs on]-> application, straight down.

    That edge was removed once VMs became real - 1,933 of them - because it
    manufactured redundancy. The genuine path is:

        cluster -[Member of]-> node <-[Runs on]- server -[Hosted on]-> vm
                -[Runs on]-> application

    which REVERSES DIRECTION at the node: the server is the parent of its node,
    not the child. A downward-only walk dead-ends on the nodes and returns them
    alone. It does not error. It returned 0 dependent applications for every
    cluster in the estate, so exposure_multiplier returned 1.0 everywhere and
    the weighting quietly did nothing at all.

    Caught by measuring after a graph change rather than by any test - the tests
    asserted `dependent_cis >= dependent_applications >= 0`, which 0 satisfies.
    There is now a test asserting the count is non-zero for a real cluster,
    because a relationship that holds trivially is not a check.

    HOW IT WORKS NOW
    ----------------
    servers_under_clusters already resolves the cluster-to-hardware step by
    CLASS rather than by asserting an edge direction, which is exactly the
    reversal that broke the walk. From the servers the path is plain containment,
    so it is one set-based join rather than a walk per cluster - 256 clusters
    times ~40 servers would be 10,000 traversals on the hot path.
    """
    if not cluster_ids:
        return {}

    # InfrastructureCluster ids are not CI ids. The CMDB knows a cluster by its
    # ConfigurationItem row and the placement engine by its own primary key;
    # the code bridges them.
    params = {f"c{i}": int(c) for i, c in enumerate(cluster_ids)}
    placeholders = ", ".join(f":{k}" for k in params)
    params["cls"] = graph.CLASS_CLUSTER
    try:
        rows = fetch_all(
            f"SELECT ic.ClusterId, ci.CiId "
            f"FROM {T('InfrastructureCluster')} ic "
            f"JOIN {T('ConfigurationItem')} ci "
            f"  ON ci.Name = ic.ClusterCode AND ci.ClassName = :cls "
            f"WHERE ic.ClusterId IN ({placeholders})",
            params,
            max_rows=10_000,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("change_exposure.lookup_failed", error=str(exc))
        return {}

    out: dict[int, dict] = {}
    for row in rows:
        try:
            out[row["ClusterId"]] = _exposure_for_cluster_ci(row["CiId"])
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "change_exposure.walk_failed", cluster_id=row["ClusterId"], error=str(exc)
            )
    return out


def _exposure_for_cluster_ci(cluster_ci_id: int) -> dict:
    """What rides on one cluster: its hardware, the VMs on it, and their apps."""
    servers = graph.servers_under_clusters([cluster_ci_id])
    if not servers:
        # A cluster with no resolvable hardware. Absent, not zero - the caller
        # gets no entry and the multiplier stays 1.0.
        raise ValueError(f"no servers resolve under cluster CI {cluster_ci_id}")

    params = {f"s{i}": int(n.ci_id) for i, n in enumerate(servers)}
    placeholders = ", ".join(f":{k}" for k in params)
    rows = fetch_all(
        f"SELECT vm.CiId AS VmId, app.CiId AS AppId, app.ClassName AS AppClass "
        f"FROM {T('CiRelationship')} hv "
        f"JOIN {T('ConfigurationItem')} vm ON vm.CiId = hv.ChildCiId "
        f"LEFT JOIN {T('CiRelationship')} va ON va.ParentCiId = vm.CiId "
        f"LEFT JOIN {T('ConfigurationItem')} app ON app.CiId = va.ChildCiId "
        f"WHERE hv.ParentCiId IN ({placeholders}) "
        f"  AND hv.TypeId = {graph.HOSTED_ON}",
        params,
        max_rows=200_000,
    )
    vms = {r["VmId"] for r in rows if r["VmId"] is not None}
    apps = {r["AppId"] for r in rows if r["AppId"] and r["AppClass"] == graph.CLASS_APPLICATION}
    # Everything the cluster carries: its own hardware, the VMs on it, and the
    # workloads on those. Databases and other VM-hosted CIs are counted in the
    # CI total but not in the application count, which is what the multiplier
    # reads - a change is risky in proportion to the WORKLOADS it can disturb.
    return {
        "dependent_cis": len(servers) + len(vms) + len(apps),
        "dependent_applications": len(apps),
        "exposure_truncated": False,
    }
