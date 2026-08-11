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
    # Optional and no longer trusted as-is: the authenticated caller's employee_id
    # (from their Bearer token, see app.api.auth) is always the value actually used.
    # If both are present and disagree, the request is rejected (403) rather than
    # silently preferring one - that mismatch means something is actually wrong.
    created_by_employee_id: Optional[int] = None


class ResumeInvestigationRequest(BaseModel):
    decision: str = Field(description="approve | reject | more_analysis")
    reviewer_employee_id: Optional[int] = Field(None, description="No longer trusted - see app.api.auth")
    comments: Optional[str] = None


class DevTokenRequest(BaseModel):
    employee_number: str = Field(min_length=1, description="Must match an active Employee row")


class LoginRequest(BaseModel):
    """Username/password sign-in. ``username`` accepts either the employee
    number (E1001) or the email address - both are UNIQUE on sad.Employee.
    """

    username: str = Field(min_length=1, description="Employee number or email address")
    password: str = Field(min_length=1, repr=False)


class _NodeDrillDownMixin(BaseModel):
    """Shared knobs for the "top N clusters, top M hosts inside each" shape
    that every placement endpoint now returns. Both default to the configured
    policy (``SAD_POLICY__TOP_CLUSTERS`` / ``SAD_POLICY__TOP_NODES_PER_CLUSTER``,
    3 and 3) rather than to a hardcoded literal, so the API and the graph
    pipeline can never disagree about how deep a shortlist goes.
    """

    top_n: Optional[int] = Field(
        None, gt=0,
        description="Top N eligible clusters (default: policy top_clusters). Rejected candidates are never truncated.",
    )
    top_nodes_per_cluster: Optional[int] = Field(
        None, gt=0,
        description="Top M eligible hosts ranked inside each returned cluster (default: policy top_nodes_per_cluster).",
    )
    include_nodes: bool = Field(
        True, description="Set false to skip per-host ranking and return cluster-level candidates only.",
    )


class HostingRecommendationRequest(_NodeDrillDownMixin):
    application_code: str
    data_center: Optional[str] = Field(None, description="Restrict candidates to this data center only")


class CapacityRecommendationRequest(_NodeDrillDownMixin):
    environment: str
    cpu_cores: float = Field(gt=0)
    memory_gb: float = Field(gt=0)
    storage_gb: float = Field(gt=0)
    platform: str
    availability_tier: str
    data_classification: str
    preferred_location: Optional[str] = None
    data_center: Optional[str] = Field(None, description="Restrict candidates to this data center only")
    expected_growth_percent: float = 0.0
    required_by_date: Optional[date] = None
    requested_by_employee_id: Optional[int] = Field(None, description="No longer trusted - see app.api.auth")
    application_id: Optional[int] = None


class QuickRecommendationRequest(_NodeDrillDownMixin):
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
    reviewer_employee_id: Optional[int] = Field(None, description="No longer trusted - see app.api.auth")
    reason: Optional[str] = None
