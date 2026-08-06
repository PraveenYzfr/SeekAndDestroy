using Dapper;
using SeekAndDestroy.Application.Interfaces;
using SeekAndDestroy.Domain.Entities;

namespace SeekAndDestroy.Infrastructure.Persistence;

/// <summary>Read-only Dapper queries against the sad schema. Every statement
/// here is parameterized; the schema name "sad" is a compile-time literal,
/// never derived from request input.</summary>
public sealed class CmdbRepository(ISqlConnectionFactory connectionFactory) : ICmdbRepository
{
    public async Task<IReadOnlyList<CmdbApplication>> GetApplicationsAsync(string? environment, CancellationToken ct)
    {
        const string sql = """
            SELECT ApplicationId, ApplicationCode, ApplicationName, Description, BusinessCriticality,
                   Environment, LifecycleStatus, TechnologyPlatform, OperatingSystemRequirement,
                   CpuRequirement, MemoryRequirementGb, StorageRequirementGb, ExpectedAnnualGrowthPercent,
                   AvailabilityTier, DataClassification, PreferredLocation, OwnerEmployeeId, SupportGroupId,
                   CreatedAt, UpdatedAt
            FROM sad.CmdbApplication
            WHERE (@Environment IS NULL OR Environment = @Environment)
            ORDER BY ApplicationCode
            """;
        using var connection = connectionFactory.Create();
        var command = new CommandDefinition(sql, new { Environment = environment }, cancellationToken: ct);
        var rows = await connection.QueryAsync<CmdbApplication>(command);
        return rows.AsList();
    }

    public async Task<CmdbApplication?> GetApplicationByCodeAsync(string applicationCode, CancellationToken ct)
    {
        const string sql = """
            SELECT ApplicationId, ApplicationCode, ApplicationName, Description, BusinessCriticality,
                   Environment, LifecycleStatus, TechnologyPlatform, OperatingSystemRequirement,
                   CpuRequirement, MemoryRequirementGb, StorageRequirementGb, ExpectedAnnualGrowthPercent,
                   AvailabilityTier, DataClassification, PreferredLocation, OwnerEmployeeId, SupportGroupId,
                   CreatedAt, UpdatedAt
            FROM sad.CmdbApplication WHERE ApplicationCode = @ApplicationCode
            """;
        using var connection = connectionFactory.Create();
        var command = new CommandDefinition(sql, new { ApplicationCode = applicationCode }, cancellationToken: ct);
        return await connection.QuerySingleOrDefaultAsync<CmdbApplication>(command);
    }

    public async Task<IReadOnlyList<InfrastructureCluster>> GetClustersAsync(string? environment, CancellationToken ct)
    {
        const string sql = """
            SELECT ClusterId, ClusterCode, ClusterName, ClusterType, Platform, OperatingSystem, Environment,
                   DataCenter, Region, LifecycleStatus, NodeCount, TotalCpuCores, TotalMemoryGb, TotalStorageGb,
                   ReservedCpuPercent, ReservedMemoryPercent, MonthlyCost, AvailabilityTier,
                   ComplianceClassification, CreatedAt, UpdatedAt
            FROM sad.InfrastructureCluster
            WHERE (@Environment IS NULL OR Environment = @Environment)
            ORDER BY ClusterCode
            """;
        using var connection = connectionFactory.Create();
        var command = new CommandDefinition(sql, new { Environment = environment }, cancellationToken: ct);
        var rows = await connection.QueryAsync<InfrastructureCluster>(command);
        return rows.AsList();
    }

    public async Task<InfrastructureCluster?> GetClusterByIdAsync(int clusterId, CancellationToken ct)
    {
        const string sql = """
            SELECT ClusterId, ClusterCode, ClusterName, ClusterType, Platform, OperatingSystem, Environment,
                   DataCenter, Region, LifecycleStatus, NodeCount, TotalCpuCores, TotalMemoryGb, TotalStorageGb,
                   ReservedCpuPercent, ReservedMemoryPercent, MonthlyCost, AvailabilityTier,
                   ComplianceClassification, CreatedAt, UpdatedAt
            FROM sad.InfrastructureCluster WHERE ClusterId = @ClusterId
            """;
        using var connection = connectionFactory.Create();
        var command = new CommandDefinition(sql, new { ClusterId = clusterId }, cancellationToken: ct);
        return await connection.QuerySingleOrDefaultAsync<InfrastructureCluster>(command);
    }
}
