using System.Text.Json.Nodes;
using SeekAndDestroy.Application.Dtos;

namespace SeekAndDestroy.Application.Interfaces;

/// <summary>Typed client over the Python AI service. Every method is a thin,
/// auditable pass-through to one FastAPI endpoint - the gateway never
/// recomputes or second-guesses a score, a rule result or a forecast; it only
/// maps the AI service's JSON into gateway DTOs and enforces auth/authorization.</summary>
public interface IAiServiceClient
{
    Task<JsonNode?> GetHostingRecommendationsAsync(HostingRecommendationRequestDto request, CancellationToken ct);
    Task<JsonNode?> GetCapacityRecommendationsAsync(CapacityRecommendationRequestDto request, CancellationToken ct);
    Task<JsonNode?> GetClusterRightSizingAsync(RightSizingClusterRequestDto request, CancellationToken ct);
    Task<JsonNode?> GetApplicationRightSizingAsync(RightSizingApplicationRequestDto request, CancellationToken ct);
    Task<JsonNode?> AnalyzeConsolidationAsync(ConsolidationRequestDto request, CancellationToken ct);
    Task<JsonNode?> GetForecastAsync(ForecastRequestDto request, CancellationToken ct);

    Task<JsonNode?> CreateInvestigationAsync(CreateInvestigationRequestDto request, CancellationToken ct);
    Task<JsonNode?> GetInvestigationAsync(int investigationId, CancellationToken ct);
    Task<JsonNode?> ResumeInvestigationAsync(int investigationId, ResumeInvestigationRequestDto request, CancellationToken ct);
    Task<JsonNode?> GetInvestigationRecommendationsAsync(int investigationId, CancellationToken ct);

    Task<JsonNode?> SubmitRecommendationDecisionAsync(int recommendationId, RecommendationDecisionRequestDto request, CancellationToken ct);
}
