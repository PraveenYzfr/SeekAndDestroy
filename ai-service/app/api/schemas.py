"""Request/response DTOs for the FastAPI layer. Kept separate from the
internal service-layer Pydantic models so the wire contract can evolve
independently of engine internals.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, Field


class CreateInvestigationRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    created_by_employee_id: int


class ResumeInvestigationRequest(BaseModel):
    decision: str = Field(description="approve | reject | more_analysis")
    reviewer_employee_id: int = Field(gt=0, description="Required - decisions cannot be anonymous")
    comments: Optional[str] = None


class HostingRecommendationRequest(BaseModel):
    application_code: str
    data_center: Optional[str] = Field(None, description="Restrict candidates to this data center only")
    top_n: Optional[int] = Field(None, gt=0, description="Return only the top N eligible candidates (rejected candidates are never truncated)")


class CapacityRecommendationRequest(BaseModel):
    environment: str
    cpu_cores: float = Field(gt=0)
    memory_gb: float = Field(gt=0)
    storage_gb: float = Field(gt=0)
    platform: str
    availability_tier: str
    data_classification: str
    preferred_location: Optional[str] = None
    data_center: Optional[str] = Field(None, description="Restrict candidates to this data center only")
    top_n: Optional[int] = Field(None, gt=0, description="Return only the top N eligible candidates")
    expected_growth_percent: float = 0.0
    required_by_date: Optional[date] = None
    requested_by_employee_id: int
    application_id: Optional[int] = None


class QuickRecommendationRequest(BaseModel):
    """A lightweight "just show me the best clusters for this shape of
    workload" lookup - no CapacityRequest row is created, unlike
    /api/capacity/recommendations. Every field except cpu_cores/memory_gb has
    a sensible default so an engineer can ask "2 cores, 2 GB" and get an
    answer immediately.
    """

    cpu_cores: float = Field(gt=0)
    memory_gb: float = Field(gt=0)
    storage_gb: float = Field(100.0, gt=0)
    environment: str = "Production"
    platform: str = "Kubernetes"
    availability_tier: str = "Tier-2"
    data_classification: str = "Internal"
    data_center: Optional[str] = None
    top_n: int = Field(10, gt=0)


class ClusterUtilizationRankingRequest(BaseModel):
    order: str = Field("least", description="least | most")
    limit: int = Field(10, gt=0, le=200)
    environment: Optional[str] = None
    data_center: Optional[str] = None
    platform: Optional[str] = None


class ClusterRightSizingRequest(BaseModel):
    cluster_code: Optional[str] = None


class ApplicationRightSizingRequest(BaseModel):
    application_code: Optional[str] = None


class ConsolidationAnalysisRequest(BaseModel):
    environment: Optional[str] = None


class ForecastRequest(BaseModel):
    cluster_code: str
    horizon_days: int = 90


class RecommendationDecisionRequest(BaseModel):
    decision: str = Field(description="Approve | Reject | RequestMoreAnalysis")
    reviewer_employee_id: int = Field(gt=0, description="Required - decisions cannot be anonymous")
    reason: Optional[str] = None
