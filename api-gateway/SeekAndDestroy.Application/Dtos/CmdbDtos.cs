namespace SeekAndDestroy.Application.Dtos;

public sealed record ApplicationDto(
    int ApplicationId, string ApplicationCode, string ApplicationName, string? Description,
    string BusinessCriticality, string Environment, string LifecycleStatus, string TechnologyPlatform,
    decimal CpuRequirement, decimal MemoryRequirementGb, decimal StorageRequirementGb,
    decimal ExpectedAnnualGrowthPercent, string AvailabilityTier, string DataClassification,
    string? PreferredLocation);

public sealed record ClusterDto(
    int ClusterId, string ClusterCode, string ClusterName, string ClusterType, string Platform,
    string Environment, string DataCenter, string Region, string LifecycleStatus, int NodeCount,
    decimal TotalCpuCores, decimal TotalMemoryGb, decimal TotalStorageGb, decimal MonthlyCost,
    string AvailabilityTier, string ComplianceClassification);
