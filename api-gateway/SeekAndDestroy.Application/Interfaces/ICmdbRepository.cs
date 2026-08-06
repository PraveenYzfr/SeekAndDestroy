using SeekAndDestroy.Domain.Entities;

namespace SeekAndDestroy.Application.Interfaces;

/// <summary>Read-only CMDB access. Every implementation must use parameterized
/// queries only - there is no execute-arbitrary-SQL path anywhere in this
/// gateway, mirroring the same rule enforced in the Python AI service.</summary>
public interface ICmdbRepository
{
    Task<IReadOnlyList<CmdbApplication>> GetApplicationsAsync(string? environment, CancellationToken ct);
    Task<CmdbApplication?> GetApplicationByCodeAsync(string applicationCode, CancellationToken ct);
    Task<IReadOnlyList<InfrastructureCluster>> GetClustersAsync(string? environment, CancellationToken ct);
    Task<InfrastructureCluster?> GetClusterByIdAsync(int clusterId, CancellationToken ct);
}
