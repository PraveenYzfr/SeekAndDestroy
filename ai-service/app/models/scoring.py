from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from app.models.capacity import ClusterCapacitySnapshot, NodeCapacitySnapshot, ProjectedUtilization


class SubScores(BaseModel):
    capacity: Decimal
    compatibility: Decimal
    resiliency: Decimal
    cost: Decimal
    dependency: Decimal
    historical: Decimal
    risk: Decimal
    #: How safe the cluster looks from its change record - upcoming churn and
    #: smoothed failure rate. Defaulted so every existing construction of this
    #: model keeps working; a cluster with no change history scores 100, which
    #: means "nothing known against it", not "proven stable".
    change_risk: Decimal = Decimal("100")


class CandidateScore(BaseModel):
    cluster_id: int
    cluster_code: str
    #: Carried here, not just on InfrastructureCluster, because a follow-up
    #: ("give me from a different DC") needs to know which data centers a
    #: PRIOR investigation's shortlist already covered - and by then all that
    #: survives is the persisted candidate_scores, not the cluster rows. See
    #: app.services.refinement.rejection_reasons, which already reads this
    #: field (candidate.get("data_center")) and, until now, always got None.
    data_center: Optional[str] = None
    eligibility_status: str  # Eligible | Rejected
    rule_results: list[dict]
    snapshot: Optional[ClusterCapacitySnapshot] = None
    projected: Optional[ProjectedUtilization] = None
    subscores: Optional[SubScores] = None
    overall_score: Optional[Decimal] = None
    estimated_monthly_cost: Optional[Decimal] = None
    rank: Optional[int] = None
    evidence: dict = {}
    top_nodes: list["NodeCandidateScore"] = []


class NodeSubScores(BaseModel):
    """Four sub-scores, not seven. Compatibility, dependency locality and
    resiliency tier are cluster attributes shared by every node in the cluster
    - they order clusters, they cannot order nodes within one.
    """

    capacity: Decimal
    cost: Decimal
    reliability: Decimal
    risk: Decimal


class NodeCandidateScore(BaseModel):
    node_id: int
    host_name: str
    cluster_id: int
    cluster_code: str
    lifecycle_status: str
    eligibility_status: str  # Eligible | Rejected
    rule_results: list[dict] = []
    snapshot: Optional[NodeCapacitySnapshot] = None
    projected: Optional[ProjectedUtilization] = None
    subscores: Optional[NodeSubScores] = None
    overall_score: Optional[Decimal] = None
    estimated_monthly_cost: Optional[Decimal] = None
    rank: Optional[int] = None
    evidence: dict = {}


CandidateScore.model_rebuild()
