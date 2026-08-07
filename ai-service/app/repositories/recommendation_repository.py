from __future__ import annotations

from typing import Any

from app.models.entities import InfrastructureRecommendation, RecommendationDecision
from app.repositories.base import T, execute, execute_insert, fetch_all, fetch_one


def save(rec: dict[str, Any]) -> int:
    """``rec`` keys must match InfrastructureRecommendation column names exactly."""
    return execute_insert(T("InfrastructureRecommendation"), "RecommendationId", rec)


def save_many(recs: list[dict[str, Any]]) -> list[int]:
    return [save(r) for r in recs]


def get_by_id(recommendation_id: int) -> InfrastructureRecommendation | None:
    row = fetch_one(
        f"SELECT * FROM {T('InfrastructureRecommendation')} WHERE RecommendationId = :id",
        {"id": recommendation_id},
    )
    return InfrastructureRecommendation(**row) if row else None


def list_for_investigation(investigation_id: int) -> list[InfrastructureRecommendation]:
    """Ordered for display: each cluster followed immediately by the hosts
    recommended inside it (cluster 1, its hosts 1..M, cluster 2, its hosts...).

    Ranks are scoped to their own level - a node's Rank is its position within
    its cluster, not within the investigation - so ordering by [Rank] alone
    would interleave every cluster's rank-1 host with every other's. The joins
    resolve each Node row back to its parent cluster's rank; a node whose
    parent cluster was not persisted falls back to its own rank via COALESCE
    rather than disappearing.
    """
    rows = fetch_all(
        f"""
        SELECT r.* FROM {T('InfrastructureRecommendation')} r
        LEFT JOIN {T('ClusterNode')} n
               ON r.CandidateEntityType = 'Node' AND n.NodeId = r.CandidateEntityId
        LEFT JOIN {T('InfrastructureRecommendation')} parent
               ON parent.InvestigationId = r.InvestigationId
              AND parent.CandidateEntityType = 'Cluster'
              AND parent.CandidateEntityId = n.ClusterId
        WHERE r.InvestigationId = :id
        ORDER BY
            COALESCE(parent.[Rank], r.[Rank]),
            CASE WHEN r.CandidateEntityType = 'Cluster' THEN 0 ELSE 1 END,
            r.[Rank]
        """,
        {"id": investigation_id},
    )
    return [InfrastructureRecommendation(**r) for r in rows]


def list_recent(status: str | None = None, limit: int = 200) -> list[InfrastructureRecommendation]:
    if status:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('InfrastructureRecommendation')} "
            f"WHERE Status = :status ORDER BY CreatedAt DESC",
            {"status": status, "limit": limit},
        )
    else:
        rows = fetch_all(
            f"SELECT TOP (:limit) * FROM {T('InfrastructureRecommendation')} ORDER BY CreatedAt DESC",
            {"limit": limit},
        )
    return [InfrastructureRecommendation(**r) for r in rows]


def update_status(recommendation_id: int, status: str) -> None:
    execute(
        f"UPDATE {T('InfrastructureRecommendation')} SET Status = :status WHERE RecommendationId = :id",
        {"status": status, "id": recommendation_id},
    )


def save_decision(
    *, recommendation_id: int, decision: str, decision_reason: str | None, decided_by: int
) -> int:
    return execute_insert(
        T("RecommendationDecision"),
        "DecisionId",
        {
            "RecommendationId": recommendation_id,
            "Decision": decision,
            "DecisionReason": decision_reason,
            "DecidedBy": decided_by,
        },
    )


def get_decisions_for_recommendation(recommendation_id: int) -> list[RecommendationDecision]:
    rows = fetch_all(
        f"SELECT * FROM {T('RecommendationDecision')} "
        f"WHERE RecommendationId = :id ORDER BY DecidedAt DESC",
        {"id": recommendation_id},
    )
    return [RecommendationDecision(**r) for r in rows]
