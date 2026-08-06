from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel

from app.models.capacity import ClusterCapacitySnapshot


class ClusterRightSizingResult(BaseModel):
    cluster_id: int
    cluster_code: str
    classification: str  # Overprovisioned | Underprovisioned | Healthy
    snapshot: ClusterCapacitySnapshot
    current_node_count: int
    recommended_node_count: int
    node_delta: int
    monthly_cost_per_node: Decimal
    estimated_monthly_savings: Decimal
    estimated_annual_savings: Decimal
    risks: list[str]
    rationale: str


class ApplicationRightSizingResult(BaseModel):
    application_id: int
    application_code: str
    cluster_code: str
    allocated_cpu_cores: Decimal
    allocated_memory_gb: Decimal
    allocated_storage_gb: Decimal
    measured_cpu_consumed: Optional[Decimal]
    measured_memory_consumed_gb: Optional[Decimal]
    measured_storage_consumed_gb: Optional[Decimal]
    recommended_cpu_cores: Decimal
    recommended_memory_gb: Decimal
    recommended_storage_gb: Decimal
    classification: str  # OverAllocated | UnderAllocated | RightSized
    estimated_monthly_savings: Decimal
    estimated_annual_savings: Decimal
    rationale: str


class ConsolidationCandidate(BaseModel):
    application_id: int
    application_code: str
    current_cluster_code: str
    target_cluster_code: str
    reason: str
    estimated_monthly_savings: Decimal
    blocking_constraints: list[str]
    feasible: bool
