"""Pydantic models mirroring the ``sad`` schema tables exactly.

These are the shapes repositories return. Every field name matches the SQL
column name (PascalCase, as-is) so mapping a row to a model is a plain
``Model(**row)`` with no translation layer to keep in sync.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import BaseModel, ConfigDict


class _Entity(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Employee(_Entity):
    EmployeeId: int
    EmployeeNumber: str
    DisplayName: str
    Email: str
    IsActive: bool
    # Deliberately excluded: PasswordHash. It exists on sad.Employee but is
    # never loaded into this model, so it cannot leak through any endpoint
    # that returns an Employee. The one code path that needs it reads the
    # column directly - employee_repository.get_password_hash().
    PasswordUpdatedAt: Optional[datetime] = None


class SupportGroup(_Entity):
    SupportGroupId: int
    GroupName: str
    Email: str
    IsActive: bool


class CmdbApplication(_Entity):
    ApplicationId: int
    ApplicationCode: str
    ApplicationName: str
    Description: Optional[str]
    BusinessCriticality: str
    Environment: str
    LifecycleStatus: str
    TechnologyPlatform: str
    OperatingSystemRequirement: str
    CpuRequirement: Decimal
    MemoryRequirementGb: Decimal
    StorageRequirementGb: Decimal
    ExpectedAnnualGrowthPercent: Decimal
    AvailabilityTier: str
    DataClassification: str
    PreferredLocation: Optional[str]
    OwnerEmployeeId: int
    SupportGroupId: int
    CreatedAt: datetime
    UpdatedAt: datetime


class InfrastructureCluster(_Entity):
    ClusterId: int
    ClusterCode: str
    ClusterName: str
    ClusterType: str
    Platform: str
    OperatingSystem: str
    Environment: str
    DataCenter: str
    Region: str
    LifecycleStatus: str
    NodeCount: int
    TotalCpuCores: Decimal
    TotalMemoryGb: Decimal
    TotalStorageGb: Decimal
    ReservedCpuPercent: Decimal
    ReservedMemoryPercent: Decimal
    MonthlyCost: Decimal
    AvailabilityTier: str
    ComplianceClassification: str
    CreatedAt: datetime
    UpdatedAt: datetime


class ClusterNode(_Entity):
    NodeId: int
    ClusterId: int
    HostName: str
    IpAddress: str
    CpuCores: Decimal
    MemoryGb: Decimal
    StorageGb: Decimal
    LifecycleStatus: str
    LastSeenAt: datetime
    MonthlyCost: Decimal
    CreatedAt: datetime
    UpdatedAt: datetime


class ApplicationHosting(_Entity):
    HostingId: int
    ApplicationId: int
    ClusterId: int
    NodeId: Optional[int]
    Environment: str
    AllocatedCpuCores: Decimal
    AllocatedMemoryGb: Decimal
    AllocatedStorageGb: Decimal
    HostingStatus: str
    IsPrimary: bool
    HostedSince: datetime
    CreatedAt: datetime
    UpdatedAt: datetime


class ClusterUtilization(_Entity):
    UtilizationId: int
    ClusterId: int
    MetricDateTime: datetime
    CpuUsedPercent: Decimal
    MemoryUsedPercent: Decimal
    StorageUsedPercent: Decimal
    NetworkUsedPercent: Decimal
    ActiveWorkloadCount: int
    RequestVolume: int


class NodeUtilization(_Entity):
    UtilizationId: int
    NodeId: int
    MetricDateTime: datetime
    CpuUsedPercent: Decimal
    MemoryUsedPercent: Decimal
    StorageUsedPercent: Decimal
    NetworkUsedPercent: Decimal


class ApplicationUsage(_Entity):
    UsageId: int
    ApplicationId: int
    UsageDateTime: datetime
    UserCount: int
    RequestCount: int
    CpuConsumed: Decimal
    MemoryConsumedGb: Decimal
    StorageConsumedGb: Decimal
    ResponseTimeMs: int


class ApplicationDependency(_Entity):
    DependencyId: int
    SourceApplicationId: int
    TargetApplicationId: Optional[int]
    TargetClusterId: Optional[int]
    DependencyType: str
    LatencySensitivity: str
    IsCritical: bool
    IsActive: bool


class Incident(_Entity):
    IncidentId: int
    ApplicationId: Optional[int]
    ClusterId: Optional[int]
    NodeId: Optional[int]
    Severity: str
    OpenedAt: datetime
    ClosedAt: Optional[datetime]
    Status: str
    RootCauseCategory: str


class CapacityRequest(_Entity):
    CapacityRequestId: int
    ApplicationId: Optional[int]
    RequestedBy: int
    Environment: str
    RequiredCpuCores: Decimal
    RequiredMemoryGb: Decimal
    RequiredStorageGb: Decimal
    ExpectedGrowthPercent: Decimal
    RequiredAvailabilityTier: str
    RequiredPlatform: str
    PreferredLocation: Optional[str]
    DataClassification: str
    RequiredByDate: Optional[date]
    Status: str
    CreatedAt: datetime


class InfrastructureRecommendation(_Entity):
    RecommendationId: int
    InvestigationId: int
    CapacityRequestId: Optional[int]
    ApplicationId: Optional[int]
    RecommendationType: str
    CandidateEntityType: str
    CandidateEntityId: int
    Rank: int
    EligibilityStatus: str
    OverallScore: Optional[Decimal]
    CapacityScore: Optional[Decimal]
    CompatibilityScore: Optional[Decimal]
    CostScore: Optional[Decimal]
    ResiliencyScore: Optional[Decimal]
    DependencyScore: Optional[Decimal]
    RiskScore: Optional[Decimal]
    ProjectedCpuUtilization: Optional[Decimal]
    ProjectedMemoryUtilization: Optional[Decimal]
    ProjectedStorageUtilization: Optional[Decimal]
    ProjectedHeadroomPercent: Optional[Decimal]
    EstimatedMonthlyCost: Optional[Decimal]
    Explanation: Optional[str]
    EvidenceJson: Optional[str]
    Status: str
    CreatedAt: datetime


class RecommendationDecision(_Entity):
    DecisionId: int
    RecommendationId: int
    Decision: str
    DecisionReason: Optional[str]
    DecidedBy: int
    DecidedAt: datetime


class Investigation(_Entity):
    InvestigationId: int
    Query: str
    InvestigationType: str
    Status: str
    CreatedBy: int
    StartedAt: datetime
    CompletedAt: Optional[datetime]
    # NULL for every investigation that did not come from the chat - the
    # structured screens and the MCP client create investigations too.
    ConversationId: Optional[str] = None


class Conversation(_Entity):
    ConversationId: str
    CreatedBy: int
    StartedAt: datetime
    LastActivityAt: datetime


class ConversationTurn(_Entity):
    TurnId: int
    ConversationId: str
    Role: str
    Message: str
    InvestigationId: Optional[int]
    CreatedAt: datetime


class AgentAuditLog(_Entity):
    AuditId: int
    InvestigationId: Optional[int]
    GraphNode: Optional[str]
    ToolName: str
    InputJson: Optional[str]
    OutputJson: Optional[str]
    StartedAt: datetime
    CompletedAt: Optional[datetime]
    Success: Optional[bool]
    ErrorMessage: Optional[str]
