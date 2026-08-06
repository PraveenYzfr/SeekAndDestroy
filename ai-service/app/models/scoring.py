from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from app.models.capacity import ClusterCapacitySnapshot, ProjectedUtilization


class SubScores(BaseModel):
    capacity: Decimal
    compatibility: Decimal
    resiliency: Decimal
    cost: Decimal
    dependency: Decimal
    historical: Decimal
    risk: Decimal


class CandidateScore(BaseModel):
    cluster_id: int
    cluster_code: str
    eligibility_status: str  # Eligible | Rejected
    rule_results: list[dict]
    snapshot: Optional[ClusterCapacitySnapshot] = None
    projected: Optional[ProjectedUtilization] = None
    subscores: Optional[SubScores] = None
    overall_score: Optional[Decimal] = None
    estimated_monthly_cost: Optional[Decimal] = None
    rank: Optional[int] = None
    evidence: dict = {}
