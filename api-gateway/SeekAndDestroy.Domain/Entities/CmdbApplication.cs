namespace SeekAndDestroy.Domain.Entities;

/// <summary>Read-only projection of sad.CmdbApplication. The gateway never
/// writes to CMDB tables - all mutation happens through the AI service's
/// governance tables (CapacityRequest, Investigation, InfrastructureRecommendation,
/// RecommendationDecision) via the recommendation endpoints.</summary>
public sealed class CmdbApplication
{
    public int ApplicationId { get; init; }
    public string ApplicationCode { get; init; } = string.Empty;
    public string ApplicationName { get; init; } = string.Empty;
    public string? Description { get; init; }
    public string BusinessCriticality { get; init; } = string.Empty;
    public string Environment { get; init; } = string.Empty;
    public string LifecycleStatus { get; init; } = string.Empty;
    public string TechnologyPlatform { get; init; } = string.Empty;
    public string OperatingSystemRequirement { get; init; } = string.Empty;
    public decimal CpuRequirement { get; init; }
    public decimal MemoryRequirementGb { get; init; }
    public decimal StorageRequirementGb { get; init; }
    public decimal ExpectedAnnualGrowthPercent { get; init; }
    public string AvailabilityTier { get; init; } = string.Empty;
    public string DataClassification { get; init; } = string.Empty;
    public string? PreferredLocation { get; init; }
    public int OwnerEmployeeId { get; init; }
    public int SupportGroupId { get; init; }
    public DateTime CreatedAt { get; init; }
    public DateTime UpdatedAt { get; init; }
}
