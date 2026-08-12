using System.Text.Json.Nodes;

namespace SeekAndDestroy.Application.Dtos;

// These DTOs deliberately carry the AI service's JSON through as JsonNode for
// the fields whose shape is the Python service's authoritative contract
// (candidate lists, scores, evidence) - the gateway maps stable outer fields
// into DTOs but does not re-declare the deterministic engines' internal
// shapes, so the two services can't drift silently out of sync.

public sealed record HostingRecommendationRequestDto(string ApplicationCode);

public sealed record CapacityRecommendationRequestDto(
    string Environment, decimal CpuCores, decimal MemoryGb, decimal StorageGb, string Platform,
    string AvailabilityTier, string DataClassification, string? PreferredLocation,
    decimal ExpectedGrowthPercent, DateOnly? RequiredByDate, int RequestedByEmployeeId, int? ApplicationId);

public sealed record RightSizingClusterRequestDto(string? ClusterCode);

public sealed record RightSizingApplicationRequestDto(string? ApplicationCode);

public sealed record ConsolidationRequestDto(string? Environment);

public sealed record ForecastRequestDto(string ClusterCode, int HorizonDays = 90);

public sealed record RecommendationDecisionRequestDto(string Decision, int ReviewerEmployeeId, string? Reason);

public sealed record AiServiceResponse(JsonNode? Payload);

public sealed record CreateInvestigationRequestDto(string Query, int CreatedByEmployeeId);

/// <summary>SelectedClusterCode/SelectedHostName name the option the reviewer
/// chose. Approving without one leaves every recommendation PendingReview:
/// three approved placements for one workload records no decision at all.</summary>
public sealed record ResumeInvestigationRequestDto(
    string Decision, int ReviewerEmployeeId, string? Comments,
    string? SelectedClusterCode = null, string? SelectedHostName = null);
