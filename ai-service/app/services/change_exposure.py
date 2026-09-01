"""How much depends on a cluster, for weighting the risk of changing it.

THE GAP
-------
change_risk_subscore reads two signals: how many changes are queued against a
cluster, and how often changes there have gone wrong. Both describe the CHANGE.
Neither describes the CONSEQUENCE.

Measured on the current estate, a sample of forty clusters: the most
depended-upon carries 45 dependent CIs and 29 applications, the least carries 3
and none. A maintenance window on those two is the same change and is not the
same risk, and until now they scored identically.

Blast radius makes the consequence computable, so the churn half of the change
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

from decimal import Decimal

import structlog

from app.repositories import ci_graph_repository as graph
from app.repositories.base import T, fetch_all

logger = structlog.get_logger(__name__)

#: Dependent applications at which the churn penalty is doubled. Chosen from the
#: estate as measured - the busiest cluster in a forty-cluster sample carried 29
#: dependent applications - so this sits just above the observed top and a
#: typical cluster lands well under it.
#:
#: The estate is about to grow roughly forty-fold. Revisit this then: it is an
#: absolute threshold by deliberate choice (see the module docstring), which
#: means it does not self-calibrate and will understate exposure if the average
#: cluster ends up carrying far more than it does today.
EXPOSURE_REFERENCE_APPS = Decimal("30")

#: Ceiling on the multiplier. Without it a hub cluster could contribute an
#: unbounded penalty and dominate a score that has six other dimensions.
MAX_EXPOSURE_MULTIPLIER = Decimal("2.0")


def exposure_multiplier(dependent_applications: int | None) -> Decimal:
    """1.0 for a cluster nothing depends on, rising to MAX at the reference.

    Returns 1.0 - no effect - for None. Absent exposure data must leave the
    change score exactly as it was rather than inventing a penalty, for the same
    reason RULE-012 passes on silence: an incomplete CMDB must not quietly make
    an estate look dangerous.
    """
    if not dependent_applications or dependent_applications <= 0:
        return Decimal("1.0")
    ratio = Decimal(dependent_applications) / EXPOSURE_REFERENCE_APPS
    return min(MAX_EXPOSURE_MULTIPLIER, Decimal("1.0") + ratio)


def exposure_for_clusters(cluster_ids: list[int]) -> dict[int, dict]:
    """Dependent CI and application counts, keyed by InfrastructureCluster id.

    One graph walk per cluster. That is acceptable because the walk is shallow
    and bounded, and because the alternative - one recursive CTE over every
    cluster at once - would need a starting-node column threaded through the
    recursion and would be considerably harder to read for a saving that does not
    show up at this size.

    Best-effort throughout: any failure yields no entry for that cluster, which
    means a multiplier of 1.0 and a change score identical to today's.
    """
    if not cluster_ids:
        return {}

    # InfrastructureCluster ids are not CI ids. The CMDB knows a cluster by its
    # ConfigurationItem row, and the placement engine knows it by its own table's
    # primary key; joining them on the code is what bridges the two models.
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
            walk = graph.blast_radius(row["CiId"])
        except Exception as exc:  # noqa: BLE001
            logger.warning("change_exposure.walk_failed", cluster_id=row["ClusterId"], error=str(exc))
            continue
        out[row["ClusterId"]] = {
            "dependent_cis": len(walk),
            "dependent_applications": len(walk.of_class(graph.CLASS_APPLICATION)),
            # A truncated walk under-counts, so the exposure is a floor. Recorded
            # rather than acted on: under-stating exposure is the safe direction
            # for a penalty, unlike for resiliency where under-stating invents a
            # single point of failure.
            "exposure_truncated": walk.hit_ceiling,
        }
    return out
