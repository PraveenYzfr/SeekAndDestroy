"""Normalized hosting requirement.

Both Scenario A (an existing :class:`~app.models.entities.CmdbApplication`
looking for infrastructure) and Scenario B (a raw
:class:`~app.models.entities.CapacityRequest`) are converted into this one
shape before eligibility/scoring ever runs, so the rest of the pipeline never
has to branch on "was this an app or a raw request".
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from pydantic import BaseModel


class DependencyLocalityCheck(BaseModel):
    dependency_id: int
    dependency_type: str
    is_critical: bool
    latency_sensitivity: str
    target_description: str
    target_region: Optional[str]
    target_data_center: Optional[str]


class HostingRequirement(BaseModel):
    source_application_id: Optional[int] = None
    source_application_code: Optional[str] = None
    capacity_request_id: Optional[int] = None

    environment: str
    platform: str
    os_requirement: str
    cpu_cores: Decimal
    memory_gb: Decimal
    storage_gb: Decimal
    growth_percent: Decimal
    availability_tier: str
    data_classification: str
    preferred_location: Optional[str] = None
    criticality: str

    dependency_checks: list[DependencyLocalityCheck] = []

    @classmethod
    def from_application(cls, app, dependency_checks: list[DependencyLocalityCheck] | None = None) -> "HostingRequirement":
        return cls(
            source_application_id=app.ApplicationId,
            source_application_code=app.ApplicationCode,
            environment=app.Environment,
            platform=app.TechnologyPlatform,
            os_requirement=app.OperatingSystemRequirement,
            cpu_cores=app.CpuRequirement,
            memory_gb=app.MemoryRequirementGb,
            storage_gb=app.StorageRequirementGb,
            growth_percent=app.ExpectedAnnualGrowthPercent,
            availability_tier=app.AvailabilityTier,
            data_classification=app.DataClassification,
            preferred_location=app.PreferredLocation,
            criticality=app.BusinessCriticality,
            dependency_checks=dependency_checks or [],
        )

    @classmethod
    def from_capacity_request(cls, req) -> "HostingRequirement":
        # Raw capacity requests carry no BusinessCriticality; infer a
        # conservative "Medium" so RULE-010's resiliency check still applies a
        # sensible minimum instead of silently skipping it.
        return cls(
            capacity_request_id=req.CapacityRequestId,
            source_application_id=req.ApplicationId,
            environment=req.Environment,
            platform=req.RequiredPlatform,
            os_requirement="Any",
            cpu_cores=req.RequiredCpuCores,
            memory_gb=req.RequiredMemoryGb,
            storage_gb=req.RequiredStorageGb,
            growth_percent=req.ExpectedGrowthPercent,
            availability_tier=req.RequiredAvailabilityTier,
            data_classification=req.DataClassification,
            preferred_location=req.PreferredLocation,
            criticality="Medium",
        )
