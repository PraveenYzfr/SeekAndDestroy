"""Weighted overall score + total-order ranking.

    Overall = 0.30*Capacity + 0.15*Compatibility + 0.15*Resiliency
            + 0.15*CostEfficiency + 0.10*DependencyLocality
            + 0.10*HistoricalPerformance + 0.05*(100 - OperationalRisk)

Computed entirely in ``Decimal`` with ``ROUND_HALF_UP`` to 2 dp, so re-running
the same inputs always produces the same score, byte for byte.
"""

from __future__ import annotations

from decimal import Decimal

from app.models.scoring import CandidateScore, NodeCandidateScore, NodeSubScores, SubScores
from app.scoring.subscores import round2
from app.scoring.weights import get_node_weights, get_weights


def compute_overall_score(subscores: SubScores) -> Decimal:
    w = get_weights()
    total = (
        w["capacity"] * subscores.capacity
        + w["compatibility"] * subscores.compatibility
        + w["resiliency"] * subscores.resiliency
        + w["cost"] * subscores.cost
        + w["dependency"] * subscores.dependency
        + w["historical"] * subscores.historical
        + w["risk"] * (Decimal("100") - subscores.risk)
    )
    return round2(total)


def rank_candidates(candidates: list[CandidateScore]) -> list[CandidateScore]:
    """Total order: score desc, cost asc, cluster_code asc. No ties, ever."""
    eligible = [c for c in candidates if c.eligibility_status == "Eligible"]
    rejected = [c for c in candidates if c.eligibility_status != "Eligible"]

    def sort_key(c: CandidateScore):
        score = c.overall_score if c.overall_score is not None else Decimal("-1")
        cost = c.estimated_monthly_cost if c.estimated_monthly_cost is not None else Decimal("Infinity")
        return (-score, cost, c.cluster_code)

    eligible_sorted = sorted(eligible, key=sort_key)
    for i, c in enumerate(eligible_sorted, start=1):
        c.rank = i

    rejected_sorted = sorted(rejected, key=lambda c: c.cluster_code)
    offset = len(eligible_sorted)
    for i, c in enumerate(rejected_sorted, start=1):
        c.rank = offset + i

    return eligible_sorted + rejected_sorted


def compute_node_overall_score(subscores: NodeSubScores) -> Decimal:
    """Node = 0.50*Capacity + 0.20*Cost + 0.20*Reliability + 0.10*(100 - Risk)

    Capacity carries more weight than it does at cluster level (0.50 vs 0.30)
    because the cluster-level discriminators - compatibility, dependency
    locality, resiliency tier - are constant across every node in a cluster.
    By the time a node is being ranked, the only questions left are "does the
    workload fit here", "what does this host cost", and "is this host healthy".
    """
    w = get_node_weights()
    total = (
        w["capacity"] * subscores.capacity
        + w["cost"] * subscores.cost
        + w["reliability"] * subscores.reliability
        + w["risk"] * (Decimal("100") - subscores.risk)
    )
    return round2(total)


def rank_node_candidates(candidates: list[NodeCandidateScore]) -> list[NodeCandidateScore]:
    """Total order within one cluster: score desc, cost asc, host_name asc.
    Rejected nodes sort after every eligible one, alphabetically - same
    contract as :func:`rank_candidates`, so "why not that host" stays
    answerable rather than being dropped on the floor.
    """
    eligible = [c for c in candidates if c.eligibility_status == "Eligible"]
    rejected = [c for c in candidates if c.eligibility_status != "Eligible"]

    def sort_key(c: NodeCandidateScore):
        score = c.overall_score if c.overall_score is not None else Decimal("-1")
        cost = c.estimated_monthly_cost if c.estimated_monthly_cost is not None else Decimal("Infinity")
        return (-score, cost, c.host_name)

    eligible_sorted = sorted(eligible, key=sort_key)
    for i, c in enumerate(eligible_sorted, start=1):
        c.rank = i

    rejected_sorted = sorted(rejected, key=lambda c: c.host_name)
    offset = len(eligible_sorted)
    for i, c in enumerate(rejected_sorted, start=1):
        c.rank = offset + i

    return eligible_sorted + rejected_sorted
