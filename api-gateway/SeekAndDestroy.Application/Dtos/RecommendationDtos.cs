using System.Text.Json.Nodes;

namespace SeekAndDestroy.Application.Dtos;

// These DTOs deliberately carry the AI service's JSON through as JsonNode for
// the fields whose shape is the Python service's authoritative contract
// (candidate lists, scores, evidence) - the gateway maps stable outer fields
// into DTOs but does not re-declare the deterministic engines' internal
// shapes, so the two services can't drift silently out of sync.

/// <summary>Explain adds LLM narration alongside the computed result. Off by
/// default: these endpoints can return hundreds of candidates and narration is
/// a model call, so nobody pays for prose they did not ask for.</summary>
public sealed record HostingRecommendationRequestDto(string ApplicationCode, bool Explain = false);

public sealed record CapacityRecommendationRequestDto(
    string Environment, decimal CpuCores, decimal MemoryGb, decimal StorageGb, string Platform,
    string AvailabilityTier, string DataClassification, string? PreferredLocation,
    decimal ExpectedGrowthPercent, DateOnly? RequiredByDate, int RequestedByEmployeeId, int? ApplicationId);

public sealed record RightSizingClusterRequestDto(string? ClusterCode);

public sealed record RightSizingApplicationRequestDto(string? ApplicationCode, bool Explain = false);

public sealed record ConsolidationRequestDto(string? Environment);

public sealed record ForecastRequestDto(string ClusterCode, int HorizonDays = 90, bool Explain = false);

public sealed record RecommendationDecisionRequestDto(string Decision, int ReviewerEmployeeId, string? Reason);

public sealed record AiServiceResponse(JsonNode? Payload);

/// <summary>ConversationId threads a chat together, so a follow-up like "give
/// me the options again" has something to refer to. Null starts a new
/// conversation; the AI service generates the id and returns it, and null
/// properties are omitted from the request body (see AiServiceClient), so an
/// opening message simply carries no id at all.</summary>
public sealed record CreateInvestigationRequestDto(
    string Query, int CreatedByEmployeeId, string? ConversationId = null);

/// <summary>One person's verdict on one answer.
///
/// Rating is the only required field. Demanding a reason is how a feedback
/// control stops being used, and a thumbs-up with no explanation is still the
/// data point that matters.
///
/// There is deliberately NO EmployeeId here. The AI service takes it from the
/// bearer token, so nobody can rate as somebody else by editing a payload -
/// and this is the one table whose value depends entirely on each row being
/// attributable to a real person.</summary>
public sealed record AnswerFeedbackRequestDto(
    int Rating, string? Reason = null, string? Comment = null, string? ConversationId = null);

/// <summary>SelectedClusterCode/SelectedHostName name the option the reviewer
/// chose. Approving without one leaves every recommendation PendingReview:
/// three approved placements for one workload records no decision at all.</summary>
public sealed record ResumeInvestigationRequestDto(
    string Decision, int ReviewerEmployeeId, string? Comments,
    string? SelectedClusterCode = null, string? SelectedHostName = null);
