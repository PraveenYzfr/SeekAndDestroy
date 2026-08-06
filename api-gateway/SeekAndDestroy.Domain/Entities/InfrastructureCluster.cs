namespace SeekAndDestroy.Domain.Entities;

public sealed class InfrastructureCluster
{
    public int ClusterId { get; init; }
    public string ClusterCode { get; init; } = string.Empty;
    public string ClusterName { get; init; } = string.Empty;
    public string ClusterType { get; init; } = string.Empty;
    public string Platform { get; init; } = string.Empty;
    public string OperatingSystem { get; init; } = string.Empty;
    public string Environment { get; init; } = string.Empty;
    public string DataCenter { get; init; } = string.Empty;
    public string Region { get; init; } = string.Empty;
    public string LifecycleStatus { get; init; } = string.Empty;
    public int NodeCount { get; init; }
    public decimal TotalCpuCores { get; init; }
    public decimal TotalMemoryGb { get; init; }
    public decimal TotalStorageGb { get; init; }
    public decimal ReservedCpuPercent { get; init; }
    public decimal ReservedMemoryPercent { get; init; }
    public decimal MonthlyCost { get; init; }
    public string AvailabilityTier { get; init; } = string.Empty;
    public string ComplianceClassification { get; init; } = string.Empty;
    public DateTime CreatedAt { get; init; }
    public DateTime UpdatedAt { get; init; }
}
